"""
게이트웨이 클라이언트 (gateway_client.py)
==========================================
① AI/LLM 계층(ai_llm_layer.py)의 결과물을 ② 백엔드/파이프라인 계층
(event_relay_api.go)으로 실제로 전달하고, 결과를 받아오는 연결 모듈.

    ai_llm_layer.handle_user_request()
        -> gateway_client.submit_and_wait()
            -> POST /v1/execute (Go 게이트웨이)
            -> GET  /v1/sessions?id=... 폴링
            -> 최종 결과 반환

사전 준비:
    Go 게이트웨이 빌드 및 실행 (별도 터미널)
        go run event_relay_api.go
    정상이면 다음 로그가 뜬다:
        이벤트 중계 API 게이트웨이 시작: :8080 (executor=Mock)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

logger = logging.getLogger("gateway_client")

GATEWAY_HOST = "http://localhost:8080"

# 폴링 설정
# 포인트: 게이트웨이의 SessionTimeout(30초)보다 폴링 타임아웃을 살짝 더 길게 잡아서,
# "게이트웨이가 타임아웃 처리를 마치고 상태를 갱신할 시간"까지 여유를 준다.
POLL_INTERVAL_SEC = 0.5
POLL_TIMEOUT_SEC = 35


class GatewayError(Exception):
    """게이트웨이 통신 관련 오류 (연결 실패, 거부 응답, 폴링 타임아웃 등)"""


@dataclass
class ExecutionResult:
    session_id: str
    status: str  # "completed" | "failed" | "timeout" | "rejected"
    result: Optional[str] = None
    error: Optional[str] = None


def submit_execution(payload: dict[str, Any]) -> None:
    """
    POST /v1/execute 호출. 게이트웨이가 즉시 거부(429/400)하면 여기서 예외를 던진다.
    202(Accepted)를 받으면 "실행이 시작됐다"는 뜻일 뿐, 아직 결과는 없다.
    """
    resp = requests.post(f"{GATEWAY_HOST}/v1/execute", json=payload, timeout=5)

    if resp.status_code == 202:
        logger.info("실행 요청 접수됨 (session=%s)", payload["session_id"])
        return

    # 429(동시성 초과)나 400(잘못된 요청)은 즉시 실패로 처리한다.
    # 포인트: 429는 재시도하면 성공할 수도 있는 '일시적' 실패이므로,
    # 실제 서비스에서는 이 예외를 잡아 backoff 후 재시도하는 로직을 상위에 두는 것을 권장한다.
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    raise GatewayError(f"실행 요청 거부됨 (status={resp.status_code}): {body.get('message', resp.text)}")


def poll_until_done(session_id: str) -> ExecutionResult:
    """
    세션 상태가 running이 아닐 때까지(completed/failed/timeout) 주기적으로 조회한다.

    왜 폴링 방식인가: 지금 게이트웨이는 스트리밍/웹소켓을 구현하지 않은 가장 단순한
    형태이기 때문이다. 실행 시간이 길어지거나 실시간 로그가 필요해지면, 이 함수를
    SSE(Server-Sent Events) 구독으로 바꾸는 것을 고려할 수 있다 - 지금은 필요성이
    확인되지 않아 가장 단순한 방식으로 시작했다 (점진적 확장 원칙).
    """
    deadline = time.monotonic() + POLL_TIMEOUT_SEC

    while time.monotonic() < deadline:
        resp = requests.get(f"{GATEWAY_HOST}/v1/sessions", params={"id": session_id}, timeout=5)
        if resp.status_code == 404:
            # 극히 짧은 순간, 세션이 등록되기 전에 조회가 먼저 도착할 수 있어 재시도한다.
            time.sleep(POLL_INTERVAL_SEC)
            continue
        resp.raise_for_status()

        state = resp.json()
        status = state.get("status")

        if status == "running":
            time.sleep(POLL_INTERVAL_SEC)
            continue

        # running이 아니면(completed/failed/timeout) 최종 상태로 간주하고 반환한다.
        return ExecutionResult(
            session_id=session_id,
            status=status,
            result=state.get("result"),
            error=state.get("error"),
        )

    # 이 지점에 도달했다는 것은 게이트웨이의 자체 타임아웃(30초)조차 응답하지 않았다는
    # 뜻이므로, 게이트웨이 자체의 장애 가능성을 의심해야 한다.
    raise GatewayError(f"세션 상태 폴링이 {POLL_TIMEOUT_SEC}초 내에 종료되지 않았습니다 (게이트웨이 장애 의심)")


def submit_and_wait(payload: dict[str, Any]) -> ExecutionResult:
    """AI/LLM 계층에서 바로 호출하는 단일 진입점: 제출 -> 폴링까지 한 번에 수행."""
    submit_execution(payload)
    return poll_until_done(payload["session_id"])


if __name__ == "__main__":
    # ai_llm_layer.py와 이어서 통합 테스트하는 예시
    import uuid

    from ai_llm_layer import OllamaError, handle_user_request, to_gateway_payload

    test_input = "파이썬으로 1부터 10까지 더하는 코드를 실행해줘"
    try:
        layer_result = handle_user_request(test_input)
        payload = to_gateway_payload(layer_result, session_id=str(uuid.uuid4()))

        if payload is None:
            print("도구 호출이 없어 게이트웨이로 보낼 것이 없습니다.")
        else:
            print("---- 게이트웨이로 전송 ----")
            print(payload)

            exec_result = submit_and_wait(payload)
            print("---- 최종 실행 결과 ----")
            print(exec_result)

    except OllamaError as e:
        print(f"[LLM 오류] {e}")
    except GatewayError as e:
        print(f"[게이트웨이 오류] {e}")
