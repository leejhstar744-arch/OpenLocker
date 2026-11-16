# OpenLocker
From d5a9a74d799328f798687113547d7c1ebbc72a7e Mon Sep 17 00:00:00 2001
From: leejhstar744-arch <leejhstar744-arch@users.noreply.github.com>
Date: Mon, 16 Nov 2026 23:39:35 +0900
Subject: [PATCH] =?UTF-8?q?=EC=B5=9C=EC=8B=A0=20=EC=BD=94=EB=93=9C=20?=
 =?UTF-8?q?=EB=B0=98=EC=98=81=20=EB=B0=8F=20README=20=EC=BD=94=EB=93=9C=20?=
 =?UTF-8?q?=EC=84=A4=EB=AA=85=20=EC=B6=94=EA=B0=80?=
MIME-Version: 1.0
Content-Type: text/plain; charset=UTF-8
Content-Transfer-Encoding: 8bit

- 웜스탠바이(idle 컨테이너 + docker exec), gVisor(runsc) 자동 감지/SANDBOX_RUNTIME 전환,
  docker stats 실시간 모니터링, exit code 기반 성공 판정
- Docker Go SDK 제거 → os/exec CLI 호출 (표준 라이브러리만 사용)
- Cloudflare Access JWT 검증 미들웨어(cf_access_middleware.go) 추가
- pandas/matplotlib 전용 이미지(sandbox-python-datasci.Dockerfile) 추가
- 데이터 시각화 few-shot, PLOT_BASE64 렌더링, parse_failed 분기
- benchmark.py compare 모드, test_datasci_direct.py, cgroup_setup.sh 추가
- 세션 최종 상태 기록 시 복사본 저장으로 CPU/MEM 값 유실 및 경쟁 상태 수정
- __pycache__ 제거, .gitignore 추가
- README: 아키텍처, 파일별 코드 설명, 실행 방법, 알려진 한계

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JCMmdc1VSBZJVSEuB27ESy
---
 .gitignore                                    |   5 +
 README.md                                     | 206 ++++++++++-
 .../__pycache__/ai_llm_layer.cpython-310.pyc  | Bin 8688 -> 0 bytes
 .../gateway_client.cpython-310.pyc            | Bin 4766 -> 0 bytes
 .../tool_call_parser.cpython-310.pyc          | Bin 8401 -> 0 bytes
 ai_llm_layer/ai_llm_layer.py                  |  43 ++-
 ai_llm_layer/benchmark.py                     |  87 ++++-
 ai_llm_layer/chat_bridge.py                   |  29 +-
 ai_llm_layer/dashboard.html                   | 248 ++++++++++++-
 ai_llm_layer/gateway_client.py                |   4 +-
 ai_llm_layer/test_datasci_direct.py           | 101 ++++++
 cgroup_setup.sh                               |  77 ++++
 gateway/cf_access_middleware.go               | 247 +++++++++++++
 gateway/docker_executor.go                    | 330 ++++++++++++++----
 gateway/event_relay_api.go                    | 148 +++++---
 gateway/go.mod                                |  21 +-
 gateway/go.sum                                |  63 ----
 gateway/sandbox-python-datasci.Dockerfile     |  31 ++
 18 files changed, 1382 insertions(+), 258 deletions(-)
 create mode 100644 .gitignore
 delete mode 100644 ai_llm_layer/__pycache__/ai_llm_layer.cpython-310.pyc
 delete mode 100644 ai_llm_layer/__pycache__/gateway_client.cpython-310.pyc
 delete mode 100644 ai_llm_layer/__pycache__/tool_call_parser.cpython-310.pyc
 create mode 100644 ai_llm_layer/test_datasci_direct.py
 create mode 100755 cgroup_setup.sh
 create mode 100644 gateway/cf_access_middleware.go
 delete mode 100644 gateway/go.sum
 create mode 100644 gateway/sandbox-python-datasci.Dockerfile

