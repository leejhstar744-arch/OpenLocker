# 🛡️ OpenLocker

> **피지컬 AI 로컬 온디바이스 환경을 위한 자원 최적화 및 Docker 샌드박스 보안 인프라 구축**
> OpenLocker 스터디랩 · GPU 없는 Ubuntu 노트북(CPU 전용 추론)에서 동작하는 로컬 AI 에이전트 실행 샌드박스

로컬 LLM이 생성한 코드를 **안전하게** 실행하기 위한 4계층 파이프라인입니다.
핵심 설계 원칙은 하나입니다.

> **"생각하는 부분(LLM)"과 "행동하는 부분(코드 실행)"을 서로 다른 신뢰 경계에 둔다.**
> LLM은 텍스트만 생성하므로 위험하지 않습니다. 위험은 *그 텍스트를 실행하는 순간* 시작됩니다.

```
사용자 → [① AI/LLM]  Ollama(llama3.2:3b) → Tool Call 파서(화이트리스트)
       → [② 백엔드]   Go 이벤트 중계 API (세마포어 · 30초 타임아웃 · Access JWT 검증)
       → [③ 인프라]   Docker 웜스탠바이 컨테이너 + gVisor(runsc) + cgroup 자원 제한
       → 결과 반환 → [④ 프론트엔드] 채팅 + 실시간 자원 모니터링 대시보드
```

---

## 📊 목표 달성도 (원안 대비)

| 원안 목표 | 결과 | 비고 |
|---|---|---|
| 지능형: GPU(BC-250) + 26B 모델, 40~50 t/s | ❌ 미달성 | 실제 환경이 GPU 없는 노트북 → CPU 추론(`llama3.2:3b`)으로 스코프 재정의 |
| 보안형: Cgroups + **gVisor(runsc)** | ✅ 달성 | `docker exec <id> dmesg`로 gVisor 부팅 로그 직접 확인 |
| 네트워크: Cloudflare Tunnel + Zero Trust | ⏳ 코드 완료 | `cf_access_middleware.go` 구현, 도메인 확보 후 활성화 예정 |
| 데이터셋 분석/시각화 시나리오 검증 | ✅ 달성 | pandas + matplotlib 그래프가 브라우저에 렌더링됨 |

엔드투엔드 검증 예: `"1부터 10까지 더해줘"` → Ollama → 파서 → Go 게이트웨이 → Docker 컨테이너 → **`55`** 반환.

---

## 📁 폴더 구조

```
OpenLocker/
├── ai_llm_layer/                       # Python: ① AI/LLM · ④ 프론트엔드 · 연구 도구
│   ├── tool_call_parser.py             # Tool Call 추출 + 화이트리스트 검증 (첫 번째 보안 검문소)
│   ├── ai_llm_layer.py                 # 시스템 프롬프트(few-shot) + Ollama 호출 + 파서 연결
│   ├── gateway_client.py               # Go 게이트웨이 제출/폴링 클라이언트
│   ├── chat_bridge.py                  # 브라우저 ↔ 파이프라인 브리지 (:8000)
│   ├── dashboard.html                  # 채팅 + 세션 모니터링 + Canvas 자원 그래프
│   ├── benchmark.py                    # llm / pipeline / concurrency / compare 벤치마크
│   ├── test_datasci_direct.py          # LLM 우회, 인프라 단독 검증
│   ├── llm_results.csv                 # LLM 추론 지연시간 실측 데이터
│   └── pipeline_results.csv            # 파이프라인 엔드투엔드 실측 데이터
├── gateway/                            # Go: ② 백엔드 · ③ 인프라 · 네트워크
│   ├── event_relay_api.go              # 이벤트 중계 API, SandboxExecutor 인터페이스, main()
│   ├── docker_executor.go              # 웜스탠바이 풀 + gVisor + docker stats 모니터링
│   ├── cf_access_middleware.go         # Cloudflare Access JWT 서명 검증 미들웨어
│   ├── sandbox-python-datasci.Dockerfile  # pandas/matplotlib/나눔고딕 실행 이미지
│   └── go.mod                          # 표준 라이브러리만 사용 (외부 의존성 0개)
└── cgroup_setup.sh                     # 호스트 레벨 cgroup v2 코어 파티셔닝 (참고용)
```

---

## 🧩 코드 설명

### ① AI/LLM 계층

#### `tool_call_parser.py` — 첫 번째 보안 검문소
LLM 출력 텍스트에서 `{"tool": ..., "args": {...}}` 형태의 도구 호출을 찾아 검증합니다. 여기를 통과해야만 "실행 가능한 요청"이 됩니다.

