"""
AI/LLM 계층 통합 모듈 (ai_llm_layer.py)
=======================================
아키텍처의 ① AI/LLM 계층 전체를 하나로 묶은 파일이다.

    사용자 요청 -> [프롬프트 엔지니어링] -> [로컬 LLM 추론(Ollama)] -> [Tool Call 파서] -> 결과

이 파일이 반환하는 결과는 그대로 ② 백엔드/파이프라인 계층(이벤트 중계 API)에
전달할 수 있는 형태로 정리된다. 즉 이 모듈의 산출물이 다음 계층의 입력 규격이다.

사전 준비 (지후님 로컬 PC에서):
    curl -fsSL https://ollama.com/install.sh | sh
    ollama pull llama3.2:3b
    ollama serve   # 보통 설치 시 자동으로 백그라운드 서비스 등록됨

의존 패키지:
    pip install requests --break-system-packages
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

from tool_call_parser import ToolCall, parse_and_validate

logger = logging.getLogger("ai_llm_layer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# -----------------------------------------------------------------------
# 설정값
# -----------------------------------------------------------------------
# 왜 상수로 분리했는가: 모델 교체(예: llama3.2:3b -> qwen2.5:3b)나
# 엔드포인트 변경이 잦을 것으로 예상되는 값들을 코드 곳곳에 흩어놓지 않고
# 한 곳에서 관리하기 위함이다.
OLLAMA_HOST = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2:3b"
REQUEST_TIMEOUT_SEC = 60  # LLM 호출은 CPU 추론이라 다소 길게 잡음
MAX_RETRIES = 2
RETRY_BACKOFF_SEC = 1.5


# -----------------------------------------------------------------------
# Step 2: 프롬프트 엔지니어링
# -----------------------------------------------------------------------
# 포인트: 이 시스템 프롬프트는 tool_call_parser.py의 ALLOWED_TOOLS와
# 반드시 1:1로 동기화되어야 한다. 파서에 도구를 추가/삭제하면 아래 목록도 갱신할 것.
#
# few-shot 예시를 추가한 이유:
#   3B급 소형 모델은 지시만 텍스트로 주면 가끔 코드 중간에 관계없는 토큰
#   (예: "debugger", "g" 같은 조각)을 끼워 넣는 경향이 있었다. 이는 모델이
#   "정확히 어떤 모양의 출력을 원하는지"를 형식 설명만으로는 확실히 못 잡을 때
#   흔히 나타나는 현상이라, 실제 입출력 쌍을 1~2개 보여주는 few-shot이
#   가장 직접적인 해결책이다. 예시의 code 필드는 문법적으로 완전히 깨끗한
#   코드로만 구성해서, 모델이 "이 정도 완성도로 작성해야 한다"는 기준을
#   그대로 모방하도록 유도한다.
SYSTEM_PROMPT = """당신은 코드 실행을 도와주는 에이전트입니다.
사용자 요청을 처리하려면 아래 형식으로만 도구를 호출하세요.

허용된 도구:
- run_code(language, code, timeout_sec?): 코드를 샌드박스에서 실행합니다.
- read_file(path): 파일을 읽습니다.
- write_file(path, content): 파일에 내용을 씁니다.
- search_web(query): 웹을 검색합니다.

도구 호출이 필요하면 반드시 아래 형식만 사용하세요 (다른 형식은 인식되지 않습니다):
```json
{"tool": "도구이름", "args": {"인자명": "값"}}
```

예시 1)
사용자: "1부터 10까지 더한 값을 출력해줘"
답변:
```json
{"tool": "run_code", "args": {"language": "python", "code": "total = 0\\nfor i in range(1, 11):\\n    total += i\\nprint(total)"}}
```

예시 2)
사용자: "리스트 [3, 1, 4, 1, 5]를 정렬해서 출력해줘"
답변:
```json
{"tool": "run_code", "args": {"language": "python", "code": "numbers = [3, 1, 4, 1, 5]\\nprint(sorted(numbers))"}}
```

중요 규칙:
- code 필드에는 실행 가능한 완전한 코드만 작성하세요. 설명, 주석, 디버깅 메모,
  "debugger" 같은 관계없는 단어를 코드 중간에 절대 섞지 마세요.
