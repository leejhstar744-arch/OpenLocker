// 이벤트 중계 API (Event Relay API)
// =====================================
// 에이전트 코어(Tool Call 파서 이후 단계)와 샌드박스 매니저 사이의
// 유일한 통신 경로 역할을 하는 Go 게이트웨이 서버다.
//
// 이 파일의 구조는 ② 백엔드/파이프라인 계층의 두 컴포넌트를 그대로 반영한다:
//   1) 이벤트 중계 API  -> Gateway 구조체와 HTTP 핸들러들
//   2) 실행 인터페이스  -> SandboxExecutor 인터페이스
//
// 왜 이 둘을 인터페이스로 분리했는가:
//   MockExecutor라는 가짜 구현체로 전체 파이프라인(AI/LLM -> 게이트웨이)을
//   먼저 검증한 뒤, 실제 Docker 실행 로직(DockerExecutor)은 SandboxExecutor
//   인터페이스만 구현해서 NewGateway에 주입했다. Gateway 구조체나 HTTP 핸들러
//   코드는 단 한 줄도 고칠 필요가 없었다.
//   이것이 "실행 인터페이스를 별도 계약으로 둔다"의 실질적 의미다.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"sort"
	"sync"
	"time"
)

// ===== 설정값 =====
const (
	// 동시에 실행 가능한 세션(=컨테이너) 개수의 상한.
	// cgroup 설계에서 샌드박스 전용으로 코어 2-3(2개)만 배정했으므로,
	// 이 숫자를 그 이상으로 잡으면 컨테이너들이 서로 CPU를 기다리게 된다.
	MaxConcurrentSessions = 2

	// 세션 하나가 허용되는 최대 실행 시간.
	// 포인트: tool_call_parser.py의 MAX_TIMEOUT_SEC(30)과 반드시 같은 값으로 유지할 것.
	SessionTimeout = 30 * time.Second
)

// ===== 요청/응답 구조체 =====

// ExecuteRequest: AI/LLM 계층(ai_llm_layer.py의 to_gateway_payload)이 만들어
// 보내는 실행 요청. 이 구조체에 도달했다는 것 자체가 "화이트리스트 검증은
// 이미 통과했다"는 의미이며, 게이트웨이는 그 위에 동시성/타임아웃 통제만 추가한다.
type ExecuteRequest struct {
	SessionID string                 `json:"session_id"`
	Tool      string                 `json:"tool"`
	Args      map[string]interface{} `json:"args"`
}

type ExecuteResponse struct {
	SessionID string `json:"session_id"`
	Status    string `json:"status"` // "running" | "rejected"
	Message   string `json:"message,omitempty"`
}

// SessionState: 세션 하나의 현재 상태.
// sync.Map에 이 구조체의 포인터가 저장되어 여러 goroutine이 동시에 갱신한다.
type SessionState struct {
	ID        string    `json:"id"`
	Tool      string    `json:"tool"`
	Status    string    `json:"status"` // "running" -> "completed" | "failed" | "timeout"
	StartedAt time.Time `json:"started_at"`
	Result    string    `json:"result,omitempty"`
	Error     string    `json:"error,omitempty"`
	CPUUsage  string    `json:"cpu_usage,omitempty"` // 예: "3.42%" (docker stats 실측치)
	MemUsage  string    `json:"mem_usage,omitempty"` // 예: "12.5MiB / 512MiB"
}

// -----------------------------------------------------------------------
// 실행 인터페이스 (백엔드/파이프라인 계층의 두 번째 컴포넌트)
// -----------------------------------------------------------------------

// StatsCallback: 실행 중인 컨테이너의 CPU/메모리 실측치를 주기적으로 보고하는 콜백.
// 포인트: "실행"과 "모니터링"의 책임을 분리하기 위해 Run()의 인자로 넘긴다.
// DockerExecutor는 주기적으로 이 콜백을 호출해 최신 수치를 흘려보내기만 하고,
// 그 값을 세션 상태에 저장하고 대시보드에 노출하는 건 게이트웨이의 책임이다.
type StatsCallback func(cpuPct string, memUsage string)

// SandboxExecutor: "샌드박스에 무엇을, 어떤 형식으로 넘길지"에 대한 계약(contract).
type SandboxExecutor interface {
	// Run: 도구 이름과 인자를 받아 실행하고, 결과 문자열 또는 에러를 반환한다.
	// ctx가 취소(타임아웃)되면 구현체는 반드시 실행 중인 프로세스/컨테이너를
	// 강제 종료해야 한다 (그렇지 않으면 자원 누수가 발생한다).
	// onStats: nil이 아니면 구현체가 실행 도중 주기적으로 CPU/메모리 실측치를 보고한다.
	Run(ctx context.Context, tool string, args map[string]interface{}, onStats StatsCallback) (string, error)
}

// MockExecutor: 실제 코드를 실행하지 않고, 받은 요청을 그대로 텍스트로 되돌려주는
// 임시 구현체. Docker가 없는 환경에서 파이프라인을 검증하기 위한 폴백으로 쓰인다.
type MockExecutor struct{}

