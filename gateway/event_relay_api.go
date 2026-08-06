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
//   지금은 ③ 인프라/보안 계층(샌드박스 매니저)이 아직 없으므로,
//   MockExecutor라는 가짜 구현체로 전체 파이프라인(AI/LLM -> 게이트웨이)을
//   먼저 검증할 수 있다. 나중에 실제 Docker 실행 로직을 만들 때는
//   SandboxExecutor 인터페이스만 구현해서 NewGateway에 주입하면 되고,
//   Gateway 구조체나 HTTP 핸들러 코드는 단 한 줄도 고칠 필요가 없다.
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
	// 두 계층의 타임아웃 정책이 어긋나면, "왜 파서는 30초를 허용한다는데
	// 실제로는 다른 시간에 끊기지?" 같은 디버깅하기 어려운 불일치가 생긴다.
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
}

// -----------------------------------------------------------------------
// 실행 인터페이스 (백엔드/파이프라인 계층의 두 번째 컴포넌트)
// -----------------------------------------------------------------------

// SandboxExecutor: "샌드박스에 무엇을, 어떤 형식으로 넘길지"에 대한 계약(contract).
// ③ 인프라/보안 계층을 구현할 때, 이 인터페이스를 만족하는 구조체(예: DockerExecutor)를
// 하나 더 만들어서 NewGateway(dockerExecutor)로 교체하기만 하면 된다.
type SandboxExecutor interface {
	// Run: 도구 이름과 인자를 받아 실행하고, 결과 문자열 또는 에러를 반환한다.
	// ctx가 취소(타임아웃)되면 구현체는 반드시 실행 중인 프로세스/컨테이너를
	// 강제 종료해야 한다 (그렇지 않으면 타임아웃 났지만 컨테이너는 계속 도는
	// 자원 누수가 발생한다 - 실제 Docker 구현 시 반드시 지켜야 할 계약 조건).
	Run(ctx context.Context, tool string, args map[string]interface{}) (string, error)
}

// MockExecutor: 샌드박스 매니저가 아직 없을 때 파이프라인 전체(AI/LLM -> 게이트웨이)를
// 먼저 검증하기 위한 임시 구현체. 실제 코드를 실행하지 않고, 받은 요청을 그대로
// 텍스트로 되돌려주기만 한다.
type MockExecutor struct{}

func (m *MockExecutor) Run(ctx context.Context, tool string, args map[string]interface{}) (string, error) {
	// 실제 실행에 걸리는 지연을 흉내만 낸다.
	select {
	case <-time.After(200 * time.Millisecond):
	case <-ctx.Done():
		return "", ctx.Err()
	}
	return fmt.Sprintf("[MOCK 실행 결과] tool=%s args=%v (실제 샌드박스 미연결 상태)", tool, args), nil
}

// ===== 게이트웨이 상태 =====

type Gateway struct {
	sem      chan struct{}    // 동시 세션 수 제한용 세마포어 (버퍼드 채널)
	sessions sync.Map         // sessionID -> *SessionState
	executor SandboxExecutor  // 실제 실행을 위임할 대상 (지금은 MockExecutor)
}

// NewGateway: executor를 외부에서 주입받는다 (의존성 주입).
// 이렇게 생성자에서 주입받는 구조로 만들어두면, 나중에 실제 Docker 기반
// 샌드박스 매니저가 준비됐을 때 main()의 이 한 줄만 바꾸면 전체 시스템이 교체된다.
//   gw := NewGateway(&MockExecutor{})       // 지금
//   gw := NewGateway(&DockerExecutor{...})  // 나중에
func NewGateway(executor SandboxExecutor) *Gateway {
	return &Gateway{
		sem:      make(chan struct{}, MaxConcurrentSessions),
		executor: executor,
	}
}

// ===== 핸들러 =====

