// docker_executor.go
//
// ③ 인프라/보안 계층: 실제 Docker 컨테이너로 코드를 실행하는 SandboxExecutor 구현체.
// event_relay_api.go의 SandboxExecutor 인터페이스만 만족시키면 되므로,
// 게이트웨이 쪽 코드는 이 파일의 존재 자체를 몰라도 된다 (의존성 주입으로 완전히 분리됨).
//
// 설계 변경 이력 1: github.com/docker/docker Go SDK -> docker CLI를 os/exec로 직접 호출.
//   SDK가 의존하는 docker/distribution과 distribution/reference 모듈 버전 충돌
//   (undefined: reference.SplitHostname) 문제를 근본적으로 피하기 위함.
//
// 설계 변경 이력 2: "이미지 프리풀"만 하던 웜스탠바이를,
//   실제로 컨테이너를 미리 띄워두고 재사용하는 "진짜 웜스탠바이"로 승격.
//   미리 떠 있는 idle 컨테이너에 docker exec로 코드만 주입하는 방식으로 바꿔서
//   "컨테이너 생성" 비용을 요청 경로에서 제거했다. 대신 실행이 끝난 컨테이너는
//   여전히 재사용하지 않고 즉시 폐기 + 백그라운드로 새 idle 컨테이너를 채워 넣는다
//   ("재사용은 대기 상태까지만, 실행된 컨테이너는 반드시 버린다").
//
// 설계 변경 이력 3: gVisor(runsc) 런타임 자동 감지 + SANDBOX_RUNTIME 환경변수로 강제 전환.
//
// 이 파일이 담당하는 세 가지 역할 (아키텍처의 인프라/보안 계층 3개 컴포넌트와 대응):
//   1) 샌드박스 매니저 -> Run() 함수의 세션 생명주기 처리 (풀에서 대여 -> exec -> 폐기+보충)
//   2) 리소스 리미터   -> idle 컨테이너 생성 시 넘기는 --cpuset-cpus/--memory/--pids-limit
//   3) 컨테이너 풀     -> idlePool 채널 (진짜 웜스탠바이) + 실시간 자원 모니터링(pollStats)
//
// 사전 준비:
//   docker 데몬 실행 중이어야 함 (systemctl status docker)
//   docker CLI가 PATH에 있어야 함 (which docker)
//   sudo usermod -aG docker $USER (sudo 없이 docker 명령을 쓰려면, 재로그인 필요)
//   *** go get이 전혀 필요 없다. 표준 라이브러리만 사용한다. ***
package main

import (
	"bytes"
	"context"
	"fmt"
	"log"
	"os"
	"os/exec"
	"strings"
	"time"
)

// ===== 리소스 리미터 설정값 =====
// 포인트: 아래 코어 배정은 cgroup_setup.sh가 만든 /sys/fs/cgroup/sandbox의 코어 범위와
// 반드시 일치해야 한다. (로컬 환경이 systemd cgroup 드라이버를 사용해 --cgroup-parent
// 플래그와 충돌이 확인되어, 호스트 cgroup과의 이중 편입은 제거했다. SandboxCgroupParent
// 상수는 문서화 용도로만 남겨두고, 실제 docker 커맨드에는 전달하지 않는다.)
const (
	SandboxCgroupParent = "sandbox" // 참고용: cgroup_setup.sh가 만든 /sys/fs/cgroup/sandbox
	SandboxCpuset       = "2,3"     // 샌드박스 전용 코어
	SandboxMemory       = "512m"    // 컨테이너 1개당 메모리 상한 (그룹 상한 2G를 동시세션 2개+여유로 분배)
	SandboxPidsLimit    = "64"      // 포크폭탄 방지: 컨테이너 내 프로세스 개수 상한

	// WarmPoolSize: 미리 띄워둘 idle 컨테이너 개수.
	// CPU 코어가 2-3 두 개뿐인 온디바이스 환경에서 idle 컨테이너를 여러 개 띄워두면
	// 그 자체로 메모리/CPU를 잠식하므로, 딱 1개만 상시 대기시킨다.
	WarmPoolSize = 1

	// StatsPollInterval: 실행 중인 컨테이너의 CPU/메모리를 얼마나 자주 샘플링할지.
	StatsPollInterval = 500 * time.Millisecond
)