func (m *MockExecutor) Run(ctx context.Context, tool string, args map[string]interface{}, onStats StatsCallback) (string, error) {
	select {
	case <-time.After(200 * time.Millisecond):
	case <-ctx.Done():
		return "", ctx.Err()
	}
	if onStats != nil {
		onStats("0.00%", "0MiB / 0MiB (mock)")
	}
	return fmt.Sprintf("[MOCK 실행 결과] tool=%s args=%v (실제 샌드박스 미연결 상태)", tool, args), nil
}

// ===== 게이트웨이 상태 =====

type Gateway struct {
	sem      chan struct{}   // 동시 세션 수 제한용 세마포어 (버퍼드 채널)
	sessions sync.Map        // sessionID -> *SessionState
	executor SandboxExecutor // 실제 실행을 위임할 대상
}

// NewGateway: executor를 외부에서 주입받는다 (의존성 주입).
//
//	gw := NewGateway(&MockExecutor{})   // 개발 초기
//	gw := NewGateway(dockerExecutor)    // 현재
func NewGateway(executor SandboxExecutor) *Gateway {
	return &Gateway{
		sem:      make(chan struct{}, MaxConcurrentSessions),
		executor: executor,
	}
}

// ===== 핸들러 =====

// handleExecute: POST /v1/execute
// 처리 흐름: 스키마 검증 -> 세마포어 논블로킹 획득 시도 -> 성공 시 비동기 실행 위임.
// 동시성 초과 시 큐잉하지 않고 즉시 429로 거부한다 (온디바이스 CPU 보호 우선).
func (g *Gateway) handleExecute(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req ExecuteRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, ExecuteResponse{Status: "rejected", Message: "잘못된 요청 형식"})
		return
	}
	if req.SessionID == "" || req.Tool == "" {
		writeJSON(w, http.StatusBadRequest, ExecuteResponse{Status: "rejected", Message: "session_id/tool 필수"})
		return
	}

	log.Printf("[요청 수신] session=%s tool=%s", req.SessionID, req.Tool)

	select {
	case g.sem <- struct{}{}:
		state := &SessionState{ID: req.SessionID, Tool: req.Tool, Status: "running", StartedAt: time.Now()}
		g.sessions.Store(req.SessionID, state)
		go g.runSession(req, state)
		writeJSON(w, http.StatusAccepted, ExecuteResponse{SessionID: req.SessionID, Status: "running"})
	default:
		log.Printf("[거부] session=%s: 동시 세션 한도 초과", req.SessionID)
		writeJSON(w, http.StatusTooManyRequests, ExecuteResponse{
			SessionID: req.SessionID, Status: "rejected", Message: "동시 세션 한도 초과, 잠시 후 재시도하세요",
		})
	}
}

// handleSessionStatus: GET /v1/sessions?id=xxx
// 클라이언트(에이전트 코어)가 폴링 방식으로 결과를 조회한다.
func (g *Gateway) handleSessionStatus(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("id")
	v, ok := g.sessions.Load(id)
	if !ok {
		http.Error(w, "session not found", http.StatusNotFound)
		return
	}
	writeJSON(w, http.StatusOK, v)
}

// handleSessionsAll: GET /v1/sessions/all
// 모니터링 대시보드 전용: 전체 세션 목록을 최근 것부터(StartedAt 내림차순) 반환한다.
func (g *Gateway) handleSessionsAll(w http.ResponseWriter, r *http.Request) {
	var all []*SessionState
	g.sessions.Range(func(_, v interface{}) bool {
		if s, ok := v.(*SessionState); ok {
			all = append(all, s)
		}
		return true
	})
	sort.Slice(all, func(i, j int) bool {
		return all[i].StartedAt.After(all[j].StartedAt)
	})
	writeJSON(w, http.StatusOK, all)
}

// handleHealth: GET /healthz — 무거운 로직 없는 단순 생존 확인용.
func (g *Gateway) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// withCORS: 브라우저(대시보드)가 다른 출처에서 이 API를 호출할 수 있도록 CORS 헤더를 붙인다.
// 지금은 로컬 개발 단계라 "*"로 열어뒀지만, 배포 시에는 정확한 출처로 좁히는 것이 안전하다.
func withCORS(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == http.MethodOptions {
			// 브라우저 preflight 요청 - 본 로직 없이 200만 반환
			w.WriteHeader(http.StatusOK)
			return
		}
		next(w, r)
	}
}

// ===== 실행 위임 =====