- 코드는 예시처럼 문법적으로 완전해야 하며, 마지막에 결과를 print()로 출력해야 합니다.
- 도구 호출이 필요 없는 단순 질문이면 그냥 일반 텍스트로 답하세요.
- 한 번에 하나의 도구만 호출하세요."""


def build_prompt(user_input: str, extra_context: Optional[str] = None) -> str:
    """
    사용자 입력을 LLM에 넘길 최종 프롬프트로 가공한다.

    extra_context: RAG 검색 결과나 이전 대화 요약 등을 덧붙이고 싶을 때 사용.
    지금은 단순 연결이지만, 추후 프롬프트 엔지니어링 실험(Few-shot 예시 삽입 등)을
    이 함수 안에서만 확장하면 되도록 진입점을 분리해 두었다.
    """
    if extra_context:
        return f"[참고 정보]\n{extra_context}\n\n[사용자 요청]\n{user_input}"
    return user_input


# -----------------------------------------------------------------------
# Step 3: Ollama 로컬 LLM 호출
# -----------------------------------------------------------------------

class OllamaError(Exception):
    """Ollama 호출 관련 오류 (연결 실패, 타임아웃, 비정상 응답 등)"""


def query_llm(
    user_input: str,
    model: str = DEFAULT_MODEL,
    extra_context: Optional[str] = None,
) -> str:
    """
    Ollama의 /api/generate 엔드포인트를 호출해 LLM 응답 텍스트를 받아온다.

    stream=False로 고정한 이유: 이 함수는 "완성된 텍스트에서 Tool Call을 추출"하는
    이후 단계(파서)와 맞물려 있어, 스트리밍 청크 단위로는 JSON 블록이 잘려서
    파싱이 실패할 수 있다. 완성된 응답을 한 번에 받는 쪽이 안정적이다.
    (스트리밍 UX가 필요하면 이 함수와는 별도로, 사용자에게 보여주는 용도의
    스트리밍 호출을 추가하고 Tool Call 파싱은 완성본에만 적용하는 것을 권장한다.)

    재시도 로직을 넣은 이유: 온디바이스 CPU 추론은 첫 요청 시 모델 로딩으로 인해
    지연되거나, 다른 프로세스와 CPU 경합으로 일시적으로 타임아웃될 수 있다.
    """
    prompt = build_prompt(user_input, extra_context)
    payload = {
        "model": model,
        "system": SYSTEM_PROMPT,
        "prompt": prompt,
        "stream": False,
    }

    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 2):  # 최초 1회 + 재시도 MAX_RETRIES회
        try:
            resp = requests.post(
                f"{OLLAMA_HOST}/api/generate",
                json=payload,
                timeout=REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data.get("response", "")
            if not text:
                raise OllamaError("Ollama가 빈 응답을 반환했습니다")
            return text

        except (requests.ConnectionError, requests.Timeout) as e:
            last_error = e
            logger.warning(
                "Ollama 호출 실패 (시도 %d/%d): %s", attempt, MAX_RETRIES + 1, e
            )
            if attempt <= MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SEC * attempt)  # 점진적 백오프
        except requests.HTTPError as e:
            # HTTP 오류(모델 없음, 잘못된 요청 등)는 재시도해도 같은 결과이므로 즉시 중단
            raise OllamaError(f"Ollama HTTP 오류: {e}") from e

    raise OllamaError(
        f"Ollama 연결에 반복 실패했습니다. Ollama가 실행 중인지 확인하세요 "
        f"(ollama serve). 마지막 오류: {last_error}"
    )


# -----------------------------------------------------------------------
# Step 4~5: Tool Call 파서 연결 및 통합 진입점
# -----------------------------------------------------------------------

@dataclass
class LayerResult:
    """
    AI/LLM 계층의 최종 산출물.
    다음 계층(이벤트 중계 API)이 이 구조체만 보고 바로 요청을 만들 수 있도록 설계했다.
    """
    raw_llm_output: str          # 감사/디버깅용 원본 LLM 텍스트
    tool_calls: list[ToolCall]   # 화이트리스트 검증까지 통과한 호출들
    has_tool_calls: bool         # 백엔드 계층이 분기하기 편하도록 미리 계산해 둔 플래그


def handle_user_request(
    user_input: str,
    model: str = DEFAULT_MODEL,
    extra_context: Optional[str] = None,
) -> LayerResult:
    """
    AI/LLM 계층의 단일 진입점.
    프롬프트 구성 -> LLM 호출 -> Tool Call 추출/검증까지 한 번에 수행한다.

    포인트: 이 함수는 실행을 하지 않는다 (그건 인프라/보안 계층의 책임).
    여기서 반환하는 tool_calls는 "실행해도 안전하다고 1차 검증된 요청"일 뿐,
    실제 실행 여부와 동시성 제어는 이벤트 중계 API(Go)가 결정한다.
    """
    logger.info("LLM 질의 시작 (model=%s)", model)
    llm_output = query_llm(user_input, model=model, extra_context=extra_context)
    logger.info("LLM 응답 수신 (길이=%d자)", len(llm_output))

    tool_calls = parse_and_validate(llm_output)
    logger.info("Tool Call 검증 완료: %d건 통과", len(tool_calls))

    return LayerResult(
        raw_llm_output=llm_output,
        tool_calls=tool_calls,
        has_tool_calls=len(tool_calls) > 0,
    )


def to_gateway_payload(result: LayerResult, session_id: str) -> Optional[dict[str, Any]]:
    """
    LayerResult를 이벤트 중계 API(event_relay_api.go)의 ExecuteRequest 스키마에
    맞는 dict로 변환한다. 도구 호출이 없으면 None을 반환한다 (전달할 것이 없으므로).

    포인트: 여러 개의 tool_call이 검증을 통과했더라도, 지금은 첫 번째 호출만
    전달한다. 시스템 프롬프트에서 "한 번에 하나의 도구만 호출하라"고 명시했으므로
    이 정책과 일치시킨 것이다. 멀티 호출을 지원하려면 이 함수와 Go 게이트웨이의
    ExecuteRequest 구조를 함께 확장해야 한다.
    """
    if not result.tool_calls:
        return None

    call = result.tool_calls[0]
    return {
        "session_id": session_id,
        "tool": call.name,
        "args": call.args,
    }


if __name__ == "__main__":
    # 동작 확인용 간단 실행. 실제로는 Ollama가 로컬에서 떠 있어야 성공한다.
    import uuid

    test_input = "파이썬으로 1부터 10까지 더하는 코드를 실행해줘"
    try:
        layer_result = handle_user_request(test_input)
        print("---- LLM 원문 응답 ----")
        print(layer_result.raw_llm_output)
        print("---- 검증된 Tool Call ----")
        for tc in layer_result.tool_calls:
            print(tc)

        payload = to_gateway_payload(layer_result, session_id=str(uuid.uuid4()))
        print("---- 게이트웨이 전달용 payload ----")
        print(payload)

    except OllamaError as e:
        print(f"[오류] {e}")
        print("Ollama가 로컬에서 실행 중인지 확인하세요: ollama serve")