// 도구별 실행 이미지/커맨드 매핑.
// 포인트: 화이트리스트 원칙을 여기서도 반복한다 - languageRunners에 없는 언어는 즉시 거부.
var languageRunners = map[string]struct {
	Image string
	Build func(code string) []string // 코드를 컨테이너 안에서 실행할 커맨드(argv)로 변환
}{
	"python": {
		// pandas/matplotlib/numpy가 미리 설치된 커스텀 이미지(sandbox-python-datasci.Dockerfile).
		// 실행 컨테이너는 --network none이라 런타임에 pip install을 할 수 없으므로 빌드 시점에 넣어둔다.
		// 사전 준비: docker build -t sandbox-python-datasci:latest -f sandbox-python-datasci.Dockerfile .
		Image: "sandbox-python-datasci:latest",
		// code 전체가 python3의 -c 인자 '하나'로만 전달되므로, 셸을 거치지 않아
		// code 안의 특수문자로 인한 명령 주입이 불가능하다.
		Build: func(code string) []string { return []string{"python3", "-c", code} },
	},
}

// DockerExecutor: SandboxExecutor 인터페이스의 실제 구현체 (docker CLI 호출 기반).
type DockerExecutor struct {
	idlePool chan string // 실행 준비가 된 idle 컨테이너 ID들 (진짜 웜스탠바이 풀)
	runtime  string      // "runsc"(gVisor 사용 가능) 또는 ""(기본 runc로 폴백)
}

func NewDockerExecutor() (*DockerExecutor, error) {
	if _, err := exec.LookPath("docker"); err != nil {
		return nil, fmt.Errorf("docker CLI를 찾을 수 없습니다 (PATH 확인 필요): %w", err)
	}

	d := &DockerExecutor{idlePool: make(chan string, WarmPoolSize)}

	// 기본은 자동 감지지만, SANDBOX_RUNTIME 환경변수로 강제 지정할 수 있다.
	// 같은 바이너리·같은 설치 상태에서 런타임만 바꿔가며 A/B 벤치마크를 돌리기 위함이다.
	switch strings.ToLower(strings.TrimSpace(os.Getenv("SANDBOX_RUNTIME"))) {
	case "runc":
		d.runtime = ""
		log.Println("[샌드박스 매니저] SANDBOX_RUNTIME=runc 지정됨 - gVisor 비활성화(runc 강제, 베이스라인 측정용)")
	case "runsc":
		d.runtime = "runsc"
		log.Println("[샌드박스 매니저] SANDBOX_RUNTIME=runsc 지정됨 - gVisor 강제 사용")
	default:
		d.runtime = detectGvisorRuntime()
		// gVisor가 없다고 서버 시작 자체를 막지는 않는다 ("우아한 성능 저하").
		// 다만 연구 결과를 발표할 때는 gVisor가 실제로 적용된 상태인지 로그로 반드시 확인해야 한다.
		if d.runtime == "runsc" {
			log.Println("[샌드박스 매니저] gVisor(runsc) 런타임 감지됨 - 강화된 커널 격리 적용")
		} else {
			log.Println("[경고] gVisor(runsc) 런타임을 찾을 수 없어 기본 runc로 동작합니다 " +
				"(설치: https://gvisor.dev/docs/user_guide/install/)")
		}
	}

	return d, nil
}

// detectGvisorRuntime: "docker info"로 이 Docker 데몬에 runsc 런타임이 등록되어 있는지 확인한다.
// 런타임 목록은 데몬 재시작 전까지 바뀌지 않으므로 시작 시 한 번만 확인해 캐싱한다.
func detectGvisorRuntime() string {
	out, err := exec.Command("docker", "info", "--format", "{{json .Runtimes}}").Output()
	if err != nil {
		return ""
	}
	if strings.Contains(string(out), "runsc") {
		return "runsc"
	}
	return ""
}

// EnsureImages: 이미지를 준비하고, 웜스탠바이 풀을 초기 채운다.
func (d *DockerExecutor) EnsureImages(ctx context.Context) error {
	for lang, runner := range languageRunners {
		// 로컬에서 직접 빌드한 이미지는 레지스트리에 없으므로, 먼저 "docker image inspect"로
		// 로컬 존재 여부를 확인하고 있으면 pull을 건너뛴다.
		if err := exec.CommandContext(ctx, "docker", "image", "inspect", runner.Image).Run(); err == nil {
			log.Printf("[샌드박스 매니저] 이미지 로컬에 이미 존재: %s (%s)", lang, runner.Image)
			continue
		}

		log.Printf("[샌드박스 매니저] 이미지 준비 확인: %s (%s)", lang, runner.Image)
		cmd := exec.CommandContext(ctx, "docker", "pull", runner.Image)
		out, err := cmd.CombinedOutput()
		if err != nil {
			return fmt.Errorf(
				"이미지 %s(%s) 준비 실패: %w\n%s\n"+
					"커스텀 이미지라면 먼저 로컬에서 빌드해야 합니다: "+
					"docker build -t %s -f sandbox-python-datasci.Dockerfile .",
				lang, runner.Image, err, string(out), runner.Image,
			)
		}
	}

	// 웜스탠바이 풀 초기화 - python 이미지로 WarmPoolSize개의 idle 컨테이너를 미리 띄운다.
	pythonImage := languageRunners["python"].Image
	for i := 0; i < WarmPoolSize; i++ {
		id, err := d.startIdleContainer(ctx, pythonImage)
		if err != nil {
			return fmt.Errorf("웜 컨테이너 준비 실패: %w", err)
		}
		d.idlePool <- id
		log.Printf("[샌드박스 매니저] 웜 컨테이너 준비 완료: %s", shortID(id))
	}
	return nil
}