diff --git a/.gitignore b/.gitignore
new file mode 100644
index 0000000..cad4d59
--- /dev/null
+++ b/.gitignore
@@ -0,0 +1,5 @@
+__pycache__/
+*.pyc
+.venv/
+test_plot_output.png
+gateway/gateway
diff --git a/README.md b/README.md
index 2e12971..31ae372 100644
--- a/README.md
+++ b/README.md
@@ -1 +1,205 @@
-# OpenLocker
+# 🛡️ OpenLocker
+
+> **피지컬 AI 로컬 온디바이스 환경을 위한 자원 최적화 및 Docker 샌드박스 보안 인프라 구축**
+> OpenLocker 스터디랩 · GPU 없는 Ubuntu 노트북(CPU 전용 추론)에서 동작하는 로컬 AI 에이전트 실행 샌드박스
+
+로컬 LLM이 생성한 코드를 **안전하게** 실행하기 위한 4계층 파이프라인입니다.
+핵심 설계 원칙은 하나입니다.
+
+> **"생각하는 부분(LLM)"과 "행동하는 부분(코드 실행)"을 서로 다른 신뢰 경계에 둔다.**
+> LLM은 텍스트만 생성하므로 위험하지 않습니다. 위험은 *그 텍스트를 실행하는 순간* 시작됩니다.
+
+```
+사용자 → [① AI/LLM]  Ollama(llama3.2:3b) → Tool Call 파서(화이트리스트)
+       → [② 백엔드]   Go 이벤트 중계 API (세마포어 · 30초 타임아웃 · Access JWT 검증)
+       → [③ 인프라]   Docker 웜스탠바이 컨테이너 + gVisor(runsc) + cgroup 자원 제한
+       → 결과 반환 → [④ 프론트엔드] 채팅 + 실시간 자원 모니터링 대시보드
+```
+
+---
+
+## 📊 목표 달성도 (원안 대비)
+
+| 원안 목표 | 결과 | 비고 |
+|---|---|---|
+| 지능형: GPU(BC-250) + 26B 모델, 40~50 t/s | ❌ 미달성 | 실제 환경이 GPU 없는 노트북 → CPU 추론(`llama3.2:3b`)으로 스코프 재정의 |
+| 보안형: Cgroups + **gVisor(runsc)** | ✅ 달성 | `docker exec <id> dmesg`로 gVisor 부팅 로그 직접 확인 |
+| 네트워크: Cloudflare Tunnel + Zero Trust | ⏳ 코드 완료 | `cf_access_middleware.go` 구현, 도메인 확보 후 활성화 예정 |
+| 데이터셋 분석/시각화 시나리오 검증 | ✅ 달성 | pandas + matplotlib 그래프가 브라우저에 렌더링됨 |
+
+엔드투엔드 검증 예: `"1부터 10까지 더해줘"` → Ollama → 파서 → Go 게이트웨이 → Docker 컨테이너 → **`55`** 반환.
+
+---
+
+## 📁 폴더 구조
+
+```
+OpenLocker/
+├── ai_llm_layer/                       # Python: ① AI/LLM · ④ 프론트엔드 · 연구 도구
+│   ├── tool_call_parser.py             # Tool Call 추출 + 화이트리스트 검증 (첫 번째 보안 검문소)
+│   ├── ai_llm_layer.py                 # 시스템 프롬프트(few-shot) + Ollama 호출 + 파서 연결
+│   ├── gateway_client.py               # Go 게이트웨이 제출/폴링 클라이언트
+│   ├── chat_bridge.py                  # 브라우저 ↔ 파이프라인 브리지 (:8000)
+│   ├── dashboard.html                  # 채팅 + 세션 모니터링 + Canvas 자원 그래프
+│   ├── benchmark.py                    # llm / pipeline / concurrency / compare 벤치마크
+│   ├── test_datasci_direct.py          # LLM 우회, 인프라 단독 검증
+│   ├── llm_results.csv                 # LLM 추론 지연시간 실측 데이터
+│   └── pipeline_results.csv            # 파이프라인 엔드투엔드 실측 데이터
+├── gateway/                            # Go: ② 백엔드 · ③ 인프라 · 네트워크
+│   ├── event_relay_api.go              # 이벤트 중계 API, SandboxExecutor 인터페이스, main()
+│   ├── docker_executor.go              # 웜스탠바이 풀 + gVisor + docker stats 모니터링
+│   ├── cf_access_middleware.go         # Cloudflare Access JWT 서명 검증 미들웨어
+│   ├── sandbox-python-datasci.Dockerfile  # pandas/matplotlib/나눔고딕 실행 이미지
+│   └── go.mod                          # 표준 라이브러리만 사용 (외부 의존성 0개)
+└── cgroup_setup.sh                     # 호스트 레벨 cgroup v2 코어 파티셔닝 (참고용)
+```
+
+---
+
+## 🧩 코드 설명
+
+### ① AI/LLM 계층
+
+#### `tool_call_parser.py` — 첫 번째 보안 검문소
+LLM 출력 텍스트에서 `{"tool": ..., "args": {...}}` 형태의 도구 호출을 찾아 검증합니다. 여기를 통과해야만 "실행 가능한 요청"이 됩니다.
+
+- **JSON 추출 — 정규식 대신 `json.JSONDecoder().raw_decode()`**
+  처음엔 정규식(`[^{}]*`)을 썼는데, `args` 안에 중괄호가 중첩된 JSON을 하나도 잡지 못해 **Tool Call 0건 통과** 버그가 났습니다. 지금은 텍스트의 모든 `{` 위치에서 `raw_decode`를 시도해 중첩 깊이와 무관하게 정확히 파싱하고, 코드펜스 유무도 신경 쓰지 않습니다.
+- **화이트리스트 검증 (`ALLOWED_TOOLS`)** — ① 허용된 도구명인가 ② 필수 인자가 다 있는가 ③ 모르는 인자가 섞이지 않았는가(프롬프트 인젝션 대비) ④ `run_code` 코드 길이 ≤ 20KB
+- **"규칙 위반은 거부, 형식 문제는 보정" 원칙** — `timeout_sec`은 게이트웨이와 동일한 1~30초로 클램프하고, `"5000ms"` 같은 문자열은 숫자를 추출하며, 해석 불가하면 필드만 제거하고 요청은 살립니다. `True`가 `1`로 둔갑하지 않도록 `bool`을 따로 걸러냅니다.
+
+#### `ai_llm_layer.py` — 프롬프트 엔지니어링 + Ollama 연동
+- `SYSTEM_PROMPT`: 파서의 `ALLOWED_TOOLS`와 1:1로 동기화된 도구 목록 + **few-shot 예시 3개** (덧셈 · 정렬 · pandas/matplotlib 시각화)
+- 3B 소형 모델이 실제로 저지른 실수를 규칙으로 막았습니다: 코드 안 큰따옴표 금지(JSON 깨짐), 한 줄에 한 문장, 존재하지 않는 파일 읽기 금지, `set_xticks`로 x축 라벨 고정, 그래프는 `PLOT_BASE64:` 접두사로 stdout 출력
+- `query_llm()`: `stream=False`(청크로 받으면 JSON이 잘림), 연결 오류만 점진적 백오프로 재시도, HTTP 오류는 즉시 실패
+- `handle_user_request()` → `LayerResult` → `to_gateway_payload()`로 Go 게이트웨이 스키마에 맞춰 변환
+- `looks_like_failed_tool_call()`: "도구 호출을 시도했지만 형식이 깨진 응답"과 "일반 대화"를 구분해 UX를 분기
+
+### ② 백엔드/파이프라인 계층
+
+#### `event_relay_api.go` — 유일한 진입점(문지기)
+| 엔드포인트 | 역할 |
+|---|---|
+| `POST /v1/execute` | 실행 요청 접수 → 202(running) 또는 429(동시성 초과) |
+| `GET /v1/sessions?id=` | 단일 세션 상태 폴링 |
+| `GET /v1/sessions/all` | 대시보드용 전체 세션 목록 (최신순) |
+| `GET /healthz` | 생존 확인 (인증 없음) |
+
+- **`SandboxExecutor` 인터페이스** — 게이트웨이는 "무엇을 실행하는지" 모릅니다. `MockExecutor`로 파이프라인을 먼저 검증한 뒤 `DockerExecutor`로 **핸들러 코드 수정 없이** 교체했습니다. Docker 연결 실패 시 Mock으로 자동 폴백합니다(개발용).
+- **동시성 제어** — 버퍼드 채널(`chan struct{}`, 크기 2)을 세마포어로 사용. 초과 요청은 큐잉하지 않고 **즉시 429** (온디바이스 CPU 보호 우선).
+- **하드 타임아웃** — `context.WithTimeout(30s)`, 초과 시 `status: "timeout"`.
+- **세션 상태** — `sync.Map`. 실시간 CPU/MEM 갱신(`updateSessionStats`)과 최종 상태 기록 모두 **기존 포인터를 수정하지 않고 복사본을 새로 저장**해 goroutine 간 경쟁 상태를 피합니다.
+- 미들웨어 체인: `withCORS(withCFAccess(handler))`
+
+#### `gateway_client.py` — Python → Go 연결
+`submit_execution()`(202가 아니면 `GatewayError`) → `poll_until_done()`(0.5초 간격, 35초 상한 — 게이트웨이 30초 타임아웃보다 살짝 길게) → `ExecutionResult`.
+
+### ③ 인프라/보안 계층
+
+#### `docker_executor.go` — 샌드박스 매니저
+- **Docker Go SDK를 버리고 `os/exec`로 CLI 호출** — SDK의 `docker/distribution` ↔ `distribution/reference` 버전 충돌(`undefined: reference.SplitHostname`)을 근본적으로 피했습니다. 결과적으로 **표준 라이브러리만으로 빌드**됩니다.
+- **진짜 웜스탠바이** — `sleep infinity` 상태의 idle 컨테이너 1개를 미리 띄워두고, 요청이 오면 `docker exec`로 코드만 주입합니다. 컨테이너 생성 비용이 요청 경로에서 사라집니다.
+- **재사용 금지 원칙 유지** — 한 번이라도 코드를 실행한 컨테이너는 결과와 무관하게 `docker rm -f`로 폐기하고(`destroyAndReplenish`), 백그라운드에서 새 idle 컨테이너를 채웁니다. *재사용은 대기 상태까지만.*
+- **gVisor(runsc)** — 시작 시 `docker info`로 runsc를 자동 감지해 idle 컨테이너 생성 시에만 `--runtime runsc`를 붙입니다. `SANDBOX_RUNTIME=runc|runsc`로 강제 지정 가능(A/B 벤치마크용). 미설치 시 경고 후 runc로 계속 동작합니다.
+- **격리 옵션** (심층 방어)
+
+  | 옵션 | 값 | 목적 |
+  |---|---|---|
+  | `--network` | `none` | 네트워크 완전 차단 |
+  | `--cpuset-cpus` | `2,3` | LLM(0-1)과 코어 분리 |
+  | `--memory` | `512m` | 메모리 폭주 차단 |
+  | `--pids-limit` | `64` | 포크폭탄 방지 |
+  | `--read-only` + `--tmpfs /tmp` | | 루트 FS 변조 방지 |
+  | `--cap-drop ALL` | | 리눅스 권한 전부 제거 |
+  | `--security-opt no-new-privileges` | | setuid 권한 상승 차단 |
+
+- **명령 주입 방지** — 코드는 `python3 -c <code>`의 단일 argv로 전달되어 셸을 거치지 않습니다.
+- **성공 판정 = exit code** — 예전엔 "stderr에 뭐라도 있으면 실패"였는데, matplotlib 캐시 경고 같은 정상 출력까지 실패로 오판했습니다. 지금은 종료 코드로만 판정하고, 성공 시 stderr는 서버 로그에만 남깁니다.
+- **실시간 모니터링** — 실행 중 `docker stats --no-stream`을 500ms 간격으로 폴링해 `StatsCallback`으로 게이트웨이에 보고합니다.
+
+#### `sandbox-python-datasci.Dockerfile`
+`python:3.12-slim` + pandas/matplotlib/numpy(버전 고정) + 나눔고딕. 런타임은 `--network none`이라 `pip install`이 불가능하므로 **빌드 시점에 미리 설치**합니다. `MPLBACKEND=Agg`(디스플레이 없음), `MPLCONFIGDIR=/tmp/...`(읽기 전용 루트 FS 대응).
+
+#### `cgroup_setup.sh`
+cgroup v2로 `llm`(코어 0-1, 4G) / `sandbox`(코어 2-3, 2G) 그룹을 만듭니다. systemd cgroup 드라이버와 `--cgroup-parent`가 충돌해 실제 제한은 컨테이너 레벨 옵션이 담당하므로, **호스트 레벨 참고용**입니다.
+
+### 🌐 네트워크 계층
+
+#### `cf_access_middleware.go` — Cloudflare Access JWT 검증
+엣지(Cloudflare)에서 막는 것만 믿으면 터널을 우회한 직접 접근에 무방비입니다. 그래서 게이트웨이가 `Cf-Access-Jwt-Assertion` 헤더를 직접 검증합니다.
+- JWKS(`/cdn-cgi/access/certs`)를 1시간 캐싱, `kid` 미스 시 자동 재조회(키 로테이션 대응)
+- `alg`를 RS256으로 고정 → RSA PKCS#1 v1.5 서명 검증 → `aud` 일치 → `exp` 만료 확인
+- `CF_ACCESS_TEAM_DOMAIN`/`CF_ACCESS_AUD` 미설정 시 **경고 로그와 함께 로컬 모드**로 동작
+- 인증 성공 시 이메일을 감사 로그로 남김
+
+### ④ 프론트엔드
+
+#### `chat_bridge.py` — 우회 경로 차단
+브라우저가 Ollama나 게이트웨이를 직접 호출하면 파서의 화이트리스트 검증을 건너뛸 수 있습니다. 그래서 **브라우저는 반드시 이 브리지(`POST /chat`)를 거치도록** 했습니다. 응답 상태는 `completed / failed / timeout / no_tool_call / parse_failed`로 구분됩니다.
+
+#### `dashboard.html` — 오프라인 콘솔
+- 좌측 채팅: 결과 안의 `PLOT_BASE64:` 위치를 `indexOf`로 찾아 텍스트(`df.describe()`)와 그래프 이미지를 분리 렌더링
+- 우측 모니터링: 1.5초마다 `/v1/sessions/all` 폴링, 상태별 색상 카드
+- **Canvas 자원 그래프** — Chart.js를 CDN으로 불러오면 오프라인 전제가 깨지므로 `<canvas>`로 직접 그렸습니다 (외부 의존성 0개)
+- `window.location.hostname` 기반 호스트 감지 → 로컬/원격 어디서 열어도 코드 수정 불필요
+
+### 🔬 연구 도구
+
+| 파일 | 설명 |
+|---|---|
+| `benchmark.py llm` | Ollama 순수 추론 지연시간 (웜업 제외 후 평균/표준편차/중앙값) |
+| `benchmark.py pipeline` | LLM → Docker 엔드투엔드 지연시간 |
+| `benchmark.py concurrency` | N개 동시 요청 → 202가 2개를 넘지 않는지 세마포어 검증 |
+| `benchmark.py compare` | 두 CSV(runc vs runsc) A/B 비교, 평균 차이 < 표준편차면 경고 |
+| `test_datasci_direct.py` | LLM을 건너뛰고 검증된 코드를 직접 제출 → 문제가 LLM 쪽인지 인프라 쪽인지 분리 진단 |
+
+---
+
+## 🚀 실행 방법
+
+```bash
+# 0) 사전 준비
+curl -fsSL https://ollama.com/install.sh | sh && ollama pull llama3.2:3b
+pip install requests --break-system-packages
+cd gateway && docker build -t sandbox-python-datasci:latest -f sandbox-python-datasci.Dockerfile .
+# (선택) gVisor 설치: https://gvisor.dev/docs/user_guide/install/
+
+# 1) 터미널 1 — Go 게이트웨이 (:8080)
+cd gateway && go run .
+#   SANDBOX_RUNTIME=runc go run .   ← gVisor 끄고 베이스라인 측정
+
+# 2) 터미널 2 — 채팅 브리지 (:8000)
+cd ai_llm_layer && python3 chat_bridge.py
+
+# 3) 터미널 3 — 대시보드 정적 서버 (:8090)
+cd ai_llm_layer && python3 -m http.server 8090
+#   브라우저: http://localhost:8090/dashboard.html
+```
+
+gVisor 적용 확인:
+```bash
+docker ps                          # sleep infinity 상태의 idle 컨테이너 확인
+docker exec <container_id> dmesg   # "Starting gVisor..." 가 보이면 적용된 것
+```
+
+---
+
+## ⚠️ 알려진 한계 (솔직하게)
+
+- **gVisor 성능 오버헤드 미확정** — `pipeline` 모드로 두 번 A/B 측정했더니 1차 **+6.8%(느림)**, 2차 **-13.9%(빠름)**로 정반대 결론이 나왔습니다. LLM 추론 시간 변동(5.6~12.9초)이 gVisor 오버헤드(수십~수백 ms) 신호를 덮어버린 **측정 방법론의 결함**입니다. LLM을 배제하고 컨테이너 계층만 측정하는 벤치마크 모드가 필요합니다.
+- **한글 그래프 라벨** — 나눔고딕 설치 + `rcParams` 지시에도 완전히 해결되지 않았습니다 (폰트 캐시 생성 시점 불일치 또는 LLM의 설정 누락 추정).
+- **프롬프트 인젝션** — 실행 단계 격리만 다루며, LLM이 위험한 코드를 "생성"하는 것 자체는 막지 못합니다.
+- **코드 정확성** — 파서는 형식만 검증합니다. 문법 오류 코드는 실행 단계에서 `failed`로 걸러집니다.
+- **커널 취약점 탈옥** — 목표는 완전 방지가 아니라 "탈옥해도 피해 최소화"입니다.
+- **네트워크 계층** — Cloudflare 도메인 미확보로 실제 터널 연결 테스트는 보류 상태입니다.
+
+---
+
+## 🔗 참고 자료
+
+- [cgroup v2 공식 문서](https://docs.kernel.org/admin-guide/cgroup-v2.html)
+- [Docker `run` 레퍼런스](https://docs.docker.com/reference/cli/docker/container/run/)
+- [gVisor 설치 가이드](https://gvisor.dev/docs/user_guide/install/)
+- [Ollama API 문서](https://github.com/ollama/ollama/blob/main/docs/api.md)
+- [Cloudflare Access JWT 검증 가이드](https://developers.cloudflare.com/cloudflare-one/identity/authorization-cookie/validating-json/)
+- [JWT 표준 (RFC 7519)](https://datatracker.ietf.org/doc/html/rfc7519)
diff --git a/ai_llm_layer/__pycache__/ai_llm_layer.cpython-310.pyc b/ai_llm_layer/__pycache__/ai_llm_layer.cpython-310.pyc
deleted file mode 100644
index 727277c6055da15b9f69e460fecbb2a7cfcebf85..0000000000000000000000000000000000000000
GIT binary patch
literal 0
HcmV?d00001

literal 8688
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

diff --git a/ai_llm_layer/__pycache__/gateway_client.cpython-310.pyc b/ai_llm_layer/__pycache__/gateway_client.cpython-310.pyc
deleted file mode 100644
index 57b4fea8400457de30e880c826fccc177952f16a..0000000000000000000000000000000000000000
GIT binary patch
literal 0
HcmV?d00001

literal 4766
zcma)9?Qayv8K2qPd-3^f%u67=BpF_kcq?oZgsOyuq*mopBmz-P8s*YxwYiy?Gk3d}
zeF?F9NCqFs;Y=~Y2S<E|?h+HAb=3n5Jyf^-(thcu_UnGCQuPD3bN@h8sns)k2gZ)n
zu5`DvGtay|^Lu{3XN<0{C_)JB>rzMm^$bG)gpcJ9wEhSoZ2b}=L{S`3Or;b~OK}N9
zIgkp(1L7^mWs0es3Z_HxkZ230!s$pnlJ1Civ}~jCC<|0NX{d_hF&d6{(MY_TcEr~l
zL^Mh}bBK1bp2<LbEejn)G)B8}h;~g%lQ_PP?xgGJ`kWkJPj}IcbW=`_Z=f&IXX)mg
z9Djywq+3o%=3m50HpMqlOt+?<O>d5GPH%~CNpFpB#fWZ;ZyVpkwy<rJIQ|^l%ARA-
zvrUy3s%R3^=PpQb!UzpG$ovJ{PM@E|7`=~ZU>LFO^ac20+Zhq>XW$**!M0QXdpq0l
z#9Bsd^VqL3itof|yqj&O+u6=JRF-^;4-gH+cYe4N#`Q}lv5eT-u|F^CvEzUBI4Rbp
zyyW~%LMYm}JI%|B&GU6WcZ-)7N%QogS6<@fMSi8;JYOfhBbv=lYGZ05m1MfzpBYo4
z2mbFT`t79(89X#_`0(4LaWBW~_j|RZno6bBls3jp7>$>Uq;a>_xVPjjEb(%Ve0!-%
zyxJXpb=JFF8fZ?7_08tQs_Ly#qP^@hrrWB?QreiRWs?0PhQdorUbRGcex6rKqEyLw
z`Gsbo*tl2O-Jaye67gztypZF?dGGuz;a6*oyEWqd>XvtTsy`YfgpjA!cwEzIim8sp
zOx0wcI?S?r6=4QX-<xEGhb-sgbkbHeovJ6b<Z|1V{X0j8kCB1TUKwDYvxH+a(tFS#
zE6(A`1g%a7hmH}^$zqn3G<55&Bt6jI-%pws7rmROAFron&hMA_S7%#$CSI-ByjF}x
z`I!Y?$q_z3;XTal7kySwO}vNG-sKYUY8Szm%`0D!UhiJcyO<-*T+RD!-OJ59S>A|2
zOh+fHk3)>kD+NAznwJ;7+l$1@&-3y$@v5c9@9K@(1o27>UVh$R2RvYRcOFp3=Pxzx
z<;dVWhgLd<@O+71zqFt1e|68Rdq}VEL&H4qwvjlYL>~eDDA?`PbltEu8$5MH!EOiD
zY%P(}EK8Bxz@R?nMvi8nRZF?uPaoj;GskT^vwvV9WhAuJal^93R3kpI{o}1iI}t*O
z<unHmZc|+kLWrR_rYJ5^91l=w7%2facF>QbA=5NW=jRBa6(>%b=kD`-g)|l?yvi*C
z=)6%Ry)AgcK}_G;O&WJ<-h&Ck%a^?T{oSp@C(X$Temw`6o|`1z<vYp|MRp^qs%vSc
zs%}(O(*|`?P>!kUr;e6twS-lb8VOasjm$2D{J-z^4;(krY``>5u(1J4)9J^?=Y1K|
z7-b22V0mZ^Jngvt%$ONL2=yX_EEypbK@lu!R(UJX_SO%VdFwqyF$+-X4gVc@15uEL
zAPdltC<jG34CP^@MBFt)B7l;H{w}kel<fvA+jOI?2vL*N4OzBkJJyl$uJ&nvXxZjR
zZpidI1Q{HM5;8kr_aIkhCql?wqpHgQq^jn6Xxh+r(GzEx-B7m%A+#HI4I@NRM2g@T
z?!lto4QI5ml%dh@x@j_D!~DC+pgtxRVLPVo%0~>HB>+I}2iyam{4r>!jS#ZYD7K|J
zL~&WlA}Z0qg}^DC#iIc#pODNgwp<C)U>4EPW>f`j4G*I%9t~B(G(tP7QYAv8Rc!WV
zaiyb*beu$|Bs)qwVT3tEV_B&Zrd?S)+F6z6P+6YDXRy-k{2jIfk-3Vpn#DSQQ0iY9
zoe<Uhn=g32)Iw_SzP+yv?Ax<PX%>pW0sQJhqh8~=65+)ueyuL(_(GB2%z63welgeH
zJ$rix6A6~F8CBYJU0Q<PJA6I@o$_hGo0;+Q^E-gYz0&V_d5L&`n%PYt5^kPuGrl1C
zbC1U#?_9>ckAlD#k}Ho|hVF*#WSSX{{pha<A!ipts6~MM%UOQ+H-uL%@u{LWbBpx0
zKz86oOEK3XgpQ2I5{7Ow-R`r;GR#HeJz6G{N+tvc4Z!dYSv8;^&i;S`sRzab%6{U8
z)6BB85vIi4NK11pSI!ug?Zyrc9vgap@B{UyM~9EOoqjhpVNm9VQpU&#GhI2Ue`2`d
z<4mK>wA{c5v)!m=Yo=|TOxnlIt$<m;mn+-sb6W|R8^wpotc)vvY|yb40C#&@qUN)0
z($Mz-+G7ZzjlrnYgJn#D1e*{8!W$;A;8TzWKZo0p{z^c)f~&}uHlb747Ue8Hfy_g8
zU{tn)KGdm{MM+EpRK5^6C1s`25DlJ?%w0CBgzZQbWAte#D*>c2%1V`J6`cxX0~Hxk
z#8Yxsrr{Hk`TBBeyEBWjvapF%v3Pe>F^yi3hmjqlog&C$<$wr)F6Vhjk%Tnm>ilex
z@X6E7$r7O4baXwbj|eCha$bIhc!lZ4!)bnVqBmirGbv^>I`D~>Offppf|Ww}*GmBZ
z%IS8lQmr+=oNg9M0-gR$*Gg?#_iDF!zRb%Ymir06Ink(3t&BK<?(-m;-Zu+g?ZN<`
zd&tjC^QFs>OEvB<G>f@rp~!De5MDUX%M+wgo8Z@P5HEj=PnDX5l9;twEHo!eyu8?`
z*I*~!LLJmNKi}WZ0eHUDsO4b4je4D5S%l@>sd;CLBH<E^%cQ+o$nh^{n*}iQZZGZz
zZ70pbEeM|*OJd65;i2APW`4%ZzG0@@WC$`kOMx|e7tgjF<ke;yf2_kM8+Rwgj_y^w
z%6zj>A$+Px`1L6-KTrBv&VlXuGxOXXUS9OKcCEn6b#Lae4+^<eK4`nTok2p1(YzwY
z?&Xykely1_rwNR7{RZK4GyLH-#b>MW7_dn@DRZSHHMjVb63S?%mbT1o2%+RGM#x+T
zQ5J3?=ZT~!vL7?h{G`@-7T97r>{_q@PdW#oadC=xXBPR@8N$Ch>wP`Fe2+zb{=S#@
zcd#<k;MZ?(aA3K}^9zd7<;nn1w<B%nhHdD{gxmAZ(Zh$;V~5@zI{Mx*b$IB<CUBSe
zGW_j;KgfaHpp{}Q<E|CPhmIT@diQ68hr!zIF}0+{)K3gk^$E)79MDNYJW5b(*^N-9
z(Nt1r<`4V<ZOvxBBOd1?LSo%D86%Za9o<f*RBGt#CqO_iA%tQbF=-2q;;1Cy2#!cm
z@sqF=kg%x1p?~*un6IuRB+y&XS)s9@>^90GxM$foizwcVCh;&*BnNVrr*1I(>I~?Y
zSDS@9hA)JmZlFQ=TcAMzm+NPFWoAF&l@fnYf?I;0r~3>+nuQYa?oNrLn<_T1e9<rb
zp-4W3!eSLHSb3EnR$MRAkCQDahRxT&ixE{#Ye`*Im4MrMG?miQn#elc_1|-qao5>~
z+Ro!znTj)r5b`fx{K7&jC|>a%Of+*f^2(mp9=r>)t!?u^z*P;T+LucR`}v}R#hx5E
zDeq#gaetxZEWw#q&+#jZtH`WcBHq14@63`P?vO8>dko}$2T<47*Vp%b{dgtE&s7OD
zJI}Z6TGybKiu>10-vr?GAzVxSMEfJ+Y=)Xup(4`okBB1sr?CkT43TMqc{_4J?fhCm
z_-b2GyUPT<gk<~^$97Dns-^@LVNmU)K4R{Hug=F86=s=$Pd8+bWuO@{Y7=Bs#G>0V
z!tBFB?@dTl{ICRBHuplWj-k&Xg-RN_2sl6Vggyhf1W-iM>&lLkq;70^UT?k*CU3ys
z0T?T4Gs{+!dd9Kcn7FvBe*P>r?gn9meSQuR%$P~tb~_<aXc@VfEd#L#Ye<^i$Q%Bp
z<V}b~>j*+fMzQtn+&LmeC0UY#GVTdTau1G%qwAtCM?w*4gCvVk-+&`hHwJt7b>nVn
P8;(qLWB8Z;BR~2tj#lI!

diff --git a/ai_llm_layer/__pycache__/tool_call_parser.cpython-310.pyc b/ai_llm_layer/__pycache__/tool_call_parser.cpython-310.pyc
deleted file mode 100644
index 370eec97c1622c589b312bfb97bb3cf9442fd93a..0000000000000000000000000000000000000000
GIT binary patch
literal 0
HcmV?d00001

literal 8401
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
CvFe2Y

diff --git a/ai_llm_layer/ai_llm_layer.py b/ai_llm_layer/ai_llm_layer.py
index 0ef42d9..2683002 100644
--- a/ai_llm_layer/ai_llm_layer.py
+++ b/ai_llm_layer/ai_llm_layer.py
@@ -8,7 +8,7 @@ AI/LLM 계층 통합 모듈 (ai_llm_layer.py)
 이 파일이 반환하는 결과는 그대로 ② 백엔드/파이프라인 계층(이벤트 중계 API)에
 전달할 수 있는 형태로 정리된다. 즉 이 모듈의 산출물이 다음 계층의 입력 규격이다.
 
-사전 준비 (지후님 로컬 PC에서):
+사전 준비 (로컬 PC에서):
     curl -fsSL https://ollama.com/install.sh | sh
     ollama pull llama3.2:3b
     ollama serve   # 보통 설치 시 자동으로 백그라운드 서비스 등록됨
@@ -87,10 +87,37 @@ SYSTEM_PROMPT = """당신은 코드 실행을 도와주는 에이전트입니다
 {"tool": "run_code", "args": {"language": "python", "code": "numbers = [3, 1, 4, 1, 5]\\nprint(sorted(numbers))"}}
 ```
 
+예시 3) 데이터 분석/시각화 요청 - 반드시 아래 패턴을 그대로 따르세요
+사용자: "월별 매출 데이터를 분석해서 막대그래프로 시각화해줘"
+답변:
+```json
+{"tool": "run_code", "args": {"language": "python", "code": "import pandas as pd\\nimport matplotlib\\nmatplotlib.use('Agg')\\nimport matplotlib.pyplot as plt\\nplt.rcParams['font.family'] = 'NanumGothic'\\nimport io, base64\\n\\ndata = {'month': ['1월','2월','3월','4월'], 'sales': [120, 135, 98, 150]}\\ndf = pd.DataFrame(data)\\nprint(df.describe())\\n\\nx_pos = range(len(df))\\nfig, ax = plt.subplots()\\nax.bar(x_pos, df['sales'])\\nax.set_xticks(list(x_pos))\\nax.set_xticklabels(df['month'])\\nax.set_title('Monthly Sales')\\n\\nbuf = io.BytesIO()\\nfig.savefig(buf, format='png')\\nbuf.seek(0)\\nprint('PLOT_BASE64:' + base64.b64encode(buf.read()).decode())"}}
+```
+
 중요 규칙:
 - code 필드에는 실행 가능한 완전한 코드만 작성하세요. 설명, 주석, 디버깅 메모,
   "debugger" 같은 관계없는 단어를 코드 중간에 절대 섞지 마세요.
 - 코드는 예시처럼 문법적으로 완전해야 하며, 마지막에 결과를 print()로 출력해야 합니다.
+- **code 안의 모든 파이썬 문자열은 반드시 작은따옴표(')만 사용하세요. 큰따옴표(")는 절대
+  쓰지 마세요.** code 자체가 이미 큰따옴표로 감싸인 JSON 문자열이라서, 안에서 또
+  큰따옴표를 쓰면 JSON 형식이 깨져 요청 전체가 실행되지 못합니다.
+  예: df = pd.DataFrame({'a': 1}) (O)  /  df = pd.DataFrame({"a": 1}) (X, 형식 깨짐)
+- **한 줄에는 문장을 하나만 쓰세요.** "def f(): a; b; c" 처럼 콜론 뒤에 여러 문장을
+  나열하지 마세요 - 파이썬 문법 오류입니다. 각 문장은 반드시 \\n으로 구분하세요.
+- **존재하지 않는 파일을 읽으려 하지 마세요** (pd.read_csv("파일명") 등 금지). 필요한
+  데이터는 예시 3처럼 코드 안에 딕셔너리/리스트로 직접 정의하세요. 이 샌드박스는
+  외부 파일이나 네트워크에 접근할 수 없습니다.
+- 그래프/차트/시각화가 필요한 요청은 반드시 예시 3의 패턴을 따르세요:
+  matplotlib.use('Agg')를 pyplot import 전에 반드시 호출하고, 그래프를 파일로 저장하지 말고
+  io.BytesIO()에 담아 base64로 인코딩한 뒤 "PLOT_BASE64:" 접두사와 함께 출력하세요.
+  (이 컨테이너는 디스플레이도 없고 파일시스템도 읽기 전용이라, 이 방식만 동작합니다.)
+- **막대/선 그래프의 x축이 카테고리(월, 이름, 항목 등)라면 반드시 예시 3처럼
+  ax.set_xticks(...)와 ax.set_xticklabels(...)를 명시적으로 호출해서 실제 카테고리
+  이름을 라벨로 지정하세요.** ax.bar()에 카테고리 값을 바로 넘기는 것만으로는
+  x축이 0, 1, 2... 같은 숫자 인덱스로 나올 수 있습니다.
+- 그래프에 한글 라벨/제목이 들어가면 반드시
+  plt.rcParams['font.family'] = 'NanumGothic' 을 pyplot import 직후에 추가하세요.
+  (기본 폰트는 한글을 지원하지 않아 글자가 깨집니다.)
 - 도구 호출이 필요 없는 단순 질문이면 그냥 일반 텍스트로 답하세요.
 - 한 번에 하나의 도구만 호출하세요."""
 
@@ -236,6 +263,20 @@ def to_gateway_payload(result: LayerResult, session_id: str) -> Optional[dict[st
     }
 
 
+def looks_like_failed_tool_call(raw_output: str) -> bool:
+    """
+    tool_calls가 0건이더라도, LLM이 애초에 도구 호출을 "시도"는 했는데 형식이
+    깨져서(예: code 안에 이스케이프 안 된 큰따옴표) 파싱에 실패한 경우와,
+    처음부터 도구 호출 의도가 없던 일반 대화 응답을 구분하기 위한 휴리스틱이다.
+
+    포인트: 이 구분이 필요한 이유는 UX 때문이다. 후자는 LLM의 정상적인 답변이니
+    그대로 보여주면 되지만, 전자를 그대로 보여주면 사용자에게 깨진 JSON 잔해가
+    그대로 노출되어 "시스템이 고장났다"는 오해를 준다. chat_bridge.py가 이 함수의
+    결과에 따라 다른 메시지를 보여주도록 분기한다.
+    """
+    return '"tool"' in raw_output or "```json" in raw_output
+
+
 if __name__ == "__main__":
     # 동작 확인용 간단 실행. 실제로는 Ollama가 로컬에서 떠 있어야 성공한다.
     import uuid
diff --git a/ai_llm_layer/benchmark.py b/ai_llm_layer/benchmark.py
index 23c773f..1ea9d91 100644
--- a/ai_llm_layer/benchmark.py
+++ b/ai_llm_layer/benchmark.py
@@ -3,18 +3,20 @@ benchmark.py
 ============
 "자원 최적화 및 Docker 샌드박스 보안 인프라"라는 연구 주제를 뒷받침하려면,
 "구현했다"가 아니라 "얼마나 효과적인지 측정했다"가 필요하다. 이 스크립트는
-그 정량적 근거를 만들기 위한 세 가지 측정 모드를 제공한다.
+그 정량적 근거를 만들기 위한 측정 모드를 제공한다.
 
 모드:
   llm         - Ollama 순수 추론 지연시간만 측정 (콜드/웜 스타트 차이 관찰용)
   pipeline    - 전체 파이프라인(LLM->파서->게이트웨이->Docker) 엔드투엔드 지연시간
   concurrency - Go 게이트웨이의 동시 세션 제한(세마포어)이 실제로 작동하는지 부하 테스트
+  compare     - 두 CSV(예: runc vs runsc) 결과를 A/B 비교
 
 사용 예:
   python3 benchmark.py llm --runs 10
   python3 benchmark.py pipeline --runs 5 --warmup 1
   python3 benchmark.py concurrency --concurrent 5
   python3 benchmark.py llm --runs 10 --csv results_llm.csv
+  python3 benchmark.py compare --before runc.csv --after runsc.csv
 
 사전 준비: ai_llm_layer.py, tool_call_parser.py, gateway_client.py와 같은 폴더에 둘 것.
 Ollama와 Go 게이트웨이가 모두 실행 중이어야 한다 (pipeline/concurrency 모드).
@@ -29,7 +31,6 @@ import time
 import uuid
 from concurrent.futures import ThreadPoolExecutor, as_completed
 from dataclasses import asdict, dataclass
-from typing import Optional
 
 import requests
 
@@ -83,9 +84,8 @@ def _write_csv(path: str, results: list[RunResult]) -> None:
 def bench_llm(prompt: str, runs: int, warmup: int) -> list[RunResult]:
     """
     Ollama 추론만 반복 측정한다 (Tool Call 파서/게이트웨이는 거치지 않음).
-    warmup 횟수만큼은 결과 집계에서 제외한다 - 이전 대화에서 확인했듯,
-    Ollama는 일정 시간 요청이 없으면 모델을 메모리에서 내리므로, 첫 요청은
-    "모델 재로딩" 비용이 섞여 순수 추론 성능을 왜곡한다.
+    warmup 횟수만큼은 결과 집계에서 제외한다 - Ollama는 일정 시간 요청이 없으면
+    모델을 메모리에서 내리므로, 첫 요청은 "모델 재로딩" 비용이 섞여 순수 추론 성능을 왜곡한다.
     """
     print(f"[LLM 벤치마크] 웜업 {warmup}회 + 측정 {runs}회, 프롬프트: {prompt!r}")
 
@@ -119,10 +119,11 @@ def bench_llm(prompt: str, runs: int, warmup: int) -> list[RunResult]:
 def bench_pipeline(prompt: str, runs: int, warmup: int) -> list[RunResult]:
     """
     LLM -> Tool Call 파서 -> Go 게이트웨이 -> Docker 실행까지 전체를 반복 측정한다.
-    포인트: 여기서 측정되는 시간은 LLM 추론 시간 + 컨테이너 생성/실행/폐기 시간의 합이다.
+    포인트: 여기서 측정되는 시간은 LLM 추론 시간 + 컨테이너 실행/폐기 시간의 합이다.
     LLM 지연시간(bench_llm 결과)과 비교하면, "인프라 계층이 전체 지연시간에서
-    차지하는 비중"을 역산할 수 있다 - 이게 곧 "자원 최적화 여지가 어디 있는지"를
-    가리키는 데이터가 된다.
+    차지하는 비중"을 역산할 수 있다.
+    주의: LLM 추론 변동성(수 초)이 gVisor 오버헤드(수십~수백 ms)를 덮어버리므로,
+    이 모드로 runc/runsc A/B 비교를 하면 결론이 뒤집힐 수 있다 (README 한계 참고).
     """
     print(f"[파이프라인 벤치마크] 웜업 {warmup}회 + 측정 {runs}회, 프롬프트: {prompt!r}")
 
@@ -170,14 +171,11 @@ def bench_concurrency(concurrent_n: int) -> list[RunResult]:
     포인트: LLM을 거치면 Ollama가 요청을 사실상 순차 처리해버려서 "동시에 몰아치는"
     상황을 재현하기 어렵다. 그래서 이 모드는 LLM을 건너뛰고, 게이트웨이에
     직접 동일한 run_code 요청 N개를 스레드로 동시에 쏴서 세마포어 동작만 순수하게 검증한다.
-    기대 결과: concurrent_n개 중 최대 2개만 202(running)를 받고, 나머지는 429(rejected)여야 한다 -
-    이 숫자가 정확히 맞아떨어지는지가 "동시성 제한이 설계대로 작동한다"는 직접적인 증거다.
+    기대 결과: concurrent_n개 중 최대 2개만 202(running)를 받고, 나머지는 429(rejected)여야 한다.
     """
     print(f"[동시성 벤치마크] {concurrent_n}개 요청을 동시에 게이트웨이로 전송")
 
     # 약간의 지연이 있는 코드를 써서, 세션들이 겹치는 창(window)을 인위적으로 넓힌다.
-    # 이게 없으면 실행이 너무 빨리 끝나 세마포어가 반납되고 다음 요청이 통과해버려
-    # "동시 실행 중" 상태를 관측하기 어렵다.
     sample_code = "import time\ntime.sleep(2)\nprint('done')"
 
     def _fire(i: int) -> RunResult:
@@ -198,8 +196,6 @@ def bench_concurrency(concurrent_n: int) -> list[RunResult]:
             return RunResult(i, elapsed, "error", str(e))
 
     results: list[RunResult] = []
-    # ThreadPoolExecutor로 전부 "동시에" 제출한다 (max_workers=concurrent_n으로
-    # 모든 요청이 대기 없이 즉시 동시 실행되도록 보장).
     with ThreadPoolExecutor(max_workers=concurrent_n) as pool:
         futures = [pool.submit(_fire, i) for i in range(concurrent_n)]
         for future in as_completed(futures):
@@ -214,27 +210,84 @@ def bench_concurrency(concurrent_n: int) -> list[RunResult]:
     for r in results:
         print(f"  요청 {r.run_index}: {r.status} ({r.detail})")
     print(f"\n  accepted={accepted}, rejected={rejected}, error={errored} (총 {concurrent_n}개 중)")
-    print(f"  기대값: accepted는 게이트웨이의 MaxConcurrentSessions(기본 2)를 넘지 않아야 함")
+    print("  기대값: accepted는 게이트웨이의 MaxConcurrentSessions(기본 2)를 넘지 않아야 함")
 
     return results
 
 
+# -----------------------------------------------------------------------
+# 모드 4: 두 벤치마크 결과(CSV) 비교 - gVisor 적용 전/후처럼 "조건 하나만 바꾼" A/B 비교용
+# -----------------------------------------------------------------------
+def bench_compare(before_csv: str, after_csv: str, before_label: str, after_label: str) -> None:
+    """
+    같은 방식(llm 또는 pipeline)으로 저장된 두 CSV를 읽어 평균/표준편차를 비교한다.
+
+    포인트: 단순히 평균값만 나열하면 "우연히 그런 것 아니냐"는 반박에 취약하다.
+    표준편차와 변화율(%)을 함께 보여주면, 그 차이가 노이즈 수준인지
+    실제로 유의미한 차이인지 판단할 근거가 생긴다.
+    """
+    def _load(path: str) -> list[float]:
+        with open(path, newline="", encoding="utf-8") as f:
+            reader = csv.DictReader(f)
+            return [float(row["elapsed_sec"]) for row in reader if row["status"] == "ok"]
+
+    before = _load(before_csv)
+    after = _load(after_csv)
+
+    if not before or not after:
+        print("비교할 데이터가 부족합니다 (성공한 측정치가 있는 CSV인지 확인하세요).")
+        return
+
+    b_mean, a_mean = statistics.mean(before), statistics.mean(after)
+    b_std = statistics.stdev(before) if len(before) > 1 else 0.0
+    a_std = statistics.stdev(after) if len(after) > 1 else 0.0
+    diff_pct = ((a_mean - b_mean) / b_mean) * 100 if b_mean else 0.0
+
+    print(f"\n=== A/B 비교: {before_label} vs {after_label} ===")
+    print(f"{'항목':<12}{before_label:>15}{after_label:>15}")
+    print(f"{'표본 수':<12}{len(before):>15}{len(after):>15}")
+    print(f"{'평균(초)':<12}{b_mean:>15.2f}{a_mean:>15.2f}")
+    print(f"{'표준편차':<12}{b_std:>15.2f}{a_std:>15.2f}")
+    print(f"{'최소':<12}{min(before):>15.2f}{min(after):>15.2f}")
+    print(f"{'최대':<12}{max(before):>15.2f}{max(after):>15.2f}")
+    print(f"\n변화율: {diff_pct:+.1f}% ({after_label}이 {before_label} 대비)")
+
+    if diff_pct > 0:
+        print(f"-> {after_label}이 평균적으로 {abs(diff_pct):.1f}% 더 느립니다.")
+    else:
+        print(f"-> {after_label}이 평균적으로 {abs(diff_pct):.1f}% 더 빠릅니다.")
+
+    # 표준편차가 두 평균의 차이보다 크면, 그 차이가 노이즈에 묻힐 수 있다는 뜻이므로 경고한다.
+    if abs(a_mean - b_mean) < max(b_std, a_std):
+        print("주의: 평균 차이가 표준편차보다 작습니다 - 표본을 늘려(--runs 증가) 재측정을 권장합니다.")
+
+
 def main() -> None:
     parser = argparse.ArgumentParser(description="샌드박스 아키텍처 성능 벤치마크")
-    parser.add_argument("mode", choices=["llm", "pipeline", "concurrency"])
+    parser.add_argument("mode", choices=["llm", "pipeline", "concurrency", "compare"])
     parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="llm/pipeline 모드에서 사용할 프롬프트")
     parser.add_argument("--runs", type=int, default=5, help="llm/pipeline 모드 반복 횟수")
     parser.add_argument("--warmup", type=int, default=1, help="집계에서 제외할 웜업 횟수")
     parser.add_argument("--concurrent", type=int, default=5, help="concurrency 모드에서 동시에 쏠 요청 수")
     parser.add_argument("--csv", default=None, help="결과를 저장할 CSV 파일 경로 (선택)")
+    parser.add_argument("--before", default=None, help="compare 모드: 이전(예: runc) 결과 CSV 경로")
+    parser.add_argument("--after", default=None, help="compare 모드: 이후(예: runsc) 결과 CSV 경로")
+    parser.add_argument("--before-label", default="runc", help="compare 모드: 이전 조건 라벨")
+    parser.add_argument("--after-label", default="runsc(gVisor)", help="compare 모드: 이후 조건 라벨")
     args = parser.parse_args()
 
     if args.mode == "llm":
         results = bench_llm(args.prompt, args.runs, args.warmup)
     elif args.mode == "pipeline":
         results = bench_pipeline(args.prompt, args.runs, args.warmup)
-    else:
+    elif args.mode == "concurrency":
         results = bench_concurrency(args.concurrent)
+    else:
+        if not args.before or not args.after:
+            print("compare 모드는 --before와 --after CSV 경로가 모두 필요합니다.")
+            return
+        bench_compare(args.before, args.after, args.before_label, args.after_label)
+        return
 
     if args.csv:
         _write_csv(args.csv, results)
diff --git a/ai_llm_layer/chat_bridge.py b/ai_llm_layer/chat_bridge.py
index f2c1a6a..fd6f691 100644
--- a/ai_llm_layer/chat_bridge.py
+++ b/ai_llm_layer/chat_bridge.py
@@ -25,7 +25,7 @@ import logging
 import uuid
 from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
 
-from ai_llm_layer import OllamaError, handle_user_request, to_gateway_payload
+from ai_llm_layer import OllamaError, handle_user_request, looks_like_failed_tool_call, to_gateway_payload
 from gateway_client import GatewayError, submit_and_wait
 
 logger = logging.getLogger("chat_bridge")
@@ -76,8 +76,22 @@ class ChatBridgeHandler(BaseHTTPRequestHandler):
 
         payload = to_gateway_payload(layer_result, session_id=session_id)
         if payload is None:
-            # 도구 호출이 없었던 경우 - LLM의 일반 텍스트 답변을 그대로 보여준다.
-            self._send_json(200, {"status": "no_tool_call", "raw_output": layer_result.raw_llm_output})
+            if looks_like_failed_tool_call(layer_result.raw_llm_output):
+                # 도구 호출을 시도는 했는데 형식이 깨져 파서가 거부한 경우.
+                # 깨진 JSON 잔해를 그대로 보여주지 않고, 무엇이 문제였는지 원인을
+                # 안내한 뒤 재시도를 유도한다 - 사용자가 "시스템 고장"으로 오해하지 않도록.
+                logger.warning("Tool Call 파싱 실패로 보이는 응답 (session=%s)", session_id)
+                self._send_json(200, {
+                    "status": "parse_failed",
+                    "raw_output": (
+                        "요청을 실행 가능한 형식으로 만드는 데 실패했어요 "
+                        "(모델이 생성한 코드에 문법 오류가 있었을 가능성이 높아요). "
+                        "같은 요청을 다시 보내보시거나, 조금 더 간단하게 표현해보세요."
+                    ),
+                })
+            else:
+                # 애초에 도구 호출 의도가 없던 일반 대화 응답 - LLM의 답변을 그대로 보여준다.
+                self._send_json(200, {"status": "no_tool_call", "raw_output": layer_result.raw_llm_output})
             return
 
         try:
@@ -99,6 +113,11 @@ class ChatBridgeHandler(BaseHTTPRequestHandler):
 
 
 if __name__ == "__main__":
-    server = ThreadingHTTPServer(("localhost", PORT), ChatBridgeHandler)
-    logger.info("chat_bridge 시작: http://localhost:%d (POST /chat)", PORT)
+    # 포인트: "localhost"가 아니라 "0.0.0.0"에 바인딩해야 한다. "localhost"로 바인딩하면
+    # 이 프로세스가 실행 중인 기기 자신만 접속 가능해서, 원격(터널/사설망)으로 연결된 팀원의
+    # 기기에서는 이 브리지에 아예 도달할 수조차 없다. "0.0.0.0"은 모든 네트워크
+    # 인터페이스에서 들어오는 연결을 받는다.
+    # 주의: 인바운드 포트를 공유기/방화벽에서 개방하지 않았다는 전제에서만 안전하다.
+    server = ThreadingHTTPServer(("0.0.0.0", PORT), ChatBridgeHandler)
+    logger.info("chat_bridge 시작: 0.0.0.0:%d (POST /chat)", PORT)
     server.serve_forever()
diff --git a/ai_llm_layer/dashboard.html b/ai_llm_layer/dashboard.html
index 5f15897..96ac95a 100644
--- a/ai_llm_layer/dashboard.html
+++ b/ai_llm_layer/dashboard.html
@@ -188,6 +188,27 @@
   }
   .resource-bar b { color: var(--text); }
 
+  .chart-panel {
+    margin: 0 20px 12px;
+    padding: 10px 12px;
+    background: var(--panel);
+    border: 1px solid var(--border);
+    border-radius: 8px;
+  }
+  .chart-panel .chart-head {
+    display: flex;
+    justify-content: space-between;
+    align-items: center;
+    margin-bottom: 6px;
+    font-family: var(--mono);
+    font-size: 10.5px;
+    color: var(--text-dim);
+  }
+  .chart-panel .legend { display: flex; gap: 12px; }
+  .chart-panel .legend span { display: flex; align-items: center; gap: 4px; }
+  .chart-panel .legend .swatch { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
+  .chart-panel canvas { display: block; width: 100%; height: 130px; }
+
   .session-list {
     flex: 1;
     overflow-y: auto;
@@ -211,6 +232,15 @@
   }
   .session-card .sid { color: var(--text-dim); }
   .session-card .tool { color: var(--accent); font-weight: 600; margin-top: 4px; font-size: 12px; }
+  .session-card .stats {
+    margin-top: 6px;
+    display: flex;
+    gap: 12px;
+    font-family: var(--mono);
+    font-size: 10.5px;
+  }
+  .session-card .stats .stat-label { color: var(--text-dim); }
+  .session-card .stats .stat-value { color: var(--accent); font-weight: 600; }
   .session-card .detail {
     margin-top: 6px;
     font-family: var(--mono);
@@ -257,6 +287,17 @@
       <span>CPU 코어: <b>0-1 LLM / 2-3 샌드박스</b></span>
       <span>동시 세션 상한: <b>2</b></span>
       <span>하드 타임아웃: <b>30초</b></span>
+      <span>웜스탠바이 풀: <b>1개</b></span>
+    </div>
+    <div class="chart-panel">
+      <div class="chart-head">
+        <span id="chart-session-label">실행 중인 세션 없음</span>
+        <div class="legend">
+          <span><i class="swatch" style="background:#4ec9b0"></i>CPU %</span>
+          <span><i class="swatch" style="background:#d29922"></i>MEM %</span>
+        </div>
+      </div>
+      <canvas id="resource-chart"></canvas>
     </div>
     <div class="session-list" id="session-list">
       <div class="empty-hint">아직 실행된 세션이 없습니다.</div>
@@ -269,21 +310,24 @@
 // 샌드박스 콘솔 프론트엔드
 // =====================================================================
 // 이 스크립트가 하는 일은 딱 두 가지다:
-//   1) 채팅 입력 -> ai_llm_layer.py가 하던 역할(POST /v1/execute) -> 폴링 -> 결과 표시
-//   2) 1초마다 GET /v1/sessions/all을 불러와 우측 모니터링 패널을 갱신
+//   1) 채팅 입력 -> chat_bridge.py(POST /chat) -> 결과 표시
+//   2) 1.5초마다 GET /v1/sessions/all을 불러와 우측 모니터링 패널을 갱신
 //
-// 포인트: 이 프론트엔드는 LLM 호출 자체는 하지 않는다. LLM 호출(Ollama)은
-// 여전히 Python(ai_llm_layer.py)의 책임이다. 하지만 데모/테스트 편의를 위해,
-// 이 파일은 간단한 요청은 게이트웨이가 아니라 별도의 작은 로컬 프록시를 통해
-// LLM까지 왕복하도록 구성한다 (아래 CHAT_ENDPOINT 참고).
-const GATEWAY = "http://localhost:8080";
+// 포인트: "localhost"로 하드코딩하지 않고 window.location.hostname을 그대로 쓴다.
+// 브라우저의 "localhost"는 항상 "이 페이지를 보고 있는 기기 자기 자신"을 가리키므로,
+// 원격 기기에서 호스트 노트북 IP로 이 페이지에 접속하면 "원격 기기 자신의 8080"을
+// 찾으려다 실패한다. window.location.hostname은 "이 HTML 파일을 어떤 주소로
+// 요청해서 받았는지"를 그대로 반영하므로, 로컬에서 열든 원격 IP로 열든 항상
+// 올바른 대상을 자동으로 가리키게 된다.
+const HOST = window.location.hostname;
+const GATEWAY = `http://${HOST}:8080`;
 
 // 포인트: 브라우저에서 직접 Ollama(:11434)를 호출하는 것도 기술적으로는 가능하지만,
 // 그러면 Tool Call 파서(tool_call_parser.py)의 검증을 건너뛰고 프론트엔드가
 // 화이트리스트 없이 임의 응답을 게이트웨이로 보내는 셈이 되어 보안 원칙이 깨진다.
-// 그래서 이 대시보드는 항상 게이트웨이 앞단에 서는 얇은 Python 브리지
-// (chat_bridge.py, 아래 안내 참고)를 통해서만 LLM 파이프라인을 태운다.
-const CHAT_ENDPOINT = "http://localhost:8000/chat";
+// 그래서 이 대시보드는 항상 얇은 Python 브리지(chat_bridge.py)를 통해서만
+// LLM 파이프라인을 태운다.
+const CHAT_ENDPOINT = `http://${HOST}:8000/chat`;
 
 const chatLog = document.getElementById("chat-log");
 const chatInput = document.getElementById("chat-text");
@@ -317,10 +361,35 @@ function appendAgentMessage({ statusText, statusClass, resultText, errorText, se
   div.appendChild(bubble);
 
   if (resultText) {
-    const code = document.createElement("div");
-    code.className = "code";
-    code.textContent = resultText;
-    div.appendChild(code);
+    // 포인트: run_code의 stdout 안에 "PLOT_BASE64:<데이터>"가 포함되어 있으면, 그 뒷부분은
+    // matplotlib이 만든 그래프를 base64로 인코딩한 것이다 (데이터 분석/시각화 시나리오,
+    // ai_llm_layer.py의 시스템 프롬프트 예시 3 참고). df.describe() 같은 텍스트 출력이
+    // PLOT_BASE64보다 먼저 나올 수 있으므로, startsWith(맨 앞부터 일치)가 아니라
+    // indexOf(문자열 어디에 있든 찾기)로 판단해야 한다.
+    const PLOT_PREFIX = "PLOT_BASE64:";
+    const plotIdx = resultText.indexOf(PLOT_PREFIX);
+
+    if (plotIdx !== -1) {
+      const textPart = resultText.slice(0, plotIdx).trim();
+      const b64Part = resultText.slice(plotIdx + PLOT_PREFIX.length).trim();
+
+      if (textPart) {
+        const code = document.createElement("div");
+        code.className = "code";
+        code.textContent = textPart;
+        div.appendChild(code);
+      }
+
+      const img = document.createElement("img");
+      img.src = "data:image/png;base64," + b64Part;
+      img.style.cssText = "max-width:100%;border-radius:6px;margin-top:8px;border:1px solid var(--border);";
+      div.appendChild(img);
+    } else {
+      const code = document.createElement("div");
+      code.className = "code";
+      code.textContent = resultText;
+      div.appendChild(code);
+    }
   }
   if (errorText) {
     const code = document.createElement("div");
@@ -366,6 +435,10 @@ async function sendChat() {
 
     if (data.status === "no_tool_call") {
       appendAgentMessage({ statusText: data.raw_output || "(도구 호출 없음)", statusClass: "completed" });
+    } else if (data.status === "parse_failed") {
+      // 포인트: chat_bridge.py가 "형식이 깨진 도구 호출 시도"를 감지해 친절한 안내 문구로
+      // 바꿔 보내주는 경우다. status-pill 클래스는 CSS에 정의된 failed 스타일을 그대로 쓴다.
+      appendAgentMessage({ statusText: data.raw_output, statusClass: "failed" });
     } else {
       appendAgentMessage({
         statusText: `실행 완료 (${data.status})`,
@@ -378,7 +451,7 @@ async function sendChat() {
   } catch (e) {
     pendingNode.remove();
     appendAgentMessage({
-      statusText: "chat_bridge.py에 연결할 수 없습니다. 아래 안내를 참고해 브리지를 먼저 실행해주세요.",
+      statusText: "chat_bridge.py에 연결할 수 없습니다. 브리지를 먼저 실행해주세요 (python3 chat_bridge.py).",
       statusClass: "failed",
     });
   } finally {
@@ -402,12 +475,24 @@ function renderSessions(sessions) {
     const card = document.createElement("div");
     card.className = "session-card";
     const started = new Date(s.started_at).toLocaleTimeString("ko-KR");
+
+    // 포인트: cpu_usage/mem_usage는 DockerExecutor가 실행 도중에만 채워 넣는 값이라
+    // (진짜 웜스탠바이 컨테이너를 docker stats로 실측), running 상태일 때만 의미가 있다.
+    // completed/failed 이후에는 마지막으로 관측된 값이 남아있을 뿐이므로 참고용으로만 표시한다.
+    const statsHtml = (s.cpu_usage || s.mem_usage)
+      ? `<div class="stats">
+           <span><span class="stat-label">CPU </span><span class="stat-value">${s.cpu_usage || "-"}</span></span>
+           <span><span class="stat-label">MEM </span><span class="stat-value">${s.mem_usage || "-"}</span></span>
+         </div>`
+      : "";
+
     card.innerHTML = `
       <div class="row1">
         <span class="sid">${s.id.slice(0, 8)}...</span>
         <span class="status-pill ${s.status}">${s.status}</span>
       </div>
       <div class="tool">${s.tool || "-"}</div>
+      ${statsHtml}
       <div class="detail">시작: ${started}${s.result ? "\n결과: " + s.result.slice(0, 120) : ""}${s.error ? "\n오류: " + s.error.slice(0, 120) : ""}</div>
     `;
     sessionList.appendChild(card);
@@ -418,13 +503,144 @@ async function pollSessions() {
   try {
     const resp = await fetch(`${GATEWAY}/v1/sessions/all`);
     const data = await resp.json();
-    renderSessions(data || []);
+    const sessions = data || [];
+    renderSessions(sessions);
+    updateChart(sessions);
     gwStatus.textContent = "연결됨";
   } catch (e) {
     gwStatus.textContent = "연결 안 됨";
   }
 }
 
+// ---------------- 실시간 CPU/메모리 그래프 ----------------
+// 포인트: 외부 차트 라이브러리(Chart.js 등)를 CDN으로 불러오면 이 대시보드 자체가
+// 인터넷 연결에 의존하게 된다. 이 프로젝트는 "완전 오프라인 온디바이스"가 핵심 전제라,
+// 대시보드도 예외를 두지 않고 순수 <canvas> API로 직접 그린다 - 새 의존성 0개.
+
+const MAX_POINTS = 40; // 폴링 주기 1.5초 기준 약 60초 분량의 히스토리
+const history = new Map(); // sessionId -> { cpu: number[], mem: number[] }
+
+function parseCpuPct(text) {
+  // "3.42%" -> 3.42
+  if (!text) return null;
+  const m = text.match(/([\d.]+)\s*%/);
+  return m ? parseFloat(m[1]) : null;
+}
+
+function parseMemPct(text) {
+  // "12.3MiB / 512MiB" -> 사용량을 상한 대비 백분율로 환산 (2.4)
+  if (!text) return null;
+  const m = text.match(/([\d.]+)\s*(\w+)\s*\/\s*([\d.]+)\s*(\w+)/);
+  if (!m) return null;
+  const toMiB = (value, unit) => {
+    unit = unit.toLowerCase();
+    if (unit.startsWith("kib") || unit.startsWith("kb")) return value / 1024;
+    if (unit.startsWith("gib") || unit.startsWith("gb")) return value * 1024;
+    return value; // MiB/MB는 그대로
+  };
+  const used = toMiB(parseFloat(m[1]), m[2]);
+  const limit = toMiB(parseFloat(m[3]), m[4]);
+  if (!limit) return null;
+  return (used / limit) * 100;
+}
+
+function recordStats(session) {
+  // running 상태일 때만 기록한다 - 완료/실패 이후의 값은 마지막 관측치일 뿐이라
+  // 그래프에 계속 이어붙이면 실제로는 멈춘 실행이 계속되는 것처럼 오해를 줄 수 있다.
+  if (session.status !== "running") return;
+  const cpu = parseCpuPct(session.cpu_usage);
+  const mem = parseMemPct(session.mem_usage);
+  if (cpu === null && mem === null) return;
+
+  let h = history.get(session.id);
+  if (!h) {
+    h = { cpu: [], mem: [] };
+    history.set(session.id, h);
+  }
+  h.cpu.push(cpu ?? 0);
+  h.mem.push(mem ?? 0);
+  if (h.cpu.length > MAX_POINTS) h.cpu.shift();
+  if (h.mem.length > MAX_POINTS) h.mem.shift();
+}
+
+function pickActiveSessionId(sessions) {
+  // 우선순위: 지금 running 중인 세션 > 가장 최근에 기록이 남은 세션(완료 직후 잔상 표시용)
+  const running = sessions.find((s) => s.status === "running" && history.has(s.id));
+  if (running) return running.id;
+  let latestId = null;
+  let latestTime = -Infinity;
+  for (const s of sessions) {
+    if (history.has(s.id)) {
+      const t = new Date(s.started_at).getTime();
+      if (t > latestTime) { latestTime = t; latestId = s.id; }
+    }
+  }
+  return latestId;
+}
+
+function drawChart(sessionId) {
+  const canvas = document.getElementById("resource-chart");
+  const label = document.getElementById("chart-session-label");
+  const dpr = window.devicePixelRatio || 1;
+  const cssWidth = canvas.clientWidth || 300;
+  const cssHeight = 130;
+  canvas.width = cssWidth * dpr;
+  canvas.height = cssHeight * dpr;
+  const ctx = canvas.getContext("2d");
+  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
+  ctx.clearRect(0, 0, cssWidth, cssHeight);
+
+  const h = sessionId ? history.get(sessionId) : null;
+  label.textContent = sessionId ? `세션 ${sessionId.slice(0, 8)}... 실시간 추이` : "실행 중인 세션 없음";
+
+  const padL = 30, padR = 6, padT = 8, padB = 16;
+  const plotW = cssWidth - padL - padR;
+  const plotH = cssHeight - padT - padB;
+
+  // 격자선 + y축 라벨 (0/25/50/75/100%)
+  ctx.strokeStyle = "rgba(255,255,255,0.08)";
+  ctx.fillStyle = "#6b7a89";
+  ctx.font = "9px monospace";
+  ctx.lineWidth = 1;
+  for (let pct = 0; pct <= 100; pct += 25) {
+    const y = padT + plotH * (1 - pct / 100);
+    ctx.beginPath();
+    ctx.moveTo(padL, y);
+    ctx.lineTo(padL + plotW, y);
+    ctx.stroke();
+    ctx.fillText(`${pct}`, 2, y + 3);
+  }
+
+  if (!h || h.cpu.length < 2) {
+    ctx.fillStyle = "#6b7a89";
+    ctx.font = "11px sans-serif";
+    ctx.fillText("데이터 수집 중... (요청을 보내면 그래프가 그려집니다)", padL + 8, padT + plotH / 2);
+    return;
+  }
+
+  const drawLine = (series, color) => {
+    ctx.strokeStyle = color;
+    ctx.lineWidth = 1.5;
+    ctx.beginPath();
+    const n = series.length;
+    series.forEach((v, i) => {
+      const x = padL + (plotW * i) / Math.max(n - 1, 1);
+      const y = padT + plotH * (1 - Math.min(v, 100) / 100);
+      if (i === 0) ctx.moveTo(x, y);
+      else ctx.lineTo(x, y);
+    });
+    ctx.stroke();
+  };
+
+  drawLine(h.cpu, "#4ec9b0"); // CPU - teal (브랜드 시그니처 컬러)
+  drawLine(h.mem, "#d29922"); // MEM - amber (running 상태 컬러와 통일)
+}
+
+function updateChart(sessions) {
+  for (const s of sessions) recordStats(s);
+  drawChart(pickActiveSessionId(sessions));
+}
+
 pollSessions();
 setInterval(pollSessions, 1500);
 </script>
diff --git a/ai_llm_layer/gateway_client.py b/ai_llm_layer/gateway_client.py
index 827554a..0e5f069 100644
--- a/ai_llm_layer/gateway_client.py
+++ b/ai_llm_layer/gateway_client.py
@@ -12,9 +12,9 @@
 
 사전 준비:
     Go 게이트웨이 빌드 및 실행 (별도 터미널)
-        go run event_relay_api.go
+        cd gateway && go run .
     정상이면 다음 로그가 뜬다:
-        이벤트 중계 API 게이트웨이 시작: :8080 (executor=Mock)
+        이벤트 중계 API 게이트웨이 시작: :8080 (executor=*main.DockerExecutor)
 """
 
 from __future__ import annotations
diff --git a/ai_llm_layer/test_datasci_direct.py b/ai_llm_layer/test_datasci_direct.py
new file mode 100644
index 0000000..70d74ba
--- /dev/null
+++ b/ai_llm_layer/test_datasci_direct.py
@@ -0,0 +1,101 @@
+"""
+test_datasci_direct.py
+=======================
+LLM을 완전히 건너뛰고, 검증된 코드를 게이트웨이에 직접 제출해서
+Docker 샌드박스 + matplotlib 파이프라인 자체가 정상인지 확인하는 스크립트.
+
+목적: "LLM이 가끔 형식을 깬다"는 변수를 제거하고, 나머지 파이프라인
+(게이트웨이 -> Docker exec -> matplotlib -> base64 -> 결과 반환)이
+정말 문제없이 동작하는지 독립적으로 검증한다. 이게 통과하면 문제는
+100% AI/LLM 계층(프롬프트/모델)에 있는 것이고, 이것도 실패하면
+인프라 쪽(이미지, gVisor, 리소스 제한 등)을 봐야 한다는 뜻이다.
+
+사용법:
+    python3 test_datasci_direct.py
+(gateway_client.py와 같은 폴더에 있어야 하고, 게이트웨이가 떠 있어야 함)
+"""
+
+import base64
+import uuid
+
+from gateway_client import GatewayError, submit_and_wait
+
+# 이미 검증된 패턴(작은따옴표만 사용, 데이터 직접 정의, Agg 백엔드)의 코드.
+# ai_llm_layer.py 시스템 프롬프트의 예시 3과 완전히 동일한 구조다.
+CODE = """
+import pandas as pd
+import matplotlib
+matplotlib.use('Agg')
+import matplotlib.pyplot as plt
+plt.rcParams['font.family'] = 'NanumGothic'
+import io, base64
+
+months = ['1월','2월','3월','4월','5월','6월','7월','8월','9월','10월','11월','12월']
+sales = [320, 298, 350, 410, 380, 420, 390, 405, 460, 510, 550, 620]
+df = pd.DataFrame({'month': months, 'sales': sales})
+print(df.describe())
+
+fig, ax = plt.subplots(figsize=(8, 4))
+ax.bar(df['month'], df['sales'], color='#4ec9b0')
+ax.set_title('월별 매출 (테스트 데이터)')
+ax.set_ylabel('매출 (만원)')
+
+buf = io.BytesIO()
+fig.savefig(buf, format='png', bbox_inches='tight')
+buf.seek(0)
+print('PLOT_BASE64:' + base64.b64encode(buf.read()).decode())
+""".strip()
+
+
+def main() -> None:
+    session_id = str(uuid.uuid4())
+    payload = {
+        "session_id": session_id,
+        "tool": "run_code",
+        "args": {"language": "python", "code": CODE},
+    }
+
+    print(f"제출 중... (session={session_id})")
+    try:
+        result = submit_and_wait(payload)
+    except GatewayError as e:
+        print(f"[게이트웨이 오류] {e}")
+        print("-> 게이트웨이(cd gateway && go run .)가 떠 있는지 확인하세요.")
+        return
+
+    print(f"상태: {result.status}")
+
+    if result.status != "completed":
+        print(f"[실패] {result.error}")
+        print("-> Docker 이미지(sandbox-python-datasci:latest)가 빌드됐는지,")
+        print("   컨테이너 실행 로그(게이트웨이 터미널)에 에러가 없는지 확인하세요.")
+        return
+
+    plot_prefix = "PLOT_BASE64:"
+    output = result.result or ""
+    idx = output.find(plot_prefix)
+
+    if idx != -1:
+        # 포인트: PLOT_BASE64가 출력 맨 앞에 오라는 법은 없다 - 이번 코드처럼
+        # df.describe() 텍스트가 먼저 출력되고, 그 뒤에 그래프가 이어질 수도 있다.
+        # 그래서 startswith(맨 앞부터 일치)가 아니라 find(어디에 있든 찾기)로 판단해야 한다.
+        text_part = output[:idx].strip()
+        b64_data = output[idx + len(plot_prefix):].strip()
+
+        if text_part:
+            print("--- 텍스트 출력 ---")
+            print(text_part)
+
+        out_path = "test_plot_output.png"
+        with open(out_path, "wb") as f:
+            f.write(base64.b64decode(b64_data))
+        print(f"[성공] 그래프가 정상 생성됐습니다. 저장 위치: {out_path}")
+        print("-> 이 파일을 열어서 실제로 막대그래프가 보이고 한글이 깨지지 않았다면,")
+        print("   인프라/샌드박스 쪽은 완전히 정상입니다.")
+    else:
+        print("[의심] PLOT_BASE64가 출력에 포함되어 있지 않습니다:")
+        print(output)
+
+
+if __name__ == "__main__":
+    main()
diff --git a/cgroup_setup.sh b/cgroup_setup.sh
new file mode 100755
index 0000000..d34e066
--- /dev/null
+++ b/cgroup_setup.sh
@@ -0,0 +1,77 @@
+#!/usr/bin/env bash
+#
+# 리소스 리미터: cgroup v2 기반 CPU 코어 분리 스크립트
+# =====================================================
+# 목적: LLM 추론 프로세스(Ollama)와 Docker 샌드박스 컨테이너가
+#       같은 CPU를 두고 서로 자원을 뺏는 상황(경합)을 물리적으로 막는다.
+#
+# 왜 이 스크립트가 필요한가:
+#   GPU가 있으면 "LLM=GPU, 샌드박스=CPU"로 자연히 자원이 나뉘지만,
+#   이 노트북은 CPU 전용이라 두 워크로드가 같은 자원을 공유한다.
+#   cgroup의 cpuset 기능으로 "이 프로세스 그룹은 반드시 이 코어에서만 돈다"를
+#   커널 레벨에서 강제하면, 소프트웨어 스케줄링에 맡기는 것보다 훨씬 예측 가능해진다.
+#
+# 참고: systemd cgroup 드라이버 환경에서는 docker --cgroup-parent와 충돌하므로,
+#   실제 샌드박스 컨테이너의 자원 제한은 docker_executor.go의 컨테이너 레벨 옵션
+#   (--cpuset-cpus/--memory/--pids-limit)이 담당한다. 이 스크립트는 호스트 레벨 참고용이다.
+#
+# 전제 조건:
+#   - 물리 코어 4개 이상 (0-1: LLM용, 2-3: 샌드박스용으로 나누기 때문)
+#   - cgroup v2가 마운트되어 있어야 함 (Ubuntu 22.04+는 기본적으로 v2 사용)
+#
+# 사용법: sudo ./cgroup_setup.sh
+
+set -euo pipefail
+# set -e: 명령어 하나라도 실패하면 즉시 중단 ("일부만 적용된 상태" 방지)
+# set -u: 정의되지 않은 변수 참조를 에러로 처리 (오타로 인한 사고 방지)
+# set -o pipefail: 파이프라인 중간 명령 실패도 감지
+
+CGROOT="/sys/fs/cgroup"
+LLM_GROUP="${CGROOT}/llm"
+SANDBOX_GROUP="${CGROOT}/sandbox"
+
+echo "[1/5] cgroup v2 마운트 확인"
+if ! mount | grep -q "cgroup2 on ${CGROOT}"; then
+  echo "오류: cgroup v2가 ${CGROOT}에 마운트되어 있지 않습니다." >&2
+  exit 1
+fi
+
+echo "[2/5] llm / sandbox 서브그룹 생성"
+# cgroup v2에서는 디렉토리를 만드는 것 자체가 곧 새로운 cgroup을 만드는 것이다.
+mkdir -p "${LLM_GROUP}"
+mkdir -p "${SANDBOX_GROUP}"
+
+echo "[3/5] CPU 코어 파티셔닝 (cpuset)"
+# 겹치지 않게 나누는 것이 핵심이다. 겹치면 결국 같은 코어를 두고 다시 경합하게 된다.
+echo "0-1" > "${LLM_GROUP}/cpuset.cpus"
+echo "2-3" > "${SANDBOX_GROUP}/cpuset.cpus"
+
+# cpuset.mems: 일반 노트북은 NUMA 노드가 1개(0번)뿐이라 형식적인 설정이지만,
+# 비어있으면 cpuset 컨트롤러가 동작을 거부하는 경우가 있어 명시해 둔다.
+echo "0" > "${LLM_GROUP}/cpuset.mems" 2>/dev/null || true
+echo "0" > "${SANDBOX_GROUP}/cpuset.mems" 2>/dev/null || true
+
+echo "[4/5] 메모리 상한 설정"
+# 이 상한이 없으면, 샌드박스 안의 메모리 폭주 코드가 호스트 전체 메모리를 고갈시켜
+# LLM 프로세스까지 함께 죽일 수 있다. 아래 값은 8GB RAM 기준 예시이므로 실제 RAM에 맞춰 조정할 것.
+echo "4G" > "${LLM_GROUP}/memory.max"
+echo "2G" > "${SANDBOX_GROUP}/memory.max"
+
+echo "[5/5] 완료. 프로세스/컨테이너 배정 방법 안내:"
+cat <<'EOF'
+
+# 1) Ollama 프로세스를 llm cgroup에 배정하는 방법
+#    ollama_pid=$(pgrep -f "ollama serve" | head -n1)
+#    echo "$ollama_pid" > /sys/fs/cgroup/llm/cgroup.procs
+
+# 2) Docker 컨테이너를 sandbox 전용 코어에 고정해서 실행하는 방법
+#    docker run --cpuset-cpus="2,3" --memory="2g" <image>
+
+# --- 참고: 향후 GPU 확보 시 VRAM 제한 ---
+#   nvidia-container-toolkit 설치 후
+#   docker run --gpus '"device=0"' <image>
+#   다만 이것만으로는 VRAM 사용량 상한까지 강제되진 않으며,
+#   NVIDIA MPS(시간분할) 같은 별도 설정이 추가로 필요하다.
+EOF
+
+echo "cgroup 설정 완료: llm(코어0-1, 4G) / sandbox(코어2-3, 2G)"
diff --git a/gateway/cf_access_middleware.go b/gateway/cf_access_middleware.go
new file mode 100644
index 0000000..353a646
--- /dev/null
+++ b/gateway/cf_access_middleware.go
@@ -0,0 +1,247 @@
+// cf_access_middleware.go
+//
+// 네트워크 목표: Cloudflare Tunnel + Zero Trust(Access) 연동의 서버 측 보완책.
+//
+// Cloudflare Access는 엣지(Cloudflare 네트워크)에서 이미 인증되지 않은 사용자를 차단한다.
+// 그런데 그것만 믿으면, "터널을 거치지 않고 이 서버에 직접 접근하는 경로"(같은 호스트,
+// 같은 LAN, 또는 Ingress 설정 실수)가 있을 경우 Access를 완전히 우회하게 된다.
+// 이 파일은 게이트웨이 자신도 "이 요청이 정말 Cloudflare Access를 통과했는가"를
+// 암호학적으로 직접 검증하게 만들어, 심층 방어(defense in depth) 원칙을 네트워크
+// 계층까지 확장한다.
+//
+// 동작 원리:
+//   1) Cloudflare Access를 통과한 요청에는 "Cf-Access-Jwt-Assertion" 헤더가 자동으로 붙는다.
+//   2) 이 JWT는 Cloudflare 팀 도메인의 공개키로 서명되어 있다.
+//   3) 게이트웨이가 그 공개키(JWKS)를 받아와서 서명을 직접 검증하고,
+//      "이 애플리케이션(aud)을 위해 발급된 게 맞는지", "아직 만료 전인지"까지 확인한다.
+//
+// 설정 (환경변수):
+//   CF_ACCESS_TEAM_DOMAIN = "<팀이름>.cloudflareaccess.com"
+//   CF_ACCESS_AUD         = "Access 애플리케이션 설정 화면에서 복사한 Audience 태그"
+//
+// 포인트: 두 환경변수가 비어 있으면 "로컬 전용 개발 모드"로 간주해 검증을 건너뛴다.
+// 대신 반드시 경고 로그를 남겨서 "지금 인증이 꺼져 있다"는 사실을 놓치지 않게 한다.
+package main
+
+import (
+	"crypto"
+	"crypto/rsa"
+	"crypto/sha256"
+	"encoding/base64"
+	"encoding/json"
+	"fmt"
+	"log"
+	"math/big"
+	"net/http"
+	"os"
+	"strings"
+	"sync"
+	"time"
+)
+
+const jwksCacheTTL = 1 * time.Hour
+
+// jwkKey: Cloudflare의 JWKS 엔드포인트가 반환하는 개별 공개키 항목 (표준 JWK 형식 중 쓰는 필드만).
+type jwkKey struct {
+	Kid string `json:"kid"`
+	Kty string `json:"kty"`
+	N   string `json:"n"` // RSA modulus (base64url)
+	E   string `json:"e"` // RSA exponent (base64url)
+}
+
+type jwksResponse struct {
+	Keys []jwkKey `json:"keys"`
+}
+
+// jwksCache: kid -> rsa.PublicKey 캐시.
+// 키는 자주 바뀌지 않으므로 1시간 캐싱하고, kid를 못 찾는 순간 자동으로 재조회한다.
+type jwksCache struct {
+	mu        sync.Mutex
+	keys      map[string]*rsa.PublicKey
+	fetchedAt time.Time
+}
+
+var globalJWKS = &jwksCache{}
+
+func (c *jwksCache) getKey(kid, teamDomain string) (*rsa.PublicKey, error) {
+	c.mu.Lock()
+	defer c.mu.Unlock()
+
+	if key, ok := c.keys[kid]; ok && time.Since(c.fetchedAt) < jwksCacheTTL {
+		return key, nil
+	}
+
+	url := fmt.Sprintf("https://%s/cdn-cgi/access/certs", teamDomain)
+	client := &http.Client{Timeout: 5 * time.Second}
+	resp, err := client.Get(url)
+	if err != nil {
+		return nil, fmt.Errorf("Cloudflare 공개키 조회 실패: %w", err)
+	}
+	defer resp.Body.Close()
+
+	var jwks jwksResponse
+	if err := json.NewDecoder(resp.Body).Decode(&jwks); err != nil {
+		return nil, fmt.Errorf("공개키 응답 파싱 실패: %w", err)
+	}
+
+	newKeys := make(map[string]*rsa.PublicKey, len(jwks.Keys))
+	for _, k := range jwks.Keys {
+		pub, err := jwkToRSAPublicKey(k.N, k.E)
+		if err != nil {
+			continue // 이 키 하나가 이상해도 나머지 키는 계속 쓸 수 있게 건너뛴다
+		}
+		newKeys[k.Kid] = pub
+	}
+	c.keys = newKeys
+	c.fetchedAt = time.Now()
+
+	key, ok := c.keys[kid]
+	if !ok {
+		return nil, fmt.Errorf("kid %q에 해당하는 공개키를 찾을 수 없습니다", kid)
+	}
+	return key, nil
+}
+
+// jwkToRSAPublicKey: JWK의 n(modulus)/e(exponent) 필드를 Go의 rsa.PublicKey로 변환한다.
+func jwkToRSAPublicKey(nB64, eB64 string) (*rsa.PublicKey, error) {
+	nBytes, err := base64.RawURLEncoding.DecodeString(nB64)
+	if err != nil {
+		return nil, fmt.Errorf("modulus 디코딩 실패: %w", err)
+	}
+	eBytes, err := base64.RawURLEncoding.DecodeString(eB64)
+	if err != nil {
+		return nil, fmt.Errorf("exponent 디코딩 실패: %w", err)
+	}
+
+	e := 0
+	for _, b := range eBytes {
+		e = e<<8 | int(b)
+	}
+
+	return &rsa.PublicKey{
+		N: new(big.Int).SetBytes(nBytes),
+		E: e,
+	}, nil
+}
+
+// jwtHeader: JWT의 첫 번째 부분(헤더)에서 서명 검증에 필요한 값만 뽑아낸다.
+type jwtHeader struct {
+	Kid string `json:"kid"`
+	Alg string `json:"alg"`
+}
+
+// verifyCFAccessJWT: Cloudflare Access가 발급한 JWT를 검증하고, 통과하면 claims를 반환한다.
+//
+// 검증 항목:
+//  1. 서명이 Cloudflare 팀 도메인의 공개키로 만든 것이 맞는가 (위조/변조 방지)
+//  2. aud(대상 애플리케이션)가 우리 애플리케이션과 일치하는가 (다른 앱용 토큰 재사용 방지)
+//  3. exp(만료 시각)가 지나지 않았는가 (탈취된 옛 토큰 재사용 방지)
+func verifyCFAccessJWT(token, teamDomain, expectedAud string) (map[string]interface{}, error) {
+	parts := strings.Split(token, ".")
+	if len(parts) != 3 {
+		return nil, fmt.Errorf("JWT 형식이 아닙니다 (세그먼트 %d개)", len(parts))
+	}
+	headerB64, payloadB64, sigB64 := parts[0], parts[1], parts[2]
+
+	headerBytes, err := base64.RawURLEncoding.DecodeString(headerB64)
+	if err != nil {
+		return nil, fmt.Errorf("헤더 디코딩 실패: %w", err)
+	}
+	var header jwtHeader
+	if err := json.Unmarshal(headerBytes, &header); err != nil {
+		return nil, fmt.Errorf("헤더 파싱 실패: %w", err)
+	}
+	if header.Alg != "RS256" {
+		// alg를 고정해 "alg=none" 같은 다운그레이드 공격을 원천 차단한다.
+		return nil, fmt.Errorf("지원하지 않는 서명 알고리즘입니다: %s", header.Alg)
+	}
+
+	pubKey, err := globalJWKS.getKey(header.Kid, teamDomain)
+	if err != nil {
+		return nil, err
+	}
+
+	sig, err := base64.RawURLEncoding.DecodeString(sigB64)
+	if err != nil {
+		return nil, fmt.Errorf("서명 디코딩 실패: %w", err)
+	}
+	signedContent := headerB64 + "." + payloadB64
+	hashed := sha256.Sum256([]byte(signedContent))
+	if err := rsa.VerifyPKCS1v15(pubKey, crypto.SHA256, hashed[:], sig); err != nil {
+		return nil, fmt.Errorf("서명 검증 실패 (위조되었거나 손상된 토큰): %w", err)
+	}
+
+	payloadBytes, err := base64.RawURLEncoding.DecodeString(payloadB64)
+	if err != nil {
+		return nil, fmt.Errorf("페이로드 디코딩 실패: %w", err)
+	}
+	var claims map[string]interface{}
+	if err := json.Unmarshal(payloadBytes, &claims); err != nil {
+		return nil, fmt.Errorf("페이로드 파싱 실패: %w", err)
+	}
+
+	if !audMatches(claims["aud"], expectedAud) {
+		return nil, fmt.Errorf("이 애플리케이션을 위해 발급된 토큰이 아닙니다")
+	}
+	if exp, ok := claims["exp"].(float64); ok {
+		if time.Now().Unix() > int64(exp) {
+			return nil, fmt.Errorf("토큰이 만료되었습니다")
+		}
+	} else {
+		return nil, fmt.Errorf("exp(만료 시각) 클레임이 없습니다")
+	}
+
+	return claims, nil
+}
+
+// audMatches: aud 클레임은 문자열 하나일 수도, 문자열 배열일 수도 있다 (JWT 표준 사양).
+func audMatches(audClaim interface{}, expected string) bool {
+	switch v := audClaim.(type) {
+	case string:
+		return v == expected
+	case []interface{}:
+		for _, item := range v {
+			if s, ok := item.(string); ok && s == expected {
+				return true
+			}
+		}
+	}
+	return false
+}
+
+// withCFAccess: 미들웨어. Cloudflare Access를 통과한 요청인지 서버 자신이 한 번 더 확인한다.
+// main()에서 withCORS(withCFAccess(handler)) 형태로 조합한다.
+func withCFAccess(next http.HandlerFunc) http.HandlerFunc {
+	teamDomain := os.Getenv("CF_ACCESS_TEAM_DOMAIN")
+	aud := os.Getenv("CF_ACCESS_AUD")
+
+	if teamDomain == "" || aud == "" {
+		log.Println("[경고] CF_ACCESS_TEAM_DOMAIN/CF_ACCESS_AUD 미설정 - Access 검증이 비활성화된 " +
+			"로컬 전용 모드로 동작합니다. 외부에 노출하기 전 반드시 두 환경변수를 설정하세요.")
+		return next
+	}
+
+	log.Printf("[게이트웨이] Cloudflare Access 검증 활성화 (team=%s)", teamDomain)
+
+	return func(w http.ResponseWriter, r *http.Request) {
+		token := r.Header.Get("Cf-Access-Jwt-Assertion")
+		if token == "" {
+			http.Error(w, "Cloudflare Access 인증이 필요합니다 (터널을 거치지 않은 접근으로 보입니다)", http.StatusUnauthorized)
+			return
+		}
+
+		claims, err := verifyCFAccessJWT(token, teamDomain, aud)
+		if err != nil {
+			log.Printf("[거부] Cloudflare Access 검증 실패: %v", err)
+			http.Error(w, "인증 검증에 실패했습니다", http.StatusUnauthorized)
+			return
+		}
+
+		// 감사 로그: 누가(email) 요청했는지 남긴다 - "사후 추적 가능성" 요구사항.
+		if email, ok := claims["email"].(string); ok {
+			log.Printf("[인증됨] %s -> %s", email, r.URL.Path)
+		}
+
+		next(w, r)
+	}
+}
diff --git a/gateway/docker_executor.go b/gateway/docker_executor.go
index c06cf31..02eefce 100644
--- a/gateway/docker_executor.go
+++ b/gateway/docker_executor.go
@@ -4,19 +4,25 @@
 // event_relay_api.go의 SandboxExecutor 인터페이스만 만족시키면 되므로,
 // 게이트웨이 쪽 코드는 이 파일의 존재 자체를 몰라도 된다 (의존성 주입으로 완전히 분리됨).
 //
-// 설계 변경 이력: 처음엔 github.com/docker/docker Go SDK를 사용했으나,
-// 그 SDK가 내부적으로 의존하는 docker/distribution과 distribution/reference
-// 모듈 버전이 서로 충돌하는(undefined: reference.SplitHostname) 고질적인 문제가 있었다.
-// 그래서 SDK 대신 "docker" CLI 명령어를 os/exec로 직접 호출하는 방식으로 바꿨다.
-// 이렇게 하면 Docker 관련 Go 의존성이 전혀 필요 없어지고(표준 라이브러리만 사용),
-// 터미널에서 직접 docker run을 칠 때와 완전히 동일하게 동작해 디버깅도 쉬워진다.
+// 설계 변경 이력 1: github.com/docker/docker Go SDK -> docker CLI를 os/exec로 직접 호출.
+//   SDK가 의존하는 docker/distribution과 distribution/reference 모듈 버전 충돌
+//   (undefined: reference.SplitHostname) 문제를 근본적으로 피하기 위함.
+//
+// 설계 변경 이력 2: "이미지 프리풀"만 하던 웜스탠바이를,
+//   실제로 컨테이너를 미리 띄워두고 재사용하는 "진짜 웜스탠바이"로 승격.
+//   미리 떠 있는 idle 컨테이너에 docker exec로 코드만 주입하는 방식으로 바꿔서
+//   "컨테이너 생성" 비용을 요청 경로에서 제거했다. 대신 실행이 끝난 컨테이너는
+//   여전히 재사용하지 않고 즉시 폐기 + 백그라운드로 새 idle 컨테이너를 채워 넣는다
+//   ("재사용은 대기 상태까지만, 실행된 컨테이너는 반드시 버린다").
+//
+// 설계 변경 이력 3: gVisor(runsc) 런타임 자동 감지 + SANDBOX_RUNTIME 환경변수로 강제 전환.
 //
 // 이 파일이 담당하는 세 가지 역할 (아키텍처의 인프라/보안 계층 3개 컴포넌트와 대응):
-//   1) 샌드박스 매니저 -> Run() 함수의 세션 생명주기 처리 (docker run 실행 -> 대기 -> --rm으로 자동 폐기)
-//   2) 리소스 리미터   -> docker run에 넘기는 --cpuset-cpus/--memory/--pids-limit 플래그
-//   3) 컨테이너 풀     -> EnsureImages()로 이미지를 미리 받아두는 "웜스탠바이"
+//   1) 샌드박스 매니저 -> Run() 함수의 세션 생명주기 처리 (풀에서 대여 -> exec -> 폐기+보충)
+//   2) 리소스 리미터   -> idle 컨테이너 생성 시 넘기는 --cpuset-cpus/--memory/--pids-limit
+//   3) 컨테이너 풀     -> idlePool 채널 (진짜 웜스탠바이) + 실시간 자원 모니터링(pollStats)
 //
-// 사전 준비 (지후님 로컬 PC):
+// 사전 준비:
 //   docker 데몬 실행 중이어야 함 (systemctl status docker)
 //   docker CLI가 PATH에 있어야 함 (which docker)
 //   sudo usermod -aG docker $USER (sudo 없이 docker 명령을 쓰려면, 재로그인 필요)
@@ -28,77 +34,244 @@ import (
 	"context"
 	"fmt"
 	"log"
+	"os"
 	"os/exec"
+	"strings"
+	"time"
 )
 
 // ===== 리소스 리미터 설정값 =====
 // 포인트: 아래 코어 배정은 cgroup_setup.sh가 만든 /sys/fs/cgroup/sandbox의 코어 범위와
-// 반드시 일치해야 한다 (실제 실행 시 지후님 로컬 환경이 systemd cgroup 드라이버를 사용해
-// --cgroup-parent 플래그와 충돌이 확인되어, 호스트 cgroup과의 이중 편입은 제거했다.
-// SandboxCgroupParent 상수는 cgroup_setup.sh와의 코어 배정 값이 같다는 것을 문서화하는
-// 용도로만 남겨두고, 실제 docker run 커맨드에는 더 이상 전달하지 않는다).
+// 반드시 일치해야 한다. (로컬 환경이 systemd cgroup 드라이버를 사용해 --cgroup-parent
+// 플래그와 충돌이 확인되어, 호스트 cgroup과의 이중 편입은 제거했다. SandboxCgroupParent
+// 상수는 문서화 용도로만 남겨두고, 실제 docker 커맨드에는 전달하지 않는다.)
 const (
-	SandboxCgroupParent = "sandbox" // 참고용: cgroup_setup.sh가 만든 /sys/fs/cgroup/sandbox (더 이상 dockerArgs에 전달하지 않음)
-	SandboxCpuset       = "2,3"     // 샌드박스 전용 코어 (event_relay_api.go 설계와 동일)
+	SandboxCgroupParent = "sandbox" // 참고용: cgroup_setup.sh가 만든 /sys/fs/cgroup/sandbox
+	SandboxCpuset       = "2,3"     // 샌드박스 전용 코어
 	SandboxMemory       = "512m"    // 컨테이너 1개당 메모리 상한 (그룹 상한 2G를 동시세션 2개+여유로 분배)
 	SandboxPidsLimit    = "64"      // 포크폭탄 방지: 컨테이너 내 프로세스 개수 상한
+
+	// WarmPoolSize: 미리 띄워둘 idle 컨테이너 개수.
+	// CPU 코어가 2-3 두 개뿐인 온디바이스 환경에서 idle 컨테이너를 여러 개 띄워두면
+	// 그 자체로 메모리/CPU를 잠식하므로, 딱 1개만 상시 대기시킨다.
+	WarmPoolSize = 1
+
+	// StatsPollInterval: 실행 중인 컨테이너의 CPU/메모리를 얼마나 자주 샘플링할지.
+	StatsPollInterval = 500 * time.Millisecond
 )
 
 // 도구별 실행 이미지/커맨드 매핑.
 // 포인트: 화이트리스트 원칙을 여기서도 반복한다 - languageRunners에 없는 언어는 즉시 거부.
-// tool_call_parser.py의 ALLOWED_TOOLS와 마찬가지로, "명시적으로 검토한 것만 허용"하는 방식이다.
 var languageRunners = map[string]struct {
 	Image string
 	Build func(code string) []string // 코드를 컨테이너 안에서 실행할 커맨드(argv)로 변환
 }{
 	"python": {
-		Image: "python:3.12-slim",
-		// 포인트: 이 배열은 "docker run ... python:3.12-slim python3 -c <code>"에서
-		// 이미지 뒤에 붙는 부분이다. code 전체가 python3의 -c 인자 '하나'로만 전달되므로,
-		// 셸을 거치지 않아 code 안의 특수문자(;, |, $() 등)로 인한 명령 주입 자체가 불가능하다.
+		// pandas/matplotlib/numpy가 미리 설치된 커스텀 이미지(sandbox-python-datasci.Dockerfile).
+		// 실행 컨테이너는 --network none이라 런타임에 pip install을 할 수 없으므로 빌드 시점에 넣어둔다.
+		// 사전 준비: docker build -t sandbox-python-datasci:latest -f sandbox-python-datasci.Dockerfile .
+		Image: "sandbox-python-datasci:latest",
+		// code 전체가 python3의 -c 인자 '하나'로만 전달되므로, 셸을 거치지 않아
+		// code 안의 특수문자로 인한 명령 주입이 불가능하다.
 		Build: func(code string) []string { return []string{"python3", "-c", code} },
 	},
 }
 
 // DockerExecutor: SandboxExecutor 인터페이스의 실제 구현체 (docker CLI 호출 기반).
-type DockerExecutor struct{}
+type DockerExecutor struct {
+	idlePool chan string // 실행 준비가 된 idle 컨테이너 ID들 (진짜 웜스탠바이 풀)
+	runtime  string      // "runsc"(gVisor 사용 가능) 또는 ""(기본 runc로 폴백)
+}
 
 func NewDockerExecutor() (*DockerExecutor, error) {
 	if _, err := exec.LookPath("docker"); err != nil {
 		return nil, fmt.Errorf("docker CLI를 찾을 수 없습니다 (PATH 확인 필요): %w", err)
 	}
-	return &DockerExecutor{}, nil
+
+	d := &DockerExecutor{idlePool: make(chan string, WarmPoolSize)}
+
+	// 기본은 자동 감지지만, SANDBOX_RUNTIME 환경변수로 강제 지정할 수 있다.
+	// 같은 바이너리·같은 설치 상태에서 런타임만 바꿔가며 A/B 벤치마크를 돌리기 위함이다.
+	switch strings.ToLower(strings.TrimSpace(os.Getenv("SANDBOX_RUNTIME"))) {
+	case "runc":
+		d.runtime = ""
+		log.Println("[샌드박스 매니저] SANDBOX_RUNTIME=runc 지정됨 - gVisor 비활성화(runc 강제, 베이스라인 측정용)")
+	case "runsc":
+		d.runtime = "runsc"
+		log.Println("[샌드박스 매니저] SANDBOX_RUNTIME=runsc 지정됨 - gVisor 강제 사용")
+	default:
+		d.runtime = detectGvisorRuntime()
+		// gVisor가 없다고 서버 시작 자체를 막지는 않는다 ("우아한 성능 저하").
+		// 다만 연구 결과를 발표할 때는 gVisor가 실제로 적용된 상태인지 로그로 반드시 확인해야 한다.
+		if d.runtime == "runsc" {
+			log.Println("[샌드박스 매니저] gVisor(runsc) 런타임 감지됨 - 강화된 커널 격리 적용")
+		} else {
+			log.Println("[경고] gVisor(runsc) 런타임을 찾을 수 없어 기본 runc로 동작합니다 " +
+				"(설치: https://gvisor.dev/docs/user_guide/install/)")
+		}
+	}
+
+	return d, nil
 }
 
-// EnsureImages: 서버 시작 시 필요한 이미지를 미리 받아둔다 ("docker pull"을 미리 실행).
-//
-// 포인트: 이것이 이 구현에서 '웜스탠바이'가 실질적으로 의미하는 바다.
-// 컨테이너 자체를 미리 띄워 재사용하는 방식(진짜 웜 컨테이너)은 상태 오염 위험이 있어
-// 애초에 지양하기로 설계했었다 (아키텍처 문서 4️⃣ 참고). 대신 "이미지가 이미 로컬에 있어
-// 매 요청마다 수십 초~수 분씩 걸리는 최초 pull 지연을 없애는 것"을 웜스탠바이의 실질적
-// 목표로 재정의했다 - 첫 요청부터 빠르게 응답하면서도, 컨테이너는 여전히 매번 새로 만든다.
+// detectGvisorRuntime: "docker info"로 이 Docker 데몬에 runsc 런타임이 등록되어 있는지 확인한다.
+// 런타임 목록은 데몬 재시작 전까지 바뀌지 않으므로 시작 시 한 번만 확인해 캐싱한다.
+func detectGvisorRuntime() string {
+	out, err := exec.Command("docker", "info", "--format", "{{json .Runtimes}}").Output()
+	if err != nil {
+		return ""
+	}
+	if strings.Contains(string(out), "runsc") {
+		return "runsc"
+	}
+	return ""
+}
+
+// EnsureImages: 이미지를 준비하고, 웜스탠바이 풀을 초기 채운다.
 func (d *DockerExecutor) EnsureImages(ctx context.Context) error {
 	for lang, runner := range languageRunners {
+		// 로컬에서 직접 빌드한 이미지는 레지스트리에 없으므로, 먼저 "docker image inspect"로
+		// 로컬 존재 여부를 확인하고 있으면 pull을 건너뛴다.
+		if err := exec.CommandContext(ctx, "docker", "image", "inspect", runner.Image).Run(); err == nil {
+			log.Printf("[샌드박스 매니저] 이미지 로컬에 이미 존재: %s (%s)", lang, runner.Image)
+			continue
+		}
+
 		log.Printf("[샌드박스 매니저] 이미지 준비 확인: %s (%s)", lang, runner.Image)
 		cmd := exec.CommandContext(ctx, "docker", "pull", runner.Image)
 		out, err := cmd.CombinedOutput()
 		if err != nil {
-			return fmt.Errorf("이미지 %s(%s) 준비 실패: %w\n%s", lang, runner.Image, err, string(out))
+			return fmt.Errorf(
+				"이미지 %s(%s) 준비 실패: %w\n%s\n"+
+					"커스텀 이미지라면 먼저 로컬에서 빌드해야 합니다: "+
+					"docker build -t %s -f sandbox-python-datasci.Dockerfile .",
+				lang, runner.Image, err, string(out), runner.Image,
+			)
 		}
 	}
+
+	// 웜스탠바이 풀 초기화 - python 이미지로 WarmPoolSize개의 idle 컨테이너를 미리 띄운다.
+	pythonImage := languageRunners["python"].Image
+	for i := 0; i < WarmPoolSize; i++ {
+		id, err := d.startIdleContainer(ctx, pythonImage)
+		if err != nil {
+			return fmt.Errorf("웜 컨테이너 준비 실패: %w", err)
+		}
+		d.idlePool <- id
+		log.Printf("[샌드박스 매니저] 웜 컨테이너 준비 완료: %s", shortID(id))
+	}
 	return nil
 }
 
+// startIdleContainer: "sleep infinity"로 대기만 하는 컨테이너를 하나 띄운다.
+// 이 컨테이너 자체가 리소스 리미터/네트워크 차단/탈옥 방지 옵션을 전부 갖고 태어나며,
+// 나중에 docker exec로 주입한 프로세스도 이 제약을 그대로 상속받는다.
+//
+// gVisor(runsc) 적용 지점: --runtime 플래그는 컨테이너 "생성" 시점에만 필요하다.
+// docker exec는 이미 만들어진 컨테이너의 런타임을 그대로 물려받는다.
+func (d *DockerExecutor) startIdleContainer(ctx context.Context, image string) (string, error) {
+	args := []string{
+		"run", "-d", // 백그라운드 대기 (--rm 없음: 실행 후 destroyAndReplenish에서 직접 폐기)
+	}
+
+	if d.runtime != "" {
+		args = append(args, "--runtime", d.runtime)
+	}
+
+	args = append(args,
+		// --- 네트워크 정책: 완전 차단 ---
+		"--network", "none",
+
+		// --- 리소스 리미터 ---
+		"--cpuset-cpus", SandboxCpuset,
+		"--memory", SandboxMemory,
+		"--pids-limit", SandboxPidsLimit,
+
+		// --- 탈옥 피해 최소화 (심층 방어) ---
+		// gVisor와 일부 중복되지만, "여러 겹 방어" 원칙을 유지한다.
+		"--read-only",
+		"--tmpfs", "/tmp:rw,size=64m",
+		"--cap-drop", "ALL",
+		"--security-opt", "no-new-privileges",
+
+		image, "sleep", "infinity",
+	)
+	out, err := exec.CommandContext(ctx, "docker", args...).Output()
+	if err != nil {
+		return "", fmt.Errorf("idle 컨테이너 시작 실패: %w", err)
+	}
+	return strings.TrimSpace(string(out)), nil
+}
+
+// destroyAndReplenish: 사용이 끝난(오염 가능성이 있는) 컨테이너를 강제 삭제하고,
+// 백그라운드로 새 idle 컨테이너를 하나 채워 넣어 풀을 유지한다.
+// "재사용 금지" 원칙과 "웜스탠바이" 최적화를 동시에 만족시키는 핵심 함수다.
+func (d *DockerExecutor) destroyAndReplenish(containerID, image string) {
+	cleanupCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
+	defer cancel()
+	if err := exec.CommandContext(cleanupCtx, "docker", "rm", "-f", containerID).Run(); err != nil {
+		log.Printf("[경고] 컨테이너 제거 실패 (수동 정리 필요할 수 있음): %s: %v", shortID(containerID), err)
+	}
+
+	go func() {
+		replenishCtx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
+		defer cancel()
+		id, err := d.startIdleContainer(replenishCtx, image)
+		if err != nil {
+			// 여기서 실패해도 다음 요청이 막히진 않는다 - Run()에 즉석 생성 폴백 경로가 있다.
+			log.Printf("[경고] 웜 컨테이너 보충 실패: %v", err)
+			return
+		}
+		select {
+		case d.idlePool <- id:
+			log.Printf("[샌드박스 매니저] 웜 컨테이너 보충 완료: %s", shortID(id))
+		default:
+			// 풀이 이미 가득 찼다면(즉석 생성 경로와 겹친 경우) 여분 컨테이너는 바로 정리한다.
+			_ = exec.Command("docker", "rm", "-f", id).Run()
+		}
+	}()
+}
+
+// pollStats: 실행 중인 컨테이너의 CPU/메모리 사용량을 주기적으로 샘플링해 onStats로 보고한다.
+func (d *DockerExecutor) pollStats(containerID string, onStats StatsCallback, done <-chan struct{}) {
+	if onStats == nil {
+		return
+	}
+	ticker := time.NewTicker(StatsPollInterval)
+	defer ticker.Stop()
+	for {
+		select {
+		case <-done:
+			return
+		case <-ticker.C:
+			cpu, mem, err := d.readStats(containerID)
+			if err != nil {
+				continue // 컨테이너 시작/종료 타이밍이면 한 번쯤 실패할 수 있다 - 다음 틱에 재시도
+			}
+			onStats(cpu, mem)
+		}
+	}
+}
+
+// readStats: "docker stats --no-stream"으로 순간 CPU%/메모리 사용량을 한 번 읽어온다.
+func (d *DockerExecutor) readStats(containerID string) (cpuPct string, memUsage string, err error) {
+	out, err := exec.Command("docker", "stats", containerID, "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}").Output()
+	if err != nil {
+		return "", "", err
+	}
+	parts := strings.SplitN(strings.TrimSpace(string(out)), "|", 2)
+	if len(parts) != 2 {
+		return "", "", fmt.Errorf("docker stats 출력 형식이 예상과 다릅니다: %q", out)
+	}
+	return parts[0], parts[1], nil
+}
+
 // Run: SandboxExecutor 인터페이스 구현. event_relay_api.go의 runSession()이 이 함수를 호출한다.
 //
-// 세션 생명주기: [명령 조립] -> [docker run 실행] -> [대기] -> [자동 폐기(--rm)]
-// 포인트: --rm 플래그가 있으면 컨테이너 프로세스가 끝나는 즉시 Docker가 알아서 컨테이너를
-// 제거해준다. "하나의 세션 = 하나의 컨테이너, 재사용 금지" 원칙을 별도 정리 코드 없이
-// Docker 자체 기능으로 강제하는 셈이다.
-func (d *DockerExecutor) Run(ctx context.Context, tool string, args map[string]interface{}) (string, error) {
+// 세션 생명주기: [풀에서 대여] -> [docker exec로 코드 주입] -> [대기] -> [강제 폐기+풀 보충]
+func (d *DockerExecutor) Run(ctx context.Context, tool string, args map[string]interface{}, onStats StatsCallback) (string, error) {
 	if tool != "run_code" {
-		// 화이트리스트에 없는 도구 요청 - 파서 단계에서 이미 걸러졌어야 하지만,
-		// 계층 간 신뢰를 100%로 가정하지 않고 여기서도 한 번 더 확인한다 (심층 방어).
+		// 파서 단계에서 이미 걸러졌어야 하지만, 계층 간 신뢰를 100%로 가정하지 않는다 (심층 방어).
 		return "", fmt.Errorf("샌드박스 매니저는 run_code만 지원합니다 (요청: %s)", tool)
 	}
 
@@ -112,40 +285,33 @@ func (d *DockerExecutor) Run(ctx context.Context, tool string, args map[string]i
 		return "", fmt.Errorf("code 인자가 비어 있습니다")
 	}
 
-	dockerArgs := []string{
-		"run", "--rm", // 종료 즉시 자동 삭제 (재사용 금지 원칙을 Docker 기능으로 강제)
-
-		// --- 네트워크 정책: 기본 차단 ---
-		// 포인트: "none"은 화이트리스트 이전에 네트워크 인터페이스 자체가 없는 상태다.
-		// run_code는 애초에 네트워크가 필요 없는 순수 계산 작업을 전제로 하므로 안전하다.
-		"--network", "none",
-
-		// --- 리소스 리미터: cgroup_setup.sh의 host 레벨 설정과 짝을 이룬다 ---
-		// 포인트: 원래는 --cgroup-parent로 호스트의 /sys/fs/cgroup/sandbox 산하에
-		// 컨테이너를 편입시켜 이중 방어를 하려 했으나, systemd cgroup 드라이버를 쓰는
-		// 환경(Ubuntu 24.04 기본값)에서는 --cgroup-parent에 "sandbox.slice"처럼
-		// systemd 슬라이스 이름 규칙을 따라야만 동작한다. 이 프로젝트 규모에서는
-		// 별도의 systemd 슬라이스를 만드는 복잡도를 감수할 실익이 적어서, 이 플래그는
-		// 제거하고 컨테이너 자체의 cpuset/memory/pids 제한만으로 리소스를 강제하기로 했다.
-		// (cgroup_setup.sh가 만든 llm/sandbox 그룹은 여전히 유효하며, Ollama 프로세스를
-		//  llm 그룹에 배정하는 용도로는 그대로 사용한다.)
-		"--cpuset-cpus", SandboxCpuset,
-		"--memory", SandboxMemory,
-		"--pids-limit", SandboxPidsLimit,
+	// ----- [풀에서 대여] -----
+	var containerID string
+	select {
+	case containerID = <-d.idlePool:
+		log.Printf("[샌드박스 매니저] 웜 컨테이너 재사용: %s", shortID(containerID))
+	case <-time.After(2 * time.Second):
+		// 풀이 비어있다면 그 자리에서 즉시 새 컨테이너를 만든다 - 지연은 늘지만 요청을 거부하진 않는다.
+		log.Printf("[샌드박스 매니저] 웜 컨테이너 풀이 비어있어 즉시 생성합니다")
+		id, err := d.startIdleContainer(ctx, runner.Image)
+		if err != nil {
+			return "", fmt.Errorf("컨테이너 준비 실패: %w", err)
+		}
+		containerID = id
+	}
 
-		// --- 탈옥 피해 최소화 (심층 방어) ---
-		"--read-only",                       // 루트 파일시스템 쓰기 금지
-		"--tmpfs", "/tmp:rw,size=64m",       // 코드 실행에 필요한 최소 쓰기 공간만 예외 허용
-		"--cap-drop", "ALL",                  // 모든 리눅스 capability 제거
-		"--security-opt", "no-new-privileges", // setuid 등을 통한 권한 상승 금지
+	// ----- [폐기 보장 + 풀 보충] -----
+	defer d.destroyAndReplenish(containerID, runner.Image)
 
-		runner.Image,
-	}
-	dockerArgs = append(dockerArgs, runner.Build(code)...)
+	// ----- [자원 모니터링 시작] -----
+	statsDone := make(chan struct{})
+	go d.pollStats(containerID, onStats, statsDone)
+	defer close(statsDone)
 
-	// context에 데드라인이 걸려 있으면(게이트웨이의 SessionTimeout), CommandContext가
-	// 그 시점에 프로세스를 자동으로 Kill한다 - 별도 타이머 코드를 직접 짤 필요가 없다.
-	cmd := exec.CommandContext(ctx, "docker", dockerArgs...)
+	// ----- [docker exec로 코드 주입] -----
+	// 컨테이너는 이미 떠 있으므로 "이미 격리된 환경 안에 새 프로세스 하나를 추가로 실행"할 뿐이다.
+	execArgs := append([]string{"exec", containerID}, runner.Build(code)...)
+	cmd := exec.CommandContext(ctx, "docker", execArgs...)
 	var stdout, stderr bytes.Buffer
 	cmd.Stdout = &stdout
 	cmd.Stderr = &stderr
@@ -153,18 +319,32 @@ func (d *DockerExecutor) Run(ctx context.Context, tool string, args map[string]i
 	runErr := cmd.Run()
 
 	if ctx.Err() == context.DeadlineExceeded {
+		// exec 클라이언트는 CommandContext로 죽지만, 컨테이너 내부 프로세스까지 확실히 정리되는 건
+		// defer destroyAndReplenish()가 컨테이너를 강제 삭제(docker rm -f)하기 때문이다.
 		return "", fmt.Errorf("실행 시간 초과로 컨테이너를 강제 종료했습니다")
 	}
 
-	if stderr.Len() > 0 {
-		// 포인트: stderr에 내용이 있으면 실패로 간주한다. LLM이 생성한 코드에 문법 오류가
-		// 있었다면 바로 여기서 걸러진다 - 파서는 형식만 검증하고, 코드 내용의 실제
-		// 정확성은 이 실행 단계에서 자연스럽게 드러나도록 설계했다 (설계문서 9️⃣ 참고).
-		return "", fmt.Errorf("실행 중 오류 출력: %s", stderr.String())
-	}
+	// 포인트: 성공/실패 판정은 stderr 유무가 아니라 프로세스 종료 코드(exit code)로 한다.
+	// 파이썬은 정상 실행 중에도 경고(matplotlib 캐시 알림 등)를 stderr로 출력하기 때문이다.
 	if runErr != nil {
+		if stderr.Len() > 0 {
+			return "", fmt.Errorf("실행 중 오류 출력: %s", stderr.String())
+		}
 		return "", fmt.Errorf("컨테이너 실행 실패: %w", runErr)
 	}
 
+	// 종료 코드 0인데 stderr에 내용이 있다면 경고일 뿐이다 - 서버 로그에만 남긴다.
+	if stderr.Len() > 0 {
+		log.Printf("[참고] 실행은 성공했지만 경고가 출력됨: %s", stderr.String())
+	}
+
 	return stdout.String(), nil
 }
+
+// shortID: 로그 출력용으로 컨테이너 ID 앞 12자리만 잘라낸다 (짧은 ID에서도 패닉하지 않도록).
+func shortID(id string) string {
+	if len(id) > 12 {
+		return id[:12]
+	}
+	return id
+}
diff --git a/gateway/event_relay_api.go b/gateway/event_relay_api.go
index 46404a4..4e000e4 100644
--- a/gateway/event_relay_api.go
+++ b/gateway/event_relay_api.go
@@ -8,11 +8,10 @@
 //   2) 실행 인터페이스  -> SandboxExecutor 인터페이스
 //
 // 왜 이 둘을 인터페이스로 분리했는가:
-//   지금은 ③ 인프라/보안 계층(샌드박스 매니저)이 아직 없으므로,
 //   MockExecutor라는 가짜 구현체로 전체 파이프라인(AI/LLM -> 게이트웨이)을
-//   먼저 검증할 수 있다. 나중에 실제 Docker 실행 로직을 만들 때는
-//   SandboxExecutor 인터페이스만 구현해서 NewGateway에 주입하면 되고,
-//   Gateway 구조체나 HTTP 핸들러 코드는 단 한 줄도 고칠 필요가 없다.
+//   먼저 검증한 뒤, 실제 Docker 실행 로직(DockerExecutor)은 SandboxExecutor
+//   인터페이스만 구현해서 NewGateway에 주입했다. Gateway 구조체나 HTTP 핸들러
+//   코드는 단 한 줄도 고칠 필요가 없었다.
 //   이것이 "실행 인터페이스를 별도 계약으로 둔다"의 실질적 의미다.
 package main
 
@@ -36,8 +35,6 @@ const (
 
 	// 세션 하나가 허용되는 최대 실행 시간.
 	// 포인트: tool_call_parser.py의 MAX_TIMEOUT_SEC(30)과 반드시 같은 값으로 유지할 것.
-	// 두 계층의 타임아웃 정책이 어긋나면, "왜 파서는 30초를 허용한다는데
-	// 실제로는 다른 시간에 끊기지?" 같은 디버깅하기 어려운 불일치가 생긴다.
 	SessionTimeout = 30 * time.Second
 )
 
@@ -67,51 +64,57 @@ type SessionState struct {
 	StartedAt time.Time `json:"started_at"`
 	Result    string    `json:"result,omitempty"`
 	Error     string    `json:"error,omitempty"`
+	CPUUsage  string    `json:"cpu_usage,omitempty"` // 예: "3.42%" (docker stats 실측치)
+	MemUsage  string    `json:"mem_usage,omitempty"` // 예: "12.5MiB / 512MiB"
 }
 
 // -----------------------------------------------------------------------
 // 실행 인터페이스 (백엔드/파이프라인 계층의 두 번째 컴포넌트)
 // -----------------------------------------------------------------------
 
+// StatsCallback: 실행 중인 컨테이너의 CPU/메모리 실측치를 주기적으로 보고하는 콜백.
+// 포인트: "실행"과 "모니터링"의 책임을 분리하기 위해 Run()의 인자로 넘긴다.
+// DockerExecutor는 주기적으로 이 콜백을 호출해 최신 수치를 흘려보내기만 하고,
+// 그 값을 세션 상태에 저장하고 대시보드에 노출하는 건 게이트웨이의 책임이다.
+type StatsCallback func(cpuPct string, memUsage string)
+
 // SandboxExecutor: "샌드박스에 무엇을, 어떤 형식으로 넘길지"에 대한 계약(contract).
-// ③ 인프라/보안 계층을 구현할 때, 이 인터페이스를 만족하는 구조체(예: DockerExecutor)를
-// 하나 더 만들어서 NewGateway(dockerExecutor)로 교체하기만 하면 된다.
 type SandboxExecutor interface {
 	// Run: 도구 이름과 인자를 받아 실행하고, 결과 문자열 또는 에러를 반환한다.
 	// ctx가 취소(타임아웃)되면 구현체는 반드시 실행 중인 프로세스/컨테이너를
-	// 강제 종료해야 한다 (그렇지 않으면 타임아웃 났지만 컨테이너는 계속 도는
-	// 자원 누수가 발생한다 - 실제 Docker 구현 시 반드시 지켜야 할 계약 조건).
-	Run(ctx context.Context, tool string, args map[string]interface{}) (string, error)
+	// 강제 종료해야 한다 (그렇지 않으면 자원 누수가 발생한다).
+	// onStats: nil이 아니면 구현체가 실행 도중 주기적으로 CPU/메모리 실측치를 보고한다.
+	Run(ctx context.Context, tool string, args map[string]interface{}, onStats StatsCallback) (string, error)
 }
 
-// MockExecutor: 샌드박스 매니저가 아직 없을 때 파이프라인 전체(AI/LLM -> 게이트웨이)를
-// 먼저 검증하기 위한 임시 구현체. 실제 코드를 실행하지 않고, 받은 요청을 그대로
-// 텍스트로 되돌려주기만 한다.
+// MockExecutor: 실제 코드를 실행하지 않고, 받은 요청을 그대로 텍스트로 되돌려주는
+// 임시 구현체. Docker가 없는 환경에서 파이프라인을 검증하기 위한 폴백으로 쓰인다.
 type MockExecutor struct{}
 
-func (m *MockExecutor) Run(ctx context.Context, tool string, args map[string]interface{}) (string, error) {
-	// 실제 실행에 걸리는 지연을 흉내만 낸다.
+func (m *MockExecutor) Run(ctx context.Context, tool string, args map[string]interface{}, onStats StatsCallback) (string, error) {
 	select {
 	case <-time.After(200 * time.Millisecond):
 	case <-ctx.Done():
 		return "", ctx.Err()
 	}
+	if onStats != nil {
+		onStats("0.00%", "0MiB / 0MiB (mock)")
+	}
 	return fmt.Sprintf("[MOCK 실행 결과] tool=%s args=%v (실제 샌드박스 미연결 상태)", tool, args), nil
 }
 
 // ===== 게이트웨이 상태 =====
 
 type Gateway struct {
-	sem      chan struct{}    // 동시 세션 수 제한용 세마포어 (버퍼드 채널)
-	sessions sync.Map         // sessionID -> *SessionState
-	executor SandboxExecutor  // 실제 실행을 위임할 대상 (지금은 MockExecutor)
+	sem      chan struct{}   // 동시 세션 수 제한용 세마포어 (버퍼드 채널)
+	sessions sync.Map        // sessionID -> *SessionState
+	executor SandboxExecutor // 실제 실행을 위임할 대상
 }
 
 // NewGateway: executor를 외부에서 주입받는다 (의존성 주입).
-// 이렇게 생성자에서 주입받는 구조로 만들어두면, 나중에 실제 Docker 기반
-// 샌드박스 매니저가 준비됐을 때 main()의 이 한 줄만 바꾸면 전체 시스템이 교체된다.
-//   gw := NewGateway(&MockExecutor{})       // 지금
-//   gw := NewGateway(&DockerExecutor{...})  // 나중에
+//
+//	gw := NewGateway(&MockExecutor{})   // 개발 초기
+//	gw := NewGateway(dockerExecutor)    // 현재
 func NewGateway(executor SandboxExecutor) *Gateway {
 	return &Gateway{
 		sem:      make(chan struct{}, MaxConcurrentSessions),
@@ -123,8 +126,7 @@ func NewGateway(executor SandboxExecutor) *Gateway {
 
 // handleExecute: POST /v1/execute
 // 처리 흐름: 스키마 검증 -> 세마포어 논블로킹 획득 시도 -> 성공 시 비동기 실행 위임.
-// 동시성 초과 시 큐잉하지 않고 즉시 429로 거부한다 (온디바이스 CPU 보호가 우선이라는
-// 설계 판단 - 무한 대기열보다 클라이언트의 명시적 재시도가 더 안전하다고 봤다).
+// 동시성 초과 시 큐잉하지 않고 즉시 429로 거부한다 (온디바이스 CPU 보호 우선).
 func (g *Gateway) handleExecute(w http.ResponseWriter, r *http.Request) {
 	if r.Method != http.MethodPost {
 		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
@@ -170,10 +172,7 @@ func (g *Gateway) handleSessionStatus(w http.ResponseWriter, r *http.Request) {
 }
 
 // handleSessionsAll: GET /v1/sessions/all
-// 모니터링 대시보드 프론트엔드가 전체 세션 목록을 한 번에 가져오기 위한 엔드포인트.
-// 포인트: gateway_client.py의 폴링(단일 세션 조회)과는 목적이 다르다 -
-// 이 엔드포인트는 "지금까지 무슨 세션들이 있었는지"를 보여주는 대시보드 전용이다.
-// 최근 것부터 보여주기 위해 StartedAt 기준 내림차순으로 정렬한다.
+// 모니터링 대시보드 전용: 전체 세션 목록을 최근 것부터(StartedAt 내림차순) 반환한다.
 func (g *Gateway) handleSessionsAll(w http.ResponseWriter, r *http.Request) {
 	var all []*SessionState
 	g.sessions.Range(func(_, v interface{}) bool {
@@ -193,20 +192,15 @@ func (g *Gateway) handleHealth(w http.ResponseWriter, r *http.Request) {
 	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
 }
 
-// withCORS: 프론트엔드(브라우저)가 다른 출처(예: file:// 또는 다른 포트로 열린 정적 HTML)에서
-// 이 API를 호출할 수 있도록 CORS 헤더를 붙여주는 미들웨어.
-//
-// 포인트: 브라우저는 보안상 "다른 출처의 API를 기본적으로 차단"하는데(CORS 정책),
-// 이 헤더가 없으면 프론트엔드의 fetch() 호출이 브라우저 단에서 막혀버린다.
-// 지금은 로컬 개발 단계라 "*"(모든 출처 허용)로 단순하게 열어뒀지만,
-// 실제 배포 시에는 프론트엔드가 서빙되는 정확한 출처로 좁히는 것이 안전하다.
+// withCORS: 브라우저(대시보드)가 다른 출처에서 이 API를 호출할 수 있도록 CORS 헤더를 붙인다.
+// 지금은 로컬 개발 단계라 "*"로 열어뒀지만, 배포 시에는 정확한 출처로 좁히는 것이 안전하다.
 func withCORS(next http.HandlerFunc) http.HandlerFunc {
 	return func(w http.ResponseWriter, r *http.Request) {
 		w.Header().Set("Access-Control-Allow-Origin", "*")
 		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
 		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
 		if r.Method == http.MethodOptions {
-			// 브라우저가 실제 요청 전에 미리 보내는 preflight 요청 - 본 로직 없이 200만 반환
+			// 브라우저 preflight 요청 - 본 로직 없이 200만 반환
 			w.WriteHeader(http.StatusOK)
 			return
 		}
@@ -225,11 +219,14 @@ func (g *Gateway) runSession(req ExecuteRequest, state *SessionState) {
 	resultCh := make(chan string, 1)
 	errCh := make(chan error, 1)
 
+	// onStats: DockerExecutor가 주기적으로 호출하는 콜백. 실행 도중 세션 상태에
+	// CPU/메모리 실측치를 반영해서, 대시보드가 폴링할 때마다 최신 값을 볼 수 있게 한다.
+	onStats := func(cpu, mem string) {
+		g.updateSessionStats(req.SessionID, cpu, mem)
+	}
+
 	go func() {
-		// 실제 실행은 executor(지금은 MockExecutor, 나중엔 DockerExecutor)에 완전히 위임한다.
-		// 게이트웨이 코드는 "무엇을 실행하는지"를 전혀 알 필요가 없다 -
-		// 이것이 인터페이스로 분리한 핵심 이점이다.
-		result, err := g.executor.Run(ctx, req.Tool, req.Args)
+		result, err := g.executor.Run(ctx, req.Tool, req.Args, onStats)
 		if err != nil {
 			errCh <- err
 			return
@@ -237,21 +234,58 @@ func (g *Gateway) runSession(req ExecuteRequest, state *SessionState) {
 		resultCh <- result
 	}()
 
+	// 포인트: 최종 상태는 최신 스냅샷(모니터링 수치가 반영된 것)을 "복사"해서 만든다.
+	// 기존 state 포인터를 직접 수정하면 (1) updateSessionStats가 저장한 CPU/MEM 값이
+	// 덮어써져 사라지고 (2) 대시보드가 JSON 인코딩 중인 객체를 동시에 쓰는 경쟁 상태가 생긴다.
+	var final SessionState
 	select {
 	case result := <-resultCh:
-		state.Status = "completed"
-		state.Result = result
+		final = g.snapshot(req.SessionID, state)
+		final.Status = "completed"
+		final.Result = result
 		log.Printf("[완료] session=%s", req.SessionID)
 	case err := <-errCh:
-		state.Status = "failed"
-		state.Error = err.Error()
+		final = g.snapshot(req.SessionID, state)
+		final.Status = "failed"
+		final.Error = err.Error()
 		log.Printf("[실패] session=%s: %v", req.SessionID, err)
 	case <-ctx.Done():
-		state.Status = "timeout"
-		state.Error = "실행 시간 초과 (하드 타임아웃)"
+		final = g.snapshot(req.SessionID, state)
+		final.Status = "timeout"
+		final.Error = "실행 시간 초과 (하드 타임아웃)"
 		log.Printf("[타임아웃] session=%s", req.SessionID)
 	}
-	g.sessions.Store(req.SessionID, state)
+	g.sessions.Store(req.SessionID, &final)
+}
+
+// snapshot: sync.Map에 저장된 현재 세션 상태의 값 복사본을 반환한다.
+func (g *Gateway) snapshot(sessionID string, fallback *SessionState) SessionState {
+	if v, ok := g.sessions.Load(sessionID); ok {
+		if s, ok := v.(*SessionState); ok {
+			return *s
+		}
+	}
+	return *fallback
+}
+
+// updateSessionStats: 실행 도중 CPU/메모리 실측치로 세션 상태를 갱신한다.
+//
+// 포인트: state 포인터의 필드를 직접 수정하지 않고, 기존 값을 복사해서 새 필드만
+// 바꾼 뒤 새 포인터로 다시 Store한다. 두 goroutine이 같은 메모리를 동시에 쓰는
+// 경쟁 상태를 피하기 위함이며, "재사용 대신 새로 만들어 교체" 원칙과도 일치한다.
+func (g *Gateway) updateSessionStats(sessionID, cpu, mem string) {
+	v, ok := g.sessions.Load(sessionID)
+	if !ok {
+		return
+	}
+	current, ok := v.(*SessionState)
+	if !ok {
+		return
+	}
+	updated := *current // 값 복사 (필드가 전부 string/time이라 얕은 복사로 충분)
+	updated.CPUUsage = cpu
+	updated.MemUsage = mem
+	g.sessions.Store(sessionID, &updated)
 }
 
 // ===== 유틸 =====
@@ -264,10 +298,9 @@ func writeJSON(w http.ResponseWriter, status int, v interface{}) {
 
 func main() {
 	// ③ 인프라/보안 계층 연동: DockerExecutor를 우선 시도하고,
-	// Docker 데몬 연결 자체가 안 되면(개발 초기 단계 등) MockExecutor로 자동 폴백한다.
+	// Docker 데몬 연결 자체가 안 되면 MockExecutor로 자동 폴백한다.
 	// 포인트: 이 폴백은 "개발 편의"일 뿐이다. 실제 배포 시에는 폴백 없이
-	// DockerExecutor 생성 실패를 즉시 log.Fatal로 처리해, 격리 없이 서비스가
-	// 떠 있는 상황(=보안 사고)을 방지하는 것이 맞다.
+	// log.Fatal로 처리해, 격리 없이 서비스가 떠 있는 상황(=보안 사고)을 방지하는 것이 맞다.
 	var executor SandboxExecutor
 	dockerExec, err := NewDockerExecutor()
 	if err != nil {
@@ -286,14 +319,13 @@ func main() {
 
 	gw := NewGateway(executor)
 
-	// 포인트: 프론트엔드(대시보드) 추가에 맞춰 모든 라우트를 withCORS로 감쌌다.
-	// gateway_client.py 같은 서버 간 통신은 CORS의 영향을 받지 않으므로
-	// (CORS는 브라우저에서만 강제되는 정책), 이 변경이 기존 Python 클라이언트
-	// 동작에는 전혀 영향을 주지 않는다.
+	// withCFAccess로 한 번 더 감싸서, Cloudflare Access를 통과한 요청인지 게이트웨이
+	// 스스로도 검증하게 한다 (엣지에서만 막고 서버는 무방비인 상태를 피하기 위함).
+	// /healthz는 예외: 민감한 정보를 노출하지 않으므로 인증을 요구하지 않는다.
 	mux := http.NewServeMux()
-	mux.HandleFunc("/v1/execute", withCORS(gw.handleExecute))
-	mux.HandleFunc("/v1/sessions", withCORS(gw.handleSessionStatus))
-	mux.HandleFunc("/v1/sessions/all", withCORS(gw.handleSessionsAll))
+	mux.HandleFunc("/v1/execute", withCORS(withCFAccess(gw.handleExecute)))
+	mux.HandleFunc("/v1/sessions", withCORS(withCFAccess(gw.handleSessionStatus)))
+	mux.HandleFunc("/v1/sessions/all", withCORS(withCFAccess(gw.handleSessionsAll)))
 	mux.HandleFunc("/healthz", withCORS(gw.handleHealth))
 
 	log.Printf("이벤트 중계 API 게이트웨이 시작: :8080 (executor=%T)", executor)
diff --git a/gateway/go.mod b/gateway/go.mod
index 7bb3be3..be697fe 100644
--- a/gateway/go.mod
+++ b/gateway/go.mod
@@ -1,22 +1,3 @@
 module gateway
 
-go 1.26.5
-
-require github.com/docker/docker v24.0.7+incompatible
-
-require (
-	github.com/Microsoft/go-winio v0.6.2 // indirect
-	github.com/distribution/reference v0.6.0 // indirect
-	github.com/docker/distribution v2.8.3+incompatible // indirect
-	github.com/docker/go-connections v0.8.0 // indirect
-	github.com/docker/go-units v0.5.0 // indirect
-	github.com/gogo/protobuf v1.3.2 // indirect
-	github.com/moby/term v0.5.2 // indirect
-	github.com/morikuni/aec v1.1.0 // indirect
-	github.com/opencontainers/go-digest v1.0.0 // indirect
-	github.com/opencontainers/image-spec v1.1.1 // indirect
-	github.com/pkg/errors v0.9.1 // indirect
-	golang.org/x/sys v0.12.0 // indirect
-	golang.org/x/time v0.15.0 // indirect
-	gotest.tools/v3 v3.5.2 // indirect
-)
+go 1.22
diff --git a/gateway/go.sum b/gateway/go.sum
deleted file mode 100644
index fd56106..0000000
--- a/gateway/go.sum
+++ /dev/null
@@ -1,63 +0,0 @@
-github.com/Azure/go-ansiterm v0.0.0-20250102033503-faa5f7b0171c h1:udKWzYgxTojEKWjV8V+WSxDXJ4NFATAsZjh8iIbsQIg=
-github.com/Azure/go-ansiterm v0.0.0-20250102033503-faa5f7b0171c/go.mod h1:xomTg63KZ2rFqZQzSB4Vz2SUXa1BpHTVz9L5PTmPC4E=
-github.com/Microsoft/go-winio v0.6.2 h1:F2VQgta7ecxGYO8k3ZZz3RS8fVIXVxONVUPlNERoyfY=
-github.com/Microsoft/go-winio v0.6.2/go.mod h1:yd8OoFMLzJbo9gZq8j5qaps8bJ9aShtEA8Ipt1oGCvU=
-github.com/distribution/reference v0.6.0 h1:0IXCQ5g4/QMHHkarYzh5l+u8T3t73zM5QvfrDyIgxBk=
-github.com/distribution/reference v0.6.0/go.mod h1:BbU0aIcezP1/5jX/8MP0YiH4SdvB5Y4f/wlDRiLyi3E=
-github.com/docker/distribution v2.8.3+incompatible h1:AtKxIZ36LoNK51+Z6RpzLpddBirtxJnzDrHLEKxTAYk=
-github.com/docker/distribution v2.8.3+incompatible/go.mod h1:J2gT2udsDAN96Uj4KfcMRqY0/ypR+oyYUYmja8H+y+w=
-github.com/docker/docker v24.0.7+incompatible h1:Wo6l37AuwP3JaMnZa226lzVXGA3F9Ig1seQen0cKYlM=
-github.com/docker/docker v24.0.7+incompatible/go.mod h1:eEKB0N0r5NX/I1kEveEz05bcu8tLC/8azJZsviup8Sk=
-github.com/docker/go-connections v0.8.0 h1:T9UlP76qPLA/HaLrcC+s4Doqqv5XsWMMUGPF5Aih/k0=
-github.com/docker/go-connections v0.8.0/go.mod h1:no1qkHdjq7kLMGUXYAduOhYPSJxxvgWBh7ogVvptn3Q=
-github.com/docker/go-units v0.5.0 h1:69rxXcBk27SvSaaxTtLh/8llcHD8vYHT7WSdRZ/jvr4=
-github.com/docker/go-units v0.5.0/go.mod h1:fgPhTUdO+D/Jk86RDLlptpiXQzgHJF7gydDDbaIK4Dk=
-github.com/gogo/protobuf v1.3.2 h1:Ov1cvc58UF3b5XjBnZv7+opcTcQFZebYjWzi34vdm4Q=
-github.com/gogo/protobuf v1.3.2/go.mod h1:P1XiOD3dCwIKUDQYPy72D8LYyHL2YPYrpS2s69NZV8Q=
-github.com/google/go-cmp v0.5.9 h1:O2Tfq5qg4qc4AmwVlvv0oLiVAGB7enBSJ2x2DqQFi38=
-github.com/google/go-cmp v0.5.9/go.mod h1:17dUlkBOakJ0+DkrSSNjCkIjxS6bF9zb3elmeNGIjoY=
-github.com/kisielk/errcheck v1.5.0/go.mod h1:pFxgyoBC7bSaBwPgfKdkLd5X25qrDl4LWUI2bnpBCr8=
-github.com/kisielk/gotool v1.0.0/go.mod h1:XhKaO+MFFWcvkIS/tQcRk01m1F5IRFswLeQ+oQHNcck=
-github.com/moby/term v0.5.2 h1:6qk3FJAFDs6i/q3W/pQ97SX192qKfZgGjCQqfCJkgzQ=
-github.com/moby/term v0.5.2/go.mod h1:d3djjFCrjnB+fl8NJux+EJzu0msscUP+f8it8hPkFLc=
-github.com/morikuni/aec v1.1.0 h1:vBBl0pUnvi/Je71dsRrhMBtreIqNMYErSAbEeb8jrXQ=
-github.com/morikuni/aec v1.1.0/go.mod h1:xDRgiq/iw5l+zkao76YTKzKttOp2cwPEne25HDkJnBw=
-github.com/opencontainers/go-digest v1.0.0 h1:apOUWs51W5PlhuyGyz9FCeeBIOUDA/6nW8Oi/yOhh5U=
-github.com/opencontainers/go-digest v1.0.0/go.mod h1:0JzlMkj0TRzQZfJkVvzbP0HBR3IKzErnv2BNG4W4MAM=
-github.com/opencontainers/image-spec v1.1.1 h1:y0fUlFfIZhPF1W537XOLg0/fcx6zcHCJwooC2xJA040=
-github.com/opencontainers/image-spec v1.1.1/go.mod h1:qpqAh3Dmcf36wStyyWU+kCeDgrGnAve2nCC8+7h8Q0M=
-github.com/pkg/errors v0.9.1 h1:FEBLx1zS214owpjy7qsBeixbURkuhQAwrK5UwLGTwt4=
-github.com/pkg/errors v0.9.1/go.mod h1:bwawxfHBFNV+L2hUp1rHADufV3IMtnDRdf1r5NINEl0=
-github.com/yuin/goldmark v1.1.27/go.mod h1:3hX8gzYuyVAZsxl0MRgGTJEmQBFcNTphYh9decYSb74=
-github.com/yuin/goldmark v1.2.1/go.mod h1:3hX8gzYuyVAZsxl0MRgGTJEmQBFcNTphYh9decYSb74=
-golang.org/x/crypto v0.0.0-20190308221718-c2843e01d9a2/go.mod h1:djNgcEr1/C05ACkg1iLfiJU5Ep61QUkGW8qpdssI0+w=
-golang.org/x/crypto v0.0.0-20191011191535-87dc89f01550/go.mod h1:yigFU9vqHzYiE8UmvKecakEJjdnWj3jj499lnFckfCI=
-golang.org/x/crypto v0.0.0-20200622213623-75b288015ac9/go.mod h1:LzIPMQfyMNhhGPhUkYOs5KpL4U8rLKemX1yGLhDgUto=
-golang.org/x/mod v0.2.0/go.mod h1:s0Qsj1ACt9ePp/hMypM3fl4fZqREWJwdYDEqhRiZZUA=
-golang.org/x/mod v0.3.0/go.mod h1:s0Qsj1ACt9ePp/hMypM3fl4fZqREWJwdYDEqhRiZZUA=
-golang.org/x/net v0.0.0-20190404232315-eb5bcb51f2a3/go.mod h1:t9HGtf8HONx5eT2rtn7q6eTqICYqUVnKs3thJo3Qplg=
-golang.org/x/net v0.0.0-20190620200207-3b0461eec859/go.mod h1:z5CRVTTTmAJ677TzLLGU+0bjPO0LkuOLi4/5GtJWs/s=
-golang.org/x/net v0.0.0-20200226121028-0de0cce0169b/go.mod h1:z5CRVTTTmAJ677TzLLGU+0bjPO0LkuOLi4/5GtJWs/s=
-golang.org/x/net v0.0.0-20201021035429-f5854403a974/go.mod h1:sp8m0HH+o8qH0wwXwYZr8TS3Oi6o0r6Gce1SSxlDquU=
-golang.org/x/sync v0.0.0-20190423024810-112230192c58/go.mod h1:RxMgew5VJxzue5/jJTE5uejpjVlOe/izrB70Jof72aM=
-golang.org/x/sync v0.0.0-20190911185100-cd5d95a43a6e/go.mod h1:RxMgew5VJxzue5/jJTE5uejpjVlOe/izrB70Jof72aM=
-golang.org/x/sync v0.0.0-20201020160332-67f06af15bc9/go.mod h1:RxMgew5VJxzue5/jJTE5uejpjVlOe/izrB70Jof72aM=
-golang.org/x/sys v0.0.0-20190215142949-d0b11bdaac8a/go.mod h1:STP8DvDyc/dI5b8T5hshtkjS+E42TnysNCUPdjciGhY=
-golang.org/x/sys v0.0.0-20190412213103-97732733099d/go.mod h1:h1NjWce9XRLGQEsW7wpKNCjG9DtNlClVuFLEZdDNbEs=
-golang.org/x/sys v0.0.0-20200930185726-fdedc70b468f/go.mod h1:h1NjWce9XRLGQEsW7wpKNCjG9DtNlClVuFLEZdDNbEs=
-golang.org/x/sys v0.12.0 h1:CM0HF96J0hcLAwsHPJZjfdNzs0gftsLfgKt57wWHJ0o=
-golang.org/x/sys v0.12.0/go.mod h1:oPkhp1MJrh7nUepCBck5+mAzfO9JrbApNNgaTdGDITg=
-golang.org/x/text v0.3.0/go.mod h1:NqM8EUOU14njkJ3fqMW+pc6Ldnwhi/IjpwHt7yyuwOQ=
-golang.org/x/text v0.3.3/go.mod h1:5Zoc/QRtKVWzQhOtBMvqHzDpF6irO9z98xDceosuGiQ=
-golang.org/x/time v0.15.0 h1:bbrp8t3bGUeFOx08pvsMYRTCVSMk89u4tKbNOZbp88U=
-golang.org/x/time v0.15.0/go.mod h1:Y4YMaQmXwGQZoFaVFk4YpCt4FLQMYKZe9oeV/f4MSno=
-golang.org/x/tools v0.0.0-20180917221912-90fa682c2a6e/go.mod h1:n7NCudcB/nEzxVGmLbDWY5pfWTLqBcC2KZ6jyYvM4mQ=
-golang.org/x/tools v0.0.0-20191119224855-298f0cb1881e/go.mod h1:b+2E5dAYhXwXZwtnZ6UAqBI28+e2cm9otk0dWdXHAEo=
-golang.org/x/tools v0.0.0-20200619180055-7c47624df98f/go.mod h1:EkVYQZoAsY45+roYkvgYkIh4xh/qjgUK9TdY2XT94GE=
-golang.org/x/tools v0.0.0-20210106214847-113979e3529a/go.mod h1:emZCQorbCU4vsT4fOWvOPXz4eW1wZW4PmDk9uLelYpA=
-golang.org/x/xerrors v0.0.0-20190717185122-a985d3407aa7/go.mod h1:I/5z698sn9Ka8TeJc9MKroUUfqBBauWjQqLJ2OPfmY0=
-golang.org/x/xerrors v0.0.0-20191011141410-1b5146add898/go.mod h1:I/5z698sn9Ka8TeJc9MKroUUfqBBauWjQqLJ2OPfmY0=
-golang.org/x/xerrors v0.0.0-20191204190536-9bdfabe68543/go.mod h1:I/5z698sn9Ka8TeJc9MKroUUfqBBauWjQqLJ2OPfmY0=
-golang.org/x/xerrors v0.0.0-20200804184101-5ec99f83aff1/go.mod h1:I/5z698sn9Ka8TeJc9MKroUUfqBBauWjQqLJ2OPfmY0=
-gotest.tools/v3 v3.5.2 h1:7koQfIKdy+I8UTetycgUqXWSDwpgv193Ka+qRsmBY8Q=
-gotest.tools/v3 v3.5.2/go.mod h1:LtdLGcnqToBH83WByAAi/wiwSFCArdFIUV/xxN4pcjA=
diff --git a/gateway/sandbox-python-datasci.Dockerfile b/gateway/sandbox-python-datasci.Dockerfile
new file mode 100644
index 0000000..c7de00c
--- /dev/null
+++ b/gateway/sandbox-python-datasci.Dockerfile
@@ -0,0 +1,31 @@
+# sandbox-python-datasci.Dockerfile
+#
+# 데이터셋 분석/시각화 시나리오 전용 이미지.
+#
+# 왜 별도 이미지가 필요한가:
+#   run_code 컨테이너는 --network none으로 실행되므로, 실행 도중에는
+#   pip install이 원천적으로 불가능하다. 그래서 pandas/matplotlib/numpy를
+#   "이미지 빌드 시점"에 미리 넣어둔다 - 빌드는 인터넷이 연결된 상태로 한 번만 하고,
+#   실제 코드 실행(컨테이너 런타임)은 여전히 완전히 네트워크 차단 상태로 유지된다.
+#
+# 빌드:
+#   docker build -t sandbox-python-datasci:latest -f sandbox-python-datasci.Dockerfile .
+FROM python:3.12-slim
+
+# 한글 폰트 설치: matplotlib 기본 폰트(DejaVu Sans)는 한글 글리프가 없어서
+# 그래프에 한글 라벨을 쓰면 빈 네모(tofu)로 깨진다.
+RUN apt-get update && apt-get install -y --no-install-recommends fonts-nanum \
+    && rm -rf /var/lib/apt/lists/* \
+    && fc-cache -fv
+
+RUN pip install --no-cache-dir \
+    pandas==2.2.2 \
+    matplotlib==3.9.0 \
+    numpy==1.26.4
+
+# 디스플레이가 없는 컨테이너이므로 Agg 백엔드(이미지/메모리 출력 전용)로 고정한다.
+ENV MPLBACKEND=Agg
+
+# --read-only 루트 파일시스템이라 $HOME/.config/matplotlib에 쓸 수 없다.
+# 항상 --tmpfs로 마운트되는 /tmp를 캐시 위치로 명시해 불필요한 경고를 없앤다.
+ENV MPLCONFIGDIR=/tmp/matplotlib-cache
-- 
2.43.0