func (g *Gateway) runSession(req ExecuteRequest, state *SessionState) {
	defer func() { <-g.sem }() // 세마포어 반납 보장

	ctx, cancel := context.WithTimeout(context.Background(), SessionTimeout)
	defer cancel()

	resultCh := make(chan string, 1)
	errCh := make(chan error, 1)

	// onStats: DockerExecutor가 주기적으로 호출하는 콜백. 실행 도중 세션 상태에
	// CPU/메모리 실측치를 반영해서, 대시보드가 폴링할 때마다 최신 값을 볼 수 있게 한다.
	onStats := func(cpu, mem string) {
		g.updateSessionStats(req.SessionID, cpu, mem)
	}

	go func() {
		result, err := g.executor.Run(ctx, req.Tool, req.Args, onStats)
		if err != nil {
			errCh <- err
			return
		}
		resultCh <- result
	}()

	// 포인트: 최종 상태는 최신 스냅샷(모니터링 수치가 반영된 것)을 "복사"해서 만든다.
	// 기존 state 포인터를 직접 수정하면 (1) updateSessionStats가 저장한 CPU/MEM 값이
	// 덮어써져 사라지고 (2) 대시보드가 JSON 인코딩 중인 객체를 동시에 쓰는 경쟁 상태가 생긴다.
	var final SessionState
	select {
	case result := <-resultCh:
		final = g.snapshot(req.SessionID, state)
		final.Status = "completed"
		final.Result = result
		log.Printf("[완료] session=%s", req.SessionID)
	case err := <-errCh:
		final = g.snapshot(req.SessionID, state)
		final.Status = "failed"
		final.Error = err.Error()
		log.Printf("[실패] session=%s: %v", req.SessionID, err)
	case <-ctx.Done():
		final = g.snapshot(req.SessionID, state)
		final.Status = "timeout"
		final.Error = "실행 시간 초과 (하드 타임아웃)"
		log.Printf("[타임아웃] session=%s", req.SessionID)
	}
	g.sessions.Store(req.SessionID, &final)
}

// snapshot: sync.Map에 저장된 현재 세션 상태의 값 복사본을 반환한다.
func (g *Gateway) snapshot(sessionID string, fallback *SessionState) SessionState {
	if v, ok := g.sessions.Load(sessionID); ok {
		if s, ok := v.(*SessionState); ok {
			return *s
		}
	}
	return *fallback
}

// updateSessionStats: 실행 도중 CPU/메모리 실측치로 세션 상태를 갱신한다.
//
// 포인트: state 포인터의 필드를 직접 수정하지 않고, 기존 값을 복사해서 새 필드만
// 바꾼 뒤 새 포인터로 다시 Store한다. 두 goroutine이 같은 메모리를 동시에 쓰는
// 경쟁 상태를 피하기 위함이며, "재사용 대신 새로 만들어 교체" 원칙과도 일치한다.
func (g *Gateway) updateSessionStats(sessionID, cpu, mem string) {
	v, ok := g.sessions.Load(sessionID)
	if !ok {
		return
	}
	current, ok := v.(*SessionState)
	if !ok {
		return
	}
	updated := *current // 값 복사 (필드가 전부 string/time이라 얕은 복사로 충분)
	updated.CPUUsage = cpu
	updated.MemUsage = mem
	g.sessions.Store(sessionID, &updated)
}

// ===== 유틸 =====

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func main() {
	// ③ 인프라/보안 계층 연동: DockerExecutor를 우선 시도하고,
	// Docker 데몬 연결 자체가 안 되면 MockExecutor로 자동 폴백한다.
	// 포인트: 이 폴백은 "개발 편의"일 뿐이다. 실제 배포 시에는 폴백 없이
	// log.Fatal로 처리해, 격리 없이 서비스가 떠 있는 상황(=보안 사고)을 방지하는 것이 맞다.
	var executor SandboxExecutor
	dockerExec, err := NewDockerExecutor()
	if err != nil {
		log.Printf("[경고] Docker 연결 실패, MockExecutor로 폴백합니다: %v", err)
		executor = &MockExecutor{}
	} else {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
		if err := dockerExec.EnsureImages(ctx); err != nil {
			log.Printf("[경고] 이미지 준비 실패, MockExecutor로 폴백합니다: %v", err)
			executor = &MockExecutor{}
		} else {
			executor = dockerExec
		}
		cancel()
	}

	gw := NewGateway(executor)

	// withCFAccess로 한 번 더 감싸서, Cloudflare Access를 통과한 요청인지 게이트웨이
	// 스스로도 검증하게 한다 (엣지에서만 막고 서버는 무방비인 상태를 피하기 위함).
	// /healthz는 예외: 민감한 정보를 노출하지 않으므로 인증을 요구하지 않는다.
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/execute", withCORS(withCFAccess(gw.handleExecute)))
	mux.HandleFunc("/v1/sessions", withCORS(withCFAccess(gw.handleSessionStatus)))
	mux.HandleFunc("/v1/sessions/all", withCORS(withCFAccess(gw.handleSessionsAll)))
	mux.HandleFunc("/healthz", withCORS(gw.handleHealth))

	log.Printf("이벤트 중계 API 게이트웨이 시작: :8080 (executor=%T)", executor)
	log.Fatal(http.ListenAndServe(":8080", mux))
}
