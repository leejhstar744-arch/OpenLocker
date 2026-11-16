"""
chat_bridge.py
==============
dashboard.html(브라우저)이 직접 Ollama나 Go 게이트웨이를 호출하지 않고,
반드시 이 브리지를 거치도록 만드는 아주 얇은 로컬 HTTP 서버.

    브라우저 (fetch) -> chat_bridge.py(:8000) -> ai_llm_layer.py -> gateway_client.py -> Go 게이트웨이(:8080)

왜 브라우저가 직접 호출하면 안 되는가:
    만약 dashboard.html이 Ollama(:11434)를 직접 fetch로 호출하면, LLM의 원문 응답이
    tool_call_parser.py의 화이트리스트 검증을 거치지 않은 채 그대로 브라우저에 노출되고,
    사용자가(또는 브라우저 확장 프로그램이) 그 응답을 조작해 게이트웨이에 직접
    임의의 tool/args를 보낼 수도 있게 된다. 이 브리지가 항상 파서를 통과시키는
    유일한 경로가 되도록 강제함으로써, 그런 우회 경로 자체를 없앤다.

실행:
    python3 chat_bridge.py
    (ai_llm_layer.py, tool_call_parser.py, gateway_client.py와 같은 폴더에 있어야 함)
"""

from __future__ import annotations

import json
import logging
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ai_llm_layer import OllamaError, handle_user_request, looks_like_failed_tool_call, to_gateway_payload
from gateway_client import GatewayError, submit_and_wait

logger = logging.getLogger("chat_bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PORT = 8000


class ChatBridgeHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        # 대시보드가 file:// 또는 다른 포트에서 열릴 수 있으므로 CORS 허용.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802 (브라우저 preflight 요청 대응)
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):  # noqa: N802
        if self.path != "/chat":
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
            text = body["text"]
        except (json.JSONDecodeError, KeyError):
            self._send_json(400, {"error": "요청 본문에 text 필드가 필요합니다"})
            return

        session_id = str(uuid.uuid4())
        logger.info("채팅 요청 수신 (session=%s): %s", session_id, text)

        try:
            layer_result = handle_user_request(text)
        except OllamaError as e:
            self._send_json(502, {"status": "failed", "error": f"LLM 오류: {e}"})
            return

        payload = to_gateway_payload(layer_result, session_id=session_id)
        if payload is None:
            if looks_like_failed_tool_call(layer_result.raw_llm_output):
                # 도구 호출을 시도는 했는데 형식이 깨져 파서가 거부한 경우.
                # 깨진 JSON 잔해를 그대로 보여주지 않고, 무엇이 문제였는지 원인을
                # 안내한 뒤 재시도를 유도한다 - 사용자가 "시스템 고장"으로 오해하지 않도록.
                logger.warning("Tool Call 파싱 실패로 보이는 응답 (session=%s)", session_id)
                self._send_json(200, {
                    "status": "parse_failed",
                    "raw_output": (
                        "요청을 실행 가능한 형식으로 만드는 데 실패했어요 "
                        "(모델이 생성한 코드에 문법 오류가 있었을 가능성이 높아요). "
                        "같은 요청을 다시 보내보시거나, 조금 더 간단하게 표현해보세요."
                    ),
                })
            else:
                # 애초에 도구 호출 의도가 없던 일반 대화 응답 - LLM의 답변을 그대로 보여준다.
                self._send_json(200, {"status": "no_tool_call", "raw_output": layer_result.raw_llm_output})
            return

        try:
            exec_result = submit_and_wait(payload)
        except GatewayError as e:
            self._send_json(502, {"status": "failed", "error": f"게이트웨이 오류: {e}", "session_id": session_id})
            return

        self._send_json(200, {
            "status": exec_result.status,
            "result": exec_result.result,
            "error": exec_result.error,
            "session_id": exec_result.session_id,
        })

    def log_message(self, format, *args):  # noqa: A002
        # BaseHTTPRequestHandler의 기본 stderr 로그 대신, 위에서 이미 남긴 logger로 충분하므로 무음화.
        pass


if __name__ == "__main__":
    # 포인트: "localhost"가 아니라 "0.0.0.0"에 바인딩해야 한다. "localhost"로 바인딩하면
    # 이 프로세스가 실행 중인 기기 자신만 접속 가능해서, 원격(터널/사설망)으로 연결된 팀원의
    # 기기에서는 이 브리지에 아예 도달할 수조차 없다. "0.0.0.0"은 모든 네트워크
    # 인터페이스에서 들어오는 연결을 받는다.
    # 주의: 인바운드 포트를 공유기/방화벽에서 개방하지 않았다는 전제에서만 안전하다.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), ChatBridgeHandler)
    logger.info("chat_bridge 시작: 0.0.0.0:%d (POST /chat)", PORT)
    server.serve_forever()