- **JSON 추출 — 정규식 대신 `json.JSONDecoder().raw_decode()`**
  처음엔 정규식(`[^{}]*`)을 썼는데, `args` 안에 중괄호가 중첩된 JSON을 하나도 잡지 못해 **Tool Call 0건 통과** 버그가 났습니다. 지금은 텍스트의 모든 `{` 위치에서 `raw_decode`를 시도해 중첩 깊이와 무관하게 정확히 파싱하고, 코드펜스 유무도 신경 쓰지 않습니다.
- **화이트리스트 검증 (`ALLOWED_TOOLS`)** — ① 허용된 도구명인가 ② 필수 인자가 다 있는가 ③ 모르는 인자가 섞이지 않았는가(프롬프트 인젝션 대비) ④ `run_code` 코드 길이 ≤ 20KB
- **"규칙 위반은 거부, 형식 문제는 보정" 원칙** — `timeout_sec`은 게이트웨이와 동일한 1~30초로 클램프하고, `"5000ms"` 같은 문자열은 숫자를 추출하며, 해석 불가하면 필드만 제거하고 요청은 살립니다. `True`가 `1`로 둔갑하지 않도록 `bool`을 따로 걸러냅니다.

#### `ai_llm_layer.py` — 프롬프트 엔지니어링 + Ollama 연동
- `SYSTEM_PROMPT`: 파서의 `ALLOWED_TOOLS`와 1:1로 동기화된 도구 목록 + **few-shot 예시 3개** (덧셈 · 정렬 · pandas/matplotlib 시각화)
- 3B 소형 모델이 실제로 저지른 실수를 규칙으로 막았습니다: 코드 안 큰따옴표 금지(JSON 깨짐), 한 줄에 한 문장, 존재하지 않는 파일 읽기 금지, `set_xticks`로 x축 라벨 고정, 그래프는 `PLOT_BASE64:` 접두사로 stdout 출력
- `query_llm()`: `stream=False`(청크로 받으면 JSON이 잘림), 연결 오류만 점진적 백오프로 재시도, HTTP 오류는 즉시 실패
- `handle_user_request()` → `LayerResult` → `to_gateway_payload()`로 Go 게이트웨이 스키마에 맞춰 변환
- `looks_like_failed_tool_call()`: "도구 호출을 시도했지만 형식이 깨진 응답"과 "일반 대화"를 구분해 UX를 분기

### ② 백엔드/파이프라인 계층

#### `event_relay_api.go` — 유일한 진입점(문지기)
| 엔드포인트 | 역할 |
|---|---|
| `POST /v1/execute` | 실행 요청 접수 → 202(running) 또는 429(동시성 초과) |
| `GET /v1/sessions?id=` | 단일 세션 상태 폴링 |
| `GET /v1/sessions/all` | 대시보드용 전체 세션 목록 (최신순) |
| `GET /healthz` | 생존 확인 (인증 없음) |

- **`SandboxExecutor` 인터페이스** — 게이트웨이는 "무엇을 실행하는지" 모릅니다. `MockExecutor`로 파이프라인을 먼저 검증한 뒤 `DockerExecutor`로 **핸들러 코드 수정 없이** 교체했습니다. Docker 연결 실패 시 Mock으로 자동 폴백합니다(개발용).
- **동시성 제어** — 버퍼드 채널(`chan struct{}`, 크기 2)을 세마포어로 사용. 초과 요청은 큐잉하지 않고 **즉시 429** (온디바이스 CPU 보호 우선).
- **하드 타임아웃** — `context.WithTimeout(30s)`, 초과 시 `status: "timeout"`.
- **세션 상태** — `sync.Map`. 실시간 CPU/MEM 갱신(`updateSessionStats`)과 최종 상태 기록 모두 **기존 포인터를 수정하지 않고 복사본을 새로 저장**해 goroutine 간 경쟁 상태를 피합니다.
- 미들웨어 체인: `withCORS(withCFAccess(handler))`

#### `gateway_client.py` — Python → Go 연결
`submit_execution()`(202가 아니면 `GatewayError`) → `poll_until_done()`(0.5초 간격, 35초 상한 — 게이트웨이 30초 타임아웃보다 살짝 길게) → `ExecutionResult`.

### ③ 인프라/보안 계층