// handleExecute: POST /v1/execute
// 처리 흐름: 스키마 검증 -> 세마포어 논블로킹 획득 시도 -> 성공 시 비동기 실행 위임.
// 동시성 초과 시 큐잉하지 않고 즉시 429로 거부한다 (온디바이스 CPU 보호가 우선이라는
// 설계 판단 - 무한 대기열보다 클라이언트의 명시적 재시도가 더 안전하다고 봤다).
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
// 모니터링 대시보드 프론트엔드가 전체 세션 목록을 한 번에 가져오기 위한 엔드포인트.
// 포인트: gateway_client.py의 폴링(단일 세션 조회)과는 목적이 다르다 -
// 이 엔드포인트는 "지금까지 무슨 세션들이 있었는지"를 보여주는 대시보드 전용이다.
// 최근 것부터 보여주기 위해 StartedAt 기준 내림차순으로 정렬한다.
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

// withCORS: 프론트엔드(브라우저)가 다른 출처(예: file:// 또는 다른 포트로 열린 정적 HTML)에서
// 이 API를 호출할 수 있도록 CORS 헤더를 붙여주는 미들웨어.
//
// 포인트: 브라우저는 보안상 "다른 출처의 API를 기본적으로 차단"하는데(CORS 정책),
// 이 헤더가 없으면 프론트엔드의 fetch() 호출이 브라우저 단에서 막혀버린다.
// 지금은 로컬 개발 단계라 "*"(모든 출처 허용)로 단순하게 열어뒀지만,
// 실제 배포 시에는 프론트엔드가 서빙되는 정확한 출처로 좁히는 것이 안전하다.
func withCORS(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == http.MethodOptions {
			// 브라우저가 실제 요청 전에 미리 보내는 preflight 요청 - 본 로직 없이 200만 반환
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

	go func() {
		// 실제 실행은 executor(지금은 MockExecutor, 나중엔 DockerExecutor)에 완전히 위임한다.
		// 게이트웨이 코드는 "무엇을 실행하는지"를 전혀 알 필요가 없다 -
		// 이것이 인터페이스로 분리한 핵심 이점이다.
		result, err := g.executor.Run(ctx, req.Tool, req.Args)
		if err != nil {
			errCh <- err
			return
		}
		resultCh <- result
	}()

	select {
	case result := <-resultCh:
		state.Status = "completed"
		state.Result = result
		log.Printf("[완료] session=%s", req.SessionID)
	case err := <-errCh:
		state.Status = "failed"
		state.Error = err.Error()
		log.Printf("[실패] session=%s: %v", req.SessionID, err)
	case <-ctx.Done():
		state.Status = "timeout"
		state.Error = "실행 시간 초과 (하드 타임아웃)"
		log.Printf("[타임아웃] session=%s", req.SessionID)
	}
	g.sessions.Store(req.SessionID, state)
}

// ===== 유틸 =====

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func main() {
	// ③ 인프라/보안 계층 연동: DockerExecutor를 우선 시도하고,
	// Docker 데몬 연결 자체가 안 되면(개발 초기 단계 등) MockExecutor로 자동 폴백한다.
	// 포인트: 이 폴백은 "개발 편의"일 뿐이다. 실제 배포 시에는 폴백 없이
	// DockerExecutor 생성 실패를 즉시 log.Fatal로 처리해, 격리 없이 서비스가
	// 떠 있는 상황(=보안 사고)을 방지하는 것이 맞다.
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

	// 포인트: 프론트엔드(대시보드) 추가에 맞춰 모든 라우트를 withCORS로 감쌌다.
	// gateway_client.py 같은 서버 간 통신은 CORS의 영향을 받지 않으므로
	// (CORS는 브라우저에서만 강제되는 정책), 이 변경이 기존 Python 클라이언트
	// 동작에는 전혀 영향을 주지 않는다.
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/execute", withCORS(gw.handleExecute))
	mux.HandleFunc("/v1/sessions", withCORS(gw.handleSessionStatus))
	mux.HandleFunc("/v1/sessions/all", withCORS(gw.handleSessionsAll))
	mux.HandleFunc("/healthz", withCORS(gw.handleHealth))

	log.Printf("이벤트 중계 API 게이트웨이 시작: :8080 (executor=%T)", executor)
	log.Fatal(http.ListenAndServe(":8080", mux))
}
