"""
benchmark.py
============
"자원 최적화 및 Docker 샌드박스 보안 인프라"라는 연구 주제를 뒷받침하려면,
"구현했다"가 아니라 "얼마나 효과적인지 측정했다"가 필요하다. 이 스크립트는
그 정량적 근거를 만들기 위한 세 가지 측정 모드를 제공한다.

모드:
  llm         - Ollama 순수 추론 지연시간만 측정 (콜드/웜 스타트 차이 관찰용)
  pipeline    - 전체 파이프라인(LLM->파서->게이트웨이->Docker) 엔드투엔드 지연시간
  concurrency - Go 게이트웨이의 동시 세션 제한(세마포어)이 실제로 작동하는지 부하 테스트

사용 예:
  python3 benchmark.py llm --runs 10
  python3 benchmark.py pipeline --runs 5 --warmup 1
  python3 benchmark.py concurrency --concurrent 5
  python3 benchmark.py llm --runs 10 --csv results_llm.csv

사전 준비: ai_llm_layer.py, tool_call_parser.py, gateway_client.py와 같은 폴더에 둘 것.
Ollama와 Go 게이트웨이가 모두 실행 중이어야 한다 (pipeline/concurrency 모드).
"""

from __future__ import annotations

import argparse
import csv
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Optional

import requests

from ai_llm_layer import OllamaError, handle_user_request, query_llm, to_gateway_payload
from gateway_client import GATEWAY_HOST, GatewayError, submit_and_wait

DEFAULT_PROMPT = "1부터 10까지 더한 값을 출력해줘"


@dataclass
class RunResult:
    run_index: int
    elapsed_sec: float
    status: str          # "ok" | "error"
    detail: str = ""     # 성공 시 결과 일부, 실패 시 에러 메시지


def _print_summary(label: str, elapsed_list: list[float]) -> None:
    """
    평균/표준편차/최소/최대/중앙값을 출력한다.
    포인트: 평균만 보면 "가끔 튀는 콜드스타트"가 묻힌다. 표준편차와 최대값을
    함께 봐야 "일관되게 빠른지" vs "가끔 느려지는지"를 구분할 수 있다 -
    자원 최적화를 주장하려면 이 변동성 자체가 중요한 증거가 된다.
    """
    if not elapsed_list:
        print(f"[{label}] 성공한 측정치가 없습니다.")
        return
    n = len(elapsed_list)
    mean = statistics.mean(elapsed_list)
    stdev = statistics.stdev(elapsed_list) if n > 1 else 0.0
    print(f"\n=== {label} 요약 (n={n}) ===")
    print(f"  평균   : {mean:.2f}초")
    print(f"  표준편차: {stdev:.2f}초")
    print(f"  최소   : {min(elapsed_list):.2f}초")
    print(f"  최대   : {max(elapsed_list):.2f}초")
    print(f"  중앙값 : {statistics.median(elapsed_list):.2f}초")


def _write_csv(path: str, results: list[RunResult]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["run_index", "elapsed_sec", "status", "detail"])
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))
    print(f"CSV 저장됨: {path}")


# -----------------------------------------------------------------------
# 모드 1: LLM 순수 추론 지연시간
# -----------------------------------------------------------------------
def bench_llm(prompt: str, runs: int, warmup: int) -> list[RunResult]:
    """
    Ollama 추론만 반복 측정한다 (Tool Call 파서/게이트웨이는 거치지 않음).
    warmup 횟수만큼은 결과 집계에서 제외한다 - 이전 대화에서 확인했듯,
    Ollama는 일정 시간 요청이 없으면 모델을 메모리에서 내리므로, 첫 요청은
    "모델 재로딩" 비용이 섞여 순수 추론 성능을 왜곡한다.
    """
    print(f"[LLM 벤치마크] 웜업 {warmup}회 + 측정 {runs}회, 프롬프트: {prompt!r}")

    for i in range(warmup):
        print(f"  웜업 {i + 1}/{warmup}...")
        try:
            query_llm(prompt)
        except OllamaError as e:
            print(f"  웜업 중 오류(무시하고 계속): {e}")

    results: list[RunResult] = []
    for i in range(runs):
        start = time.monotonic()
        try:
            output = query_llm(prompt)
            elapsed = time.monotonic() - start
            results.append(RunResult(i, elapsed, "ok", output[:60]))
            print(f"  [{i + 1}/{runs}] {elapsed:.2f}초")
        except OllamaError as e:
            elapsed = time.monotonic() - start
            results.append(RunResult(i, elapsed, "error", str(e)))
            print(f"  [{i + 1}/{runs}] 실패 ({elapsed:.2f}초): {e}")

    _print_summary("LLM 추론 지연시간", [r.elapsed_sec for r in results if r.status == "ok"])
    return results