#### `docker_executor.go` — 샌드박스 매니저
- **Docker Go SDK를 버리고 `os/exec`로 CLI 호출** — SDK의 `docker/distribution` ↔ `distribution/reference` 버전 충돌(`undefined: reference.SplitHostname`)을 근본적으로 피했습니다. 결과적으로 **표준 라이브러리만으로 빌드**됩니다.
- **진짜 웜스탠바이** — `sleep infinity` 상태의 idle 컨테이너 1개를 미리 띄워두고, 요청이 오면 `docker exec`로 코드만 주입합니다. 컨테이너 생성 비용이 요청 경로에서 사라집니다.
- **재사용 금지 원칙 유지** — 한 번이라도 코드를 실행한 컨테이너는 결과와 무관하게 `docker rm -f`로 폐기하고(`destroyAndReplenish`), 백그라운드에서 새 idle 컨테이너를 채웁니다. *재사용은 대기 상태까지만.*
- **gVisor(runsc)** — 시작 시 `docker info`로 runsc를 자동 감지해 idle 컨테이너 생성 시에만 `--runtime runsc`를 붙입니다. `SANDBOX_RUNTIME=runc|runsc`로 강제 지정 가능(A/B 벤치마크용). 미설치 시 경고 후 runc로 계속 동작합니다.
- **격리 옵션** (심층 방어)

  | 옵션 | 값 | 목적 |
  |---|---|---|
  | `--network` | `none` | 네트워크 완전 차단 |
  | `--cpuset-cpus` | `2,3` | LLM(0-1)과 코어 분리 |
  | `--memory` | `512m` | 메모리 폭주 차단 |
  | `--pids-limit` | `64` | 포크폭탄 방지 |
  | `--read-only` + `--tmpfs /tmp` | | 루트 FS 변조 방지 |
  | `--cap-drop ALL` | | 리눅스 권한 전부 제거 |
  | `--security-opt no-new-privileges` | | setuid 권한 상승 차단 |

- **명령 주입 방지** — 코드는 `python3 -c <code>`의 단일 argv로 전달되어 셸을 거치지 않습니다.
- **성공 판정 = exit code** — 예전엔 "stderr에 뭐라도 있으면 실패"였는데, matplotlib 캐시 경고 같은 정상 출력까지 실패로 오판했습니다. 지금은 종료 코드로만 판정하고, 성공 시 stderr는 서버 로그에만 남깁니다.
- **실시간 모니터링** — 실행 중 `docker stats --no-stream`을 500ms 간격으로 폴링해 `StatsCallback`으로 게이트웨이에 보고합니다.

#### `sandbox-python-datasci.Dockerfile`
`python:3.12-slim` + pandas/matplotlib/numpy(버전 고정) + 나눔고딕. 런타임은 `--network none`이라 `pip install`이 불가능하므로 **빌드 시점에 미리 설치**합니다. `MPLBACKEND=Agg`(디스플레이 없음), `MPLCONFIGDIR=/tmp/...`(읽기 전용 루트 FS 대응).

#### `cgroup_setup.sh`
cgroup v2로 `llm`(코어 0-1, 4G) / `sandbox`(코어 2-3, 2G) 그룹을 만듭니다. systemd cgroup 드라이버와 `--cgroup-parent`가 충돌해 실제 제한은 컨테이너 레벨 옵션이 담당하므로, **호스트 레벨 참고용**입니다.

### 🌐 네트워크 계층

#### `cf_access_middleware.go` — Cloudflare Access JWT 검증
엣지(Cloudflare)에서 막는 것만 믿으면 터널을 우회한 직접 접근에 무방비입니다. 그래서 게이트웨이가 `Cf-Access-Jwt-Assertion` 헤더를 직접 검증합니다.
- JWKS(`/cdn-cgi/access/certs`)를 1시간 캐싱, `kid` 미스 시 자동 재조회(키 로테이션 대응)
- `alg`를 RS256으로 고정 → RSA PKCS#1 v1.5 서명 검증 → `aud` 일치 → `exp` 만료 확인
- `CF_ACCESS_TEAM_DOMAIN`/`CF_ACCESS_AUD` 미설정 시 **경고 로그와 함께 로컬 모드**로 동작
- 인증 성공 시 이메일을 감사 로그로 남김

### ④ 프론트엔드

#### `chat_bridge.py` — 우회 경로 차단
브라우저가 Ollama나 게이트웨이를 직접 호출하면 파서의 화이트리스트 검증을 건너뛸 수 있습니다. 그래서 **브라우저는 반드시 이 브리지(`POST /chat`)를 거치도록** 했습니다. 응답 상태는 `completed / failed / timeout / no_tool_call / parse_failed`로 구분됩니다.