// startIdleContainer: "sleep infinity"로 대기만 하는 컨테이너를 하나 띄운다.
// 이 컨테이너 자체가 리소스 리미터/네트워크 차단/탈옥 방지 옵션을 전부 갖고 태어나며,
// 나중에 docker exec로 주입한 프로세스도 이 제약을 그대로 상속받는다.
//
// gVisor(runsc) 적용 지점: --runtime 플래그는 컨테이너 "생성" 시점에만 필요하다.
// docker exec는 이미 만들어진 컨테이너의 런타임을 그대로 물려받는다.
func (d *DockerExecutor) startIdleContainer(ctx context.Context, image string) (string, error) {
	args := []string{
		"run", "-d", // 백그라운드 대기 (--rm 없음: 실행 후 destroyAndReplenish에서 직접 폐기)
	}

	if d.runtime != "" {
		args = append(args, "--runtime", d.runtime)
	}

	args = append(args,
		// --- 네트워크 정책: 완전 차단 ---
		"--network", "none",

		// --- 리소스 리미터 ---
		"--cpuset-cpus", SandboxCpuset,
		"--memory", SandboxMemory,
		"--pids-limit", SandboxPidsLimit,

		// --- 탈옥 피해 최소화 (심층 방어) ---
		// gVisor와 일부 중복되지만, "여러 겹 방어" 원칙을 유지한다.
		"--read-only",
		"--tmpfs", "/tmp:rw,size=64m",
		"--cap-drop", "ALL",
		"--security-opt", "no-new-privileges",

		image, "sleep", "infinity",
	)
	out, err := exec.CommandContext(ctx, "docker", args...).Output()
	if err != nil {
		return "", fmt.Errorf("idle 컨테이너 시작 실패: %w", err)
	}
	return strings.TrimSpace(string(out)), nil
}

// destroyAndReplenish: 사용이 끝난(오염 가능성이 있는) 컨테이너를 강제 삭제하고,
// 백그라운드로 새 idle 컨테이너를 하나 채워 넣어 풀을 유지한다.
// "재사용 금지" 원칙과 "웜스탠바이" 최적화를 동시에 만족시키는 핵심 함수다.
func (d *DockerExecutor) destroyAndReplenish(containerID, image string) {
	cleanupCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := exec.CommandContext(cleanupCtx, "docker", "rm", "-f", containerID).Run(); err != nil {
		log.Printf("[경고] 컨테이너 제거 실패 (수동 정리 필요할 수 있음): %s: %v", shortID(containerID), err)
	}

	go func() {
		replenishCtx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()
		id, err := d.startIdleContainer(replenishCtx, image)
		if err != nil {
			// 여기서 실패해도 다음 요청이 막히진 않는다 - Run()에 즉석 생성 폴백 경로가 있다.
			log.Printf("[경고] 웜 컨테이너 보충 실패: %v", err)
			return
		}
		select {
		case d.idlePool <- id:
			log.Printf("[샌드박스 매니저] 웜 컨테이너 보충 완료: %s", shortID(id))
		default:
			// 풀이 이미 가득 찼다면(즉석 생성 경로와 겹친 경우) 여분 컨테이너는 바로 정리한다.
			_ = exec.Command("docker", "rm", "-f", id).Run()
		}
	}()
}

// pollStats: 실행 중인 컨테이너의 CPU/메모리 사용량을 주기적으로 샘플링해 onStats로 보고한다.
func (d *DockerExecutor) pollStats(containerID string, onStats StatsCallback, done <-chan struct{}) {
	if onStats == nil {
		return
	}
	ticker := time.NewTicker(StatsPollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-done:
			return
		case <-ticker.C:
			cpu, mem, err := d.readStats(containerID)
			if err != nil {
				continue // 컨테이너 시작/종료 타이밍이면 한 번쯤 실패할 수 있다 - 다음 틱에 재시도
			}
			onStats(cpu, mem)
		}
	}
}

