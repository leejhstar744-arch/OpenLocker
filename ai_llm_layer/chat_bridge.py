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

from ai_llm_layer import OllamaError, handle_user_request, to_gateway_payload
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
            # 도구 호출이 없었던 경우 - LLM의 일반 텍스트 답변을 그대로 보여준다.
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
    server = ThreadingHTTPServer(("localhost", PORT), ChatBridgeHandler)
    logger.info("chat_bridge 시작: http://localhost:%d (POST /chat)", PORT)
    server.serve_forever()