#### `dashboard.html` — 오프라인 콘솔
- 좌측 채팅: 결과 안의 `PLOT_BASE64:` 위치를 `indexOf`로 찾아 텍스트(`df.describe()`)와 그래프 이미지를 분리 렌더링
- 우측 모니터링: 1.5초마다 `/v1/sessions/all` 폴링, 상태별 색상 카드
- **Canvas 자원 그래프** — Chart.js를 CDN으로 불러오면 오프라인 전제가 깨지므로 `<canvas>`로 직접 그렸습니다 (외부 의존성 0개)
- `window.location.hostname` 기반 호스트 감지 → 로컬/원격 어디서 열어도 코드 수정 불필요

### 🔬 연구 도구

| 파일 | 설명 |
|---|---|
| `benchmark.py llm` | Ollama 순수 추론 지연시간 (웜업 제외 후 평균/표준편차/중앙값) |
| `benchmark.py pipeline` | LLM → Docker 엔드투엔드 지연시간 |
| `benchmark.py concurrency` | N개 동시 요청 → 202가 2개를 넘지 않는지 세마포어 검증 |
| `benchmark.py compare` | 두 CSV(runc vs runsc) A/B 비교, 평균 차이 < 표준편차면 경고 |
| `test_datasci_direct.py` | LLM을 건너뛰고 검증된 코드를 직접 제출 → 문제가 LLM 쪽인지 인프라 쪽인지 분리 진단 |

---

## 🚀 실행 방법

```bash
# 0) 사전 준비
curl -fsSL https://ollama.com/install.sh | sh && ollama pull llama3.2:3b
pip install requests --break-system-packages
cd gateway && docker build -t sandbox-python-datasci:latest -f sandbox-python-datasci.Dockerfile .
# (선택) gVisor 설치: https://gvisor.dev/docs/user_guide/install/

# 1) 터미널 1 — Go 게이트웨이 (:8080)
cd gateway && go run .
#   SANDBOX_RUNTIME=runc go run .   ← gVisor 끄고 베이스라인 측정

# 2) 터미널 2 — 채팅 브리지 (:8000)
cd ai_llm_layer && python3 chat_bridge.py

# 3) 터미널 3 — 대시보드 정적 서버 (:8090)
cd ai_llm_layer && python3 -m http.server 8090
#   브라우저: http://localhost:8090/dashboard.html
```

gVisor 적용 확인:
```bash
docker ps                          # sleep infinity 상태의 idle 컨테이너 확인
docker exec <container_id> dmesg   # "Starting gVisor..." 가 보이면 적용된 것
```

---

## ⚠️ 알려진 한계 (솔직하게)

- **gVisor 성능 오버헤드 미확정** — `pipeline` 모드로 두 번 A/B 측정했더니 1차 **+6.8%(느림)**, 2차 **-13.9%(빠름)**로 정반대 결론이 나왔습니다. LLM 추론 시간 변동(5.6~12.9초)이 gVisor 오버헤드(수십~수백 ms) 신호를 덮어버린 **측정 방법론의 결함**입니다. LLM을 배제하고 컨테이너 계층만 측정하는 벤치마크 모드가 필요합니다.
- **한글 그래프 라벨** — 나눔고딕 설치 + `rcParams` 지시에도 완전히 해결되지 않았습니다 (폰트 캐시 생성 시점 불일치 또는 LLM의 설정 누락 추정).
- **프롬프트 인젝션** — 실행 단계 격리만 다루며, LLM이 위험한 코드를 "생성"하는 것 자체는 막지 못합니다.
- **코드 정확성** — 파서는 형식만 검증합니다. 문법 오류 코드는 실행 단계에서 `failed`로 걸러집니다.
- **커널 취약점 탈옥** — 목표는 완전 방지가 아니라 "탈옥해도 피해 최소화"입니다.
- **네트워크 계층** — Cloudflare 도메인 미확보로 실제 터널 연결 테스트는 보류 상태입니다.

---

## 🔗 참고 자료

- [cgroup v2 공식 문서](https://docs.kernel.org/admin-guide/cgroup-v2.html)
- [Docker `run` 레퍼런스](https://docs.docker.com/reference/cli/docker/container/run/)
- [gVisor 설치 가이드](https://gvisor.dev/docs/user_guide/install/)
- [Ollama API 문서](https://github.com/ollama/ollama/blob/main/docs/api.md)
- [Cloudflare Access JWT 검증 가이드](https://developers.cloudflare.com/cloudflare-one/identity/authorization-cookie/validating-json/)
- [JWT 표준 (RFC 7519)](https://datatracker.ietf.org/doc/html/rfc7519)