# -----------------------------------------------------------------------
# 모드 2: 전체 파이프라인 엔드투엔드 지연시간
# -----------------------------------------------------------------------
def bench_pipeline(prompt: str, runs: int, warmup: int) -> list[RunResult]:
    """
    LLM -> Tool Call 파서 -> Go 게이트웨이 -> Docker 실행까지 전체를 반복 측정한다.
    포인트: 여기서 측정되는 시간은 LLM 추론 시간 + 컨테이너 생성/실행/폐기 시간의 합이다.
    LLM 지연시간(bench_llm 결과)과 비교하면, "인프라 계층이 전체 지연시간에서
    차지하는 비중"을 역산할 수 있다 - 이게 곧 "자원 최적화 여지가 어디 있는지"를
    가리키는 데이터가 된다.
    """
    print(f"[파이프라인 벤치마크] 웜업 {warmup}회 + 측정 {runs}회, 프롬프트: {prompt!r}")

    def _one_round() -> RunResult:
        session_id = str(uuid.uuid4())
        start = time.monotonic()
        try:
            layer_result = handle_user_request(prompt)
            payload = to_gateway_payload(layer_result, session_id=session_id)
            if payload is None:
                elapsed = time.monotonic() - start
                return RunResult(-1, elapsed, "error", "도구 호출 없음 (프롬프트를 코드 실행이 필요한 요청으로 바꿔보세요)")
            exec_result = submit_and_wait(payload)
            elapsed = time.monotonic() - start
            status = "ok" if exec_result.status == "completed" else "error"
            detail = exec_result.result or exec_result.error or ""
            return RunResult(-1, elapsed, status, detail[:60])
        except (OllamaError, GatewayError) as e:
            elapsed = time.monotonic() - start
            return RunResult(-1, elapsed, "error", str(e))

    for i in range(warmup):
        print(f"  웜업 {i + 1}/{warmup}...")
        _one_round()

    results: list[RunResult] = []
    for i in range(runs):
        r = _one_round()
        r.run_index = i
        results.append(r)
        marker = "성공" if r.status == "ok" else "실패"
        print(f"  [{i + 1}/{runs}] {marker} {r.elapsed_sec:.2f}초 - {r.detail}")

    _print_summary("파이프라인 엔드투엔드", [r.elapsed_sec for r in results if r.status == "ok"])
    return results


# -----------------------------------------------------------------------
# 모드 3: 동시성 제한(세마포어) 실제 작동 검증
# -----------------------------------------------------------------------
def bench_concurrency(concurrent_n: int) -> list[RunResult]:
    """
    Go 게이트웨이의 MaxConcurrentSessions(=2) 제한이 실제로 걸리는지 검증한다.

    포인트: LLM을 거치면 Ollama가 요청을 사실상 순차 처리해버려서 "동시에 몰아치는"
    상황을 재현하기 어렵다. 그래서 이 모드는 LLM을 건너뛰고, 게이트웨이에
    직접 동일한 run_code 요청 N개를 스레드로 동시에 쏴서 세마포어 동작만 순수하게 검증한다.
    기대 결과: concurrent_n개 중 최대 2개만 202(running)를 받고, 나머지는 429(rejected)여야 한다 -
    이 숫자가 정확히 맞아떨어지는지가 "동시성 제한이 설계대로 작동한다"는 직접적인 증거다.
    """
    print(f"[동시성 벤치마크] {concurrent_n}개 요청을 동시에 게이트웨이로 전송")

    # 약간의 지연이 있는 코드를 써서, 세션들이 겹치는 창(window)을 인위적으로 넓힌다.
    # 이게 없으면 실행이 너무 빨리 끝나 세마포어가 반납되고 다음 요청이 통과해버려
    # "동시 실행 중" 상태를 관측하기 어렵다.
    sample_code = "import time\ntime.sleep(2)\nprint('done')"

    def _fire(i: int) -> RunResult:
        session_id = str(uuid.uuid4())
        payload = {
            "session_id": session_id,
            "tool": "run_code",
            "args": {"language": "python", "code": sample_code},
        }
        start = time.monotonic()
        try:
            resp = requests.post(f"{GATEWAY_HOST}/v1/execute", json=payload, timeout=5)
            elapsed = time.monotonic() - start
            status = "accepted" if resp.status_code == 202 else "rejected"
            return RunResult(i, elapsed, status, f"HTTP {resp.status_code}: {resp.json().get('message', '')}")
        except requests.RequestException as e:
            elapsed = time.monotonic() - start
            return RunResult(i, elapsed, "error", str(e))

    results: list[RunResult] = []
    # ThreadPoolExecutor로 전부 "동시에" 제출한다 (max_workers=concurrent_n으로
    # 모든 요청이 대기 없이 즉시 동시 실행되도록 보장).
    with ThreadPoolExecutor(max_workers=concurrent_n) as pool:
        futures = [pool.submit(_fire, i) for i in range(concurrent_n)]
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda r: r.run_index)
    accepted = sum(1 for r in results if r.status == "accepted")
    rejected = sum(1 for r in results if r.status == "rejected")
    errored = sum(1 for r in results if r.status == "error")

    print("\n=== 동시성 벤치마크 결과 ===")
    for r in results:
        print(f"  요청 {r.run_index}: {r.status} ({r.detail})")
    print(f"\n  accepted={accepted}, rejected={rejected}, error={errored} (총 {concurrent_n}개 중)")
    print(f"  기대값: accepted는 게이트웨이의 MaxConcurrentSessions(기본 2)를 넘지 않아야 함")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="샌드박스 아키텍처 성능 벤치마크")
    parser.add_argument("mode", choices=["llm", "pipeline", "concurrency"])
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="llm/pipeline 모드에서 사용할 프롬프트")
    parser.add_argument("--runs", type=int, default=5, help="llm/pipeline 모드 반복 횟수")
    parser.add_argument("--warmup", type=int, default=1, help="집계에서 제외할 웜업 횟수")
    parser.add_argument("--concurrent", type=int, default=5, help="concurrency 모드에서 동시에 쏠 요청 수")
    parser.add_argument("--csv", default=None, help="결과를 저장할 CSV 파일 경로 (선택)")
    args = parser.parse_args()

    if args.mode == "llm":
        results = bench_llm(args.prompt, args.runs, args.warmup)
    elif args.mode == "pipeline":
        results = bench_pipeline(args.prompt, args.runs, args.warmup)
    else:
        results = bench_concurrency(args.concurrent)

    if args.csv:
        _write_csv(args.csv, results)


if __name__ == "__main__":
    main()
