"""
Tool Call 파서
==============
LLM이 생성한 자연어 응답 안에서 "도구 호출(Tool Call)" 명령을 찾아내고,
그 명령이 실행해도 안전한지 검증하는 모듈이다.

이 모듈이 아키텍처에서 맡는 위치:
  로컬 LLM 추론(Ollama) -> [이 모듈] -> 이벤트 중계 API(Go) -> 샌드박스 매니저
LLM은 텍스트만 출력할 뿐 실행 권한이 없고, 이 모듈을 통과해야만
"실행 가능한 요청"으로 인정된다. 즉 여기가 첫 번째 보안 검문소다.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("tool_call_parser")


# -----------------------------------------------------------------------
# 허용된 도구(함수) 목록 화이트리스트
# -----------------------------------------------------------------------
# 왜 화이트리스트 방식인가?
#   블랙리스트(금지 목록)는 "생각하지 못한 위험한 함수"를 놓칠 수 있다.
#   화이트리스트는 반대로 "명시적으로 안전하다고 검토한 것만" 통과시키므로,
#   LLM이 프롬프트 인젝션 등으로 예상 밖의 함수명을 생성해도 자동으로 막힌다.
#
# required_args / optional_args를 함수별로 명시한 이유:
#   인자 구조까지 검증해야, "허용된 함수명이지만 악의적인 인자"
#   (예: run_code에 시스템 전체를 삭제하는 코드)를 걸러내는 두 번째 방어선이 생긴다.
#   (단, 이 모듈은 인자의 '형식'만 검증하고 '내용의 위험성'까지는 판단하지 않는다 —
#    코드 내용 자체의 위험성 검사는 샌드박스 격리 계층의 책임으로 분리했다.)
ALLOWED_TOOLS = {
    "run_code": {
        "required_args": ["language", "code"],
        "optional_args": ["timeout_sec"],
    },
    "read_file": {
        "required_args": ["path"],
        "optional_args": [],
    },
    "write_file": {
        "required_args": ["path", "content"],
        "optional_args": [],
    },
    "search_web": {
        "required_args": ["query"],
        "optional_args": [],
    },
}

# 코드 실행 요청의 최대 길이(byte).
# 왜 필요한가: LLM이 (혹은 프롬프트 인젝션에 의해) 매우 긴 코드를 생성해
# 샌드박스 컨테이너의 파싱/실행 단계에서 자원을 과도하게 소모하는 것을
# 이 단계에서 미리 차단하기 위함이다. 온디바이스 CPU 환경에서는
# "실행 전에 걸러내는 것"이 "실행 중 감시하는 것"보다 훨씬 저렴하다.
MAX_CODE_LENGTH = 20_000


# run_code의 timeout_sec에 허용되는 값의 범위.
# 왜 여기서도 상한을 강제하는가:
#   이벤트 중계 API(Go)는 SessionTimeout(30초)을 이미 하드코딩으로 강제하고 있어서
#   이 상한을 넘는 값은 어차피 게이트웨이 단에서 의미가 없어진다.
#   하지만 파서 단계에서도 같은 정책을 명시적으로 반영해두지 않으면,
#   "LLM이 5000초를 요청했는데 왜 30초 만에 끊기지?" 같은 계층 간 불일치가
#   디버깅을 어렵게 만든다. 두 계층의 정책을 일치시켜 계약을 명확히 한다.
MIN_TIMEOUT_SEC = 1
MAX_TIMEOUT_SEC = 30  # event_relay_api.go의 SessionTimeout(30 * time.Second)과 반드시 동일하게 유지할 것


class ToolCallParseError(Exception):
    """
    Tool Call 파싱/검증 실패 시 발생하는 예외.
    일반 Exception이 아니라 별도 클래스로 둔 이유는, 상위 계층(이벤트 중계 API)이
    "이 요청은 명시적으로 거부되었다"는 사실을 다른 종류의 오류(버그, 네트워크 오류 등)와
    구분해서 처리할 수 있게 하기 위해서다.
    """


@dataclass
class ToolCall:
    """
    검증을 통과한(혹은 통과 시도 중인) 도구 호출 하나를 표현하는 자료구조.

    name: 호출하려는 도구 이름 (예: "run_code")
    args: 도구에 전달할 인자 딕셔너리
    raw_text: 원본 JSON 문자열. 실행에는 쓰이지 않고 디버깅/감사 로그용으로만 보관한다.
              (온디바이스 보안 인프라 특성상, 나중에 "왜 이 요청이 거부/승인됐는지"
              추적할 수 있어야 하므로 원문을 버리지 않는다.)
    """
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""


# -----------------------------------------------------------------------
# JSON 추출 로직 (JSONDecoder 기반 - 정규식 아님)
# -----------------------------------------------------------------------
# 왜 정규식을 쓰지 않는가:
#   처음엔 정규식([^{}]*)으로 JSON 블록을 찾았는데, 실제로 LLM이
#   {"tool": "run_code", "args": {"language": ..., "code": ...}} 처럼
#   "args" 안에 또 중괄호가 중첩된 JSON을 코드펜스 없이 그대로 출력하는 경우,
#   [^{}]* 패턴은 중첩된 { }를 절대 통과하지 못해 통째로 매칭 실패하는 버그가 있었다.
#   (정규식은 원래 중첩 구조를 다루기에 적합하지 않은 도구다.)
#
#   그래서 json.JSONDecoder.raw_decode를 직접 써서, 텍스트 어디서 '{'가 나오든
#   그 지점부터 실제로 파싱 가능한 JSON 객체를 정확히(중첩 포함) 찾아내는 방식으로 바꿨다.
#   이 방식은 코드펜스가 있든 없든 동일하게 동작하므로, 코드펜스 유무에 따라
#   다른 정규식을 쓰던 기존의 이원화된 로직 자체가 필요 없어졌다.

_FENCE_STRIP_RE = re.compile(r"```json\s*|```", re.IGNORECASE)


def _iter_json_objects(text: str):
    """
    텍스트 전체를 스캔하며 파싱 가능한 최상위 JSON 객체를 순서대로 찾아 yield한다.

    동작 원리:
      1) 다음 '{' 위치를 찾는다
      2) 그 지점부터 JSONDecoder.raw_decode로 파싱을 시도한다
         - 성공하면: 그 객체를 반환하고, 커서를 객체가 끝난 지점으로 이동
         - 실패하면: 그 '{'는 유효한 JSON의 시작이 아니었다는 뜻이므로,
           한 글자만 건너뛰고 다음 '{'를 다시 찾는다
    이 방식이 중요한 이유: raw_decode는 중첩된 객체({ 안에 {})를 표준 json 파서로
    처리하므로, 정규식과 달리 중첩 깊이에 상관없이 항상 정확하다.
    """
    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
    while idx < n:
        brace_idx = text.find("{", idx)
        if brace_idx == -1:
            break
        try:
            obj, end_idx = decoder.raw_decode(text, brace_idx)
            yield obj
            idx = end_idx
        except json.JSONDecodeError:
            idx = brace_idx + 1


def extract_tool_calls(llm_output: str) -> list[ToolCall]:
    """
    LLM 원문 출력 문자열에서 Tool Call 후보를 모두 추출한다.

    코드펜스(```json ... ```)가 있으면 먼저 마커만 제거하고, 코드펜스 유무와 무관하게
    동일한 스캔 로직(_iter_json_objects)으로 처리한다. 이렇게 하면 모델이 지시를
    잘 따라 코드펜스를 붙이든, 순수 JSON만 뱉든 동일하게 인식된다.
    """
    cleaned = _FENCE_STRIP_RE.sub("", llm_output)

    calls: list[ToolCall] = []
    for data in _iter_json_objects(cleaned):
        if not isinstance(data, dict):
            continue

        # "tool"과 "name" 두 키 이름을 모두 허용하는 이유:
        # 프레임워크마다(OpenAI function-calling 스타일 vs 커스텀 프롬프트) 관례가 달라서,
        # 두 가지 키 이름을 모두 받아주면 프롬프트 형식이 바뀌어도 파서를 고치지 않아도 된다.
        name = data.get("tool") or data.get("name")
        if not name:
            continue  # "tool"/"name" 키가 없는 JSON은 도구 호출이 아니라고 판단하고 건너뜀

        args = data.get("args") or data.get("arguments") or {}
        calls.append(ToolCall(name=name, args=args, raw_text=json.dumps(data, ensure_ascii=False)))

    return calls


def validate_tool_call(call: ToolCall) -> None:
    """
    화이트리스트 기준으로 도구 호출 하나를 검증한다.

    검증 순서(하나라도 실패하면 즉시 예외를 던지고 중단):
      1) 도구 이름 자체가 화이트리스트에 있는가
      2) 필수 인자가 모두 채워졌는가
      3) 화이트리스트에 없는 "알 수 없는 인자"가 섞여 있지 않은가
         (예상 밖의 인자가 있다면, 프롬프트 인젝션으로 추가 필드를 몰래 끼워 넣었을 가능성이 있다)
      4) run_code 인 경우, 코드 길이가 상한을 넘지 않는가

    포인트: 검증 실패 시 여기서 바로 실행을 막지 않고 "예외를 던지기만" 하는 이유는,
    이 함수는 순수 검증 로직만 담당하고, 실패했을 때 무엇을 할지(로그/거부 응답 등)는
    호출하는 쪽(parse_and_validate, 나아가 이벤트 중계 API)의 책임으로 분리하기 위함이다.
    """
    spec = ALLOWED_TOOLS.get(call.name)
    if spec is None:
        raise ToolCallParseError(f"허용되지 않은 도구입니다: {call.name}")

    missing = [a for a in spec["required_args"] if a not in call.args]
    if missing:
        raise ToolCallParseError(f"'{call.name}' 필수 인자 누락: {missing}")

    allowed_keys = set(spec["required_args"]) | set(spec["optional_args"])
    unknown = [k for k in call.args if k not in allowed_keys]
    if unknown:
        raise ToolCallParseError(f"'{call.name}' 알 수 없는 인자: {unknown}")

    # run_code 전용 추가 검증: 코드 길이 제한
    # (다른 도구는 각자의 위험 특성에 맞는 전용 검증을 이 아래에 추가하면 된다.
    #  예: write_file은 path가 특정 디렉토리 하위인지 검증하는 로직을 추가할 수 있음)
    if call.name == "run_code":
        code = call.args.get("code", "")
        if len(code.encode("utf-8")) > MAX_CODE_LENGTH:
            raise ToolCallParseError("실행 코드 길이가 제한을 초과했습니다")

        # timeout_sec 값 검증 및 클램프/정규화
        # 포인트: timeout_sec은 optional_args에 속한 '선택' 인자다.
        # LLM이 이 값을 아예 잘못된 타입(예: "5000ms" 같은 문자열)으로 만들었다고 해서
        # run_code 전체를 거부하면, 코드 실행이라는 핵심 요청 자체가 무산되어 버린다.
        # 그래서 여기서는 두 단계로 나눈다:
        #   1) 숫자로 바로 쓸 수 있으면 -> 범위 클램프만 적용 (기존 로직)
        #   2) 숫자로 변환할 수 없으면 -> 경고만 남기고 그냥 이 필드를 제거한다
        #      (제거하면 실행 시 시스템 기본 타임아웃 정책이 그대로 적용되므로 안전하다)
        # "필수 인자 위반"과 "선택 인자의 형식 오류"의 심각도가 다르다는 것이
        # 이 분기의 핵심 설계 판단이다.
        if "timeout_sec" in call.args:
            raw_timeout = call.args["timeout_sec"]

            if isinstance(raw_timeout, bool):
                # bool은 Python에서 int의 서브클래스라 isinstance(True, int)가 True이므로
                # 명시적으로 걸러내지 않으면 True/False가 1/0으로 둔갑해 조용히 통과해버린다.
                logger.warning("timeout_sec에 불리언 값(%r)이 들어와 무시합니다", raw_timeout)
                del call.args["timeout_sec"]
            elif isinstance(raw_timeout, (int, float)):
                clamped = max(MIN_TIMEOUT_SEC, min(raw_timeout, MAX_TIMEOUT_SEC))
                if clamped != raw_timeout:
                    logger.warning(
                        "timeout_sec=%s 값이 허용 범위(%d~%d초)를 벗어나 %s초로 조정됨",
                        raw_timeout, MIN_TIMEOUT_SEC, MAX_TIMEOUT_SEC, clamped,
                    )
                call.args["timeout_sec"] = clamped
            else:
                # 문자열 등 숫자가 아닌 타입: 안에 숫자가 섞여 있으면(예: "5000ms") 추출을 시도하고,
                # 그마저 실패하면 필드 자체를 제거해 요청은 계속 진행되게 한다.
                digits = re.match(r"\s*(\d+(\.\d+)?)", str(raw_timeout))
                if digits:
                    coerced = float(digits.group(1))
                    clamped = max(MIN_TIMEOUT_SEC, min(coerced, MAX_TIMEOUT_SEC))
                    logger.warning(
                        "timeout_sec=%r (문자열)에서 %s초를 추출해 사용합니다",
                        raw_timeout, clamped,
                    )
                    call.args["timeout_sec"] = clamped
                else:
                    logger.warning(
                        "timeout_sec=%r 값을 숫자로 해석할 수 없어 무시하고 기본 정책을 따릅니다",
                        raw_timeout,
                    )
                    del call.args["timeout_sec"]


def parse_and_validate(llm_output: str) -> list[ToolCall]:
    """
    전체 파이프라인의 진입점: 추출(extract) -> 검증(validate)까지 한 번에 수행한다.

    검증에 실패한 호출은 최종 결과 리스트에서 '조용히' 제외된다.
    (거부 사실 자체는 print로만 남기고 있는데, 실제 운영 환경에서는 이 부분을
    구조화된 로깅으로 바꾸어 이벤트 중계 API 쪽 감사 로그와 연결하는 것을 권장한다 —
    "왜 이 세션이 거부됐는지"를 나중에 추적할 수 있어야 하기 때문이다.)
    """
    calls = extract_tool_calls(llm_output)
    validated: list[ToolCall] = []
    for call in calls:
        try:
            validate_tool_call(call)
            validated.append(call)
        except ToolCallParseError as e:
            print(f"[거부됨] {call.name}: {e}")
            print(f"[거부된 원본] {call.raw_text}")
    return validated


if __name__ == "__main__":
    # 간단한 동작 확인용 샘플. 실제 유닛테스트로 대체하는 것을 권장한다.
    sample_output = '''
    사용자 요청을 처리하기 위해 다음 코드를 실행하겠습니다.
    ```json
    {"tool": "run_code", "args": {"language": "python", "code": "print(1+1)"}}
    ```
    '''
    for call in parse_and_validate(sample_output):
        print(call)