// readStats: "docker stats --no-stream"으로 순간 CPU%/메모리 사용량을 한 번 읽어온다.
func (d *DockerExecutor) readStats(containerID string) (cpuPct string, memUsage string, err error) {
	out, err := exec.Command("docker", "stats", containerID, "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}").Output()
	if err != nil {
		return "", "", err
	}
	parts := strings.SplitN(strings.TrimSpace(string(out)), "|", 2)
	if len(parts) != 2 {
		return "", "", fmt.Errorf("docker stats 출력 형식이 예상과 다릅니다: %q", out)
	}
	return parts[0], parts[1], nil
}

// Run: SandboxExecutor 인터페이스 구현. event_relay_api.go의 runSession()이 이 함수를 호출한다.
//
// 세션 생명주기: [풀에서 대여] -> [docker exec로 코드 주입] -> [대기] -> [강제 폐기+풀 보충]
func (d *DockerExecutor) Run(ctx context.Context, tool string, args map[string]interface{}, onStats StatsCallback) (string, error) {
	if tool != "run_code" {
		// 파서 단계에서 이미 걸러졌어야 하지만, 계층 간 신뢰를 100%로 가정하지 않는다 (심층 방어).
		return "", fmt.Errorf("샌드박스 매니저는 run_code만 지원합니다 (요청: %s)", tool)
	}

	lang, _ := args["language"].(string)
	code, _ := args["code"].(string)
	runner, ok := languageRunners[lang]
	if !ok {
		return "", fmt.Errorf("지원하지 않는 언어입니다: %s", lang)
	}
	if code == "" {
		return "", fmt.Errorf("code 인자가 비어 있습니다")
	}

	// ----- [풀에서 대여] -----
	var containerID string
	select {
	case containerID = <-d.idlePool:
		log.Printf("[샌드박스 매니저] 웜 컨테이너 재사용: %s", shortID(containerID))
	case <-time.After(2 * time.Second):
		// 풀이 비어있다면 그 자리에서 즉시 새 컨테이너를 만든다 - 지연은 늘지만 요청을 거부하진 않는다.
		log.Printf("[샌드박스 매니저] 웜 컨테이너 풀이 비어있어 즉시 생성합니다")
		id, err := d.startIdleContainer(ctx, runner.Image)
		if err != nil {
			return "", fmt.Errorf("컨테이너 준비 실패: %w", err)
		}
		containerID = id
	}

	// ----- [폐기 보장 + 풀 보충] -----
	defer d.destroyAndReplenish(containerID, runner.Image)

	// ----- [자원 모니터링 시작] -----
	statsDone := make(chan struct{})
	go d.pollStats(containerID, onStats, statsDone)
	defer close(statsDone)

	// ----- [docker exec로 코드 주입] -----
	// 컨테이너는 이미 떠 있으므로 "이미 격리된 환경 안에 새 프로세스 하나를 추가로 실행"할 뿐이다.
	execArgs := append([]string{"exec", containerID}, runner.Build(code)...)
	cmd := exec.CommandContext(ctx, "docker", execArgs...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	runErr := cmd.Run()

	if ctx.Err() == context.DeadlineExceeded {
		// exec 클라이언트는 CommandContext로 죽지만, 컨테이너 내부 프로세스까지 확실히 정리되는 건
		// defer destroyAndReplenish()가 컨테이너를 강제 삭제(docker rm -f)하기 때문이다.
		return "", fmt.Errorf("실행 시간 초과로 컨테이너를 강제 종료했습니다")
	}

	// 포인트: 성공/실패 판정은 stderr 유무가 아니라 프로세스 종료 코드(exit code)로 한다.
	// 파이썬은 정상 실행 중에도 경고(matplotlib 캐시 알림 등)를 stderr로 출력하기 때문이다.
	if runErr != nil {
		if stderr.Len() > 0 {
			return "", fmt.Errorf("실행 중 오류 출력: %s", stderr.String())
		}
		return "", fmt.Errorf("컨테이너 실행 실패: %w", runErr)
	}

	// 종료 코드 0인데 stderr에 내용이 있다면 경고일 뿐이다 - 서버 로그에만 남긴다.
	if stderr.Len() > 0 {
		log.Printf("[참고] 실행은 성공했지만 경고가 출력됨: %s", stderr.String())
	}

	return stdout.String(), nil
}

// shortID: 로그 출력용으로 컨테이너 ID 앞 12자리만 잘라낸다 (짧은 ID에서도 패닉하지 않도록).
func shortID(id string) string {
	if len(id) > 12 {
		return id[:12]
	}
	return id
}
