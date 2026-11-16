"""
benchmark.py
============
"자원 최적화 및 Docker 샌드박스 보안 인프라"라는 연구 주제를 뒷받침하려면,
"구현했다"가 아니라 "얼마나 효과적인지 측정했다"가 필요하다. 이 스크립트는
그 정량적 근거를 만들기 위한 측정 모드를 제공한다.

모드:
  llm         - Ollama 순수 추론 지연시간만 측정 (콜드/웜 스타트 차이 관찰용)
  pipeline    - 전체 파이프라인(LLM->파서->게이트웨이->Docker) 엔드투엔드 지연시간
  concurrency - Go 게이트웨이의 동시 세션 제한(세마포어)이 실제로 작동하는지 부하 테스트
  compare     - 두 CSV(예: runc vs runsc) 결과를 A/B 비교

사용 예:
  python3 benchmark.py llm --runs 10
  python3 benchmark.py pipeline --runs 5 --warmup 1
  python3 benchmark.py concurrency --concurrent 5
  python3 benchmark.py llm --runs 10 --csv results_llm.csv
  python3 benchmark.py compare --before runc.csv --after runsc.csv

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
    warmup 횟수만큼은 결과 집계에서 제외한다 - Ollama는 일정 시간 요청이 없으면
    모델을 메모리에서 내리므로, 첫 요청은 "모델 재로딩" 비용이 섞여 순수 추론 성능을 왜곡한다.
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
    포인트: 여기서 측정되는 시간은 LLM 추론 시간 + 컨테이너 실행/폐기 시간의 합이다.
    LLM 지연시간(bench_llm 결과)과 비교하면, "인프라 계층이 전체 지연시간에서
    차지하는 비중"을 역산할 수 있다.
    주의: LLM 추론 변동성(수 초)이 gVisor 오버헤드(수십~수백 ms)를 덮어버리므로,
    이 모드로 runc/runsc A/B 비교를 하면 결론이 뒤집힐 수 있다 (README 한계 참고).
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
    기대 결과: concurrent_n개 중 최대 2개만 202(running)를 받고, 나머지는 429(rejected)여야 한다.
    """
    print(f"[동시성 벤치마크] {concurrent_n}개 요청을 동시에 게이트웨이로 전송")

    # 약간의 지연이 있는 코드를 써서, 세션들이 겹치는 창(window)을 인위적으로 넓힌다.
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
    print("  기대값: accepted는 게이트웨이의 MaxConcurrentSessions(기본 2)를 넘지 않아야 함")

    return results


# -----------------------------------------------------------------------
# 모드 4: 두 벤치마크 결과(CSV) 비교 - gVisor 적용 전/후처럼 "조건 하나만 바꾼" A/B 비교용
# -----------------------------------------------------------------------
def bench_compare(before_csv: str, after_csv: str, before_label: str, after_label: str) -> None:
    """
    같은 방식(llm 또는 pipeline)으로 저장된 두 CSV를 읽어 평균/표준편차를 비교한다.

    포인트: 단순히 평균값만 나열하면 "우연히 그런 것 아니냐"는 반박에 취약하다.
    표준편차와 변화율(%)을 함께 보여주면, 그 차이가 노이즈 수준인지
    실제로 유의미한 차이인지 판단할 근거가 생긴다.
    """
    def _load(path: str) -> list[float]:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return [float(row["elapsed_sec"]) for row in reader if row["status"] == "ok"]

    before = _load(before_csv)
    after = _load(after_csv)

    if not before or not after:
        print("비교할 데이터가 부족합니다 (성공한 측정치가 있는 CSV인지 확인하세요).")
        return

    b_mean, a_mean = statistics.mean(before), statistics.mean(after)
    b_std = statistics.stdev(before) if len(before) > 1 else 0.0
    a_std = statistics.stdev(after) if len(after) > 1 else 0.0
    diff_pct = ((a_mean - b_mean) / b_mean) * 100 if b_mean else 0.0

    print(f"\n=== A/B 비교: {before_label} vs {after_label} ===")
    print(f"{'항목':<12}{before_label:>15}{after_label:>15}")
    print(f"{'표본 수':<12}{len(before):>15}{len(after):>15}")
    print(f"{'평균(초)':<12}{b_mean:>15.2f}{a_mean:>15.2f}")
    print(f"{'표준편차':<12}{b_std:>15.2f}{a_std:>15.2f}")
    print(f"{'최소':<12}{min(before):>15.2f}{min(after):>15.2f}")
    print(f"{'최대':<12}{max(before):>15.2f}{max(after):>15.2f}")
    print(f"\n변화율: {diff_pct:+.1f}% ({after_label}이 {before_label} 대비)")

    if diff_pct > 0:
        print(f"-> {after_label}이 평균적으로 {abs(diff_pct):.1f}% 더 느립니다.")
    else:
        print(f"-> {after_label}이 평균적으로 {abs(diff_pct):.1f}% 더 빠릅니다.")

    # 표준편차가 두 평균의 차이보다 크면, 그 차이가 노이즈에 묻힐 수 있다는 뜻이므로 경고한다.
    if abs(a_mean - b_mean) < max(b_std, a_std):
        print("주의: 평균 차이가 표준편차보다 작습니다 - 표본을 늘려(--runs 증가) 재측정을 권장합니다.")


def main() -> None:
    parser = argparse.ArgumentParser(description="샌드박스 아키텍처 성능 벤치마크")
    parser.add_argument("mode", choices=["llm", "pipeline", "concurrency", "compare"])
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="llm/pipeline 모드에서 사용할 프롬프트")
    parser.add_argument("--runs", type=int, default=5, help="llm/pipeline 모드 반복 횟수")
    parser.add_argument("--warmup", type=int, default=1, help="집계에서 제외할 웜업 횟수")
    parser.add_argument("--concurrent", type=int, default=5, help="concurrency 모드에서 동시에 쏠 요청 수")
    parser.add_argument("--csv", default=None, help="결과를 저장할 CSV 파일 경로 (선택)")
    parser.add_argument("--before", default=None, help="compare 모드: 이전(예: runc) 결과 CSV 경로")
    parser.add_argument("--after", default=None, help="compare 모드: 이후(예: runsc) 결과 CSV 경로")
    parser.add_argument("--before-label", default="runc", help="compare 모드: 이전 조건 라벨")
    parser.add_argument("--after-label", default="runsc(gVisor)", help="compare 모드: 이후 조건 라벨")
    args = parser.parse_args()

    if args.mode == "llm":
        results = bench_llm(args.prompt, args.runs, args.warmup)
    elif args.mode == "pipeline":
        results = bench_pipeline(args.prompt, args.runs, args.warmup)
    elif args.mode == "concurrency":
        results = bench_concurrency(args.concurrent)
    else:
        if not args.before or not args.after:
            print("compare 모드는 --before와 --after CSV 경로가 모두 필요합니다.")
            return
        bench_compare(args.before, args.after, args.before_label, args.after_label)
        return

    if args.csv:
        _write_csv(args.csv, results)


if __name__ == "__main__":
    main()
