// docker_executor.go
//
// ③ 인프라/보안 계층: 실제 Docker 컨테이너로 코드를 실행하는 SandboxExecutor 구현체.
// event_relay_api.go의 SandboxExecutor 인터페이스만 만족시키면 되므로,
// 게이트웨이 쪽 코드는 이 파일의 존재 자체를 몰라도 된다 (의존성 주입으로 완전히 분리됨).
//
// 설계 변경 이력: 처음엔 github.com/docker/docker Go SDK를 사용했으나,
// 그 SDK가 내부적으로 의존하는 docker/distribution과 distribution/reference
// 모듈 버전이 서로 충돌하는(undefined: reference.SplitHostname) 고질적인 문제가 있었다.
// 그래서 SDK 대신 "docker" CLI 명령어를 os/exec로 직접 호출하는 방식으로 바꿨다.
// 이렇게 하면 Docker 관련 Go 의존성이 전혀 필요 없어지고(표준 라이브러리만 사용),
// 터미널에서 직접 docker run을 칠 때와 완전히 동일하게 동작해 디버깅도 쉬워진다.
//
// 이 파일이 담당하는 세 가지 역할 (아키텍처의 인프라/보안 계층 3개 컴포넌트와 대응):
//   1) 샌드박스 매니저 -> Run() 함수의 세션 생명주기 처리 (docker run 실행 -> 대기 -> --rm으로 자동 폐기)
//   2) 리소스 리미터   -> docker run에 넘기는 --cpuset-cpus/--memory/--pids-limit 플래그
//   3) 컨테이너 풀     -> EnsureImages()로 이미지를 미리 받아두는 "웜스탠바이"
//
// 사전 준비 (지후님 로컬 PC):
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
	"os/exec"
)

// ===== 리소스 리미터 설정값 =====
// 포인트: 아래 코어 배정은 cgroup_setup.sh가 만든 /sys/fs/cgroup/sandbox의 코어 범위와
// 반드시 일치해야 한다 (실제 실행 시 지후님 로컬 환경이 systemd cgroup 드라이버를 사용해
// --cgroup-parent 플래그와 충돌이 확인되어, 호스트 cgroup과의 이중 편입은 제거했다.
// SandboxCgroupParent 상수는 cgroup_setup.sh와의 코어 배정 값이 같다는 것을 문서화하는
// 용도로만 남겨두고, 실제 docker run 커맨드에는 더 이상 전달하지 않는다).
const (
	SandboxCgroupParent = "sandbox" // 참고용: cgroup_setup.sh가 만든 /sys/fs/cgroup/sandbox (더 이상 dockerArgs에 전달하지 않음)
	SandboxCpuset       = "2,3"     // 샌드박스 전용 코어 (event_relay_api.go 설계와 동일)
	SandboxMemory       = "512m"    // 컨테이너 1개당 메모리 상한 (그룹 상한 2G를 동시세션 2개+여유로 분배)
	SandboxPidsLimit    = "64"      // 포크폭탄 방지: 컨테이너 내 프로세스 개수 상한
)

// 도구별 실행 이미지/커맨드 매핑.
// 포인트: 화이트리스트 원칙을 여기서도 반복한다 - languageRunners에 없는 언어는 즉시 거부.
// tool_call_parser.py의 ALLOWED_TOOLS와 마찬가지로, "명시적으로 검토한 것만 허용"하는 방식이다.
var languageRunners = map[string]struct {
	Image string
	Build func(code string) []string // 코드를 컨테이너 안에서 실행할 커맨드(argv)로 변환
}{
	"python": {
		Image: "python:3.12-slim",
		// 포인트: 이 배열은 "docker run ... python:3.12-slim python3 -c <code>"에서
		// 이미지 뒤에 붙는 부분이다. code 전체가 python3의 -c 인자 '하나'로만 전달되므로,
		// 셸을 거치지 않아 code 안의 특수문자(;, |, $() 등)로 인한 명령 주입 자체가 불가능하다.
		Build: func(code string) []string { return []string{"python3", "-c", code} },
	},
}

// DockerExecutor: SandboxExecutor 인터페이스의 실제 구현체 (docker CLI 호출 기반).
type DockerExecutor struct{}

func NewDockerExecutor() (*DockerExecutor, error) {
	if _, err := exec.LookPath("docker"); err != nil {
		return nil, fmt.Errorf("docker CLI를 찾을 수 없습니다 (PATH 확인 필요): %w", err)
	}
	return &DockerExecutor{}, nil
}

// EnsureImages: 서버 시작 시 필요한 이미지를 미리 받아둔다 ("docker pull"을 미리 실행).
//
// 포인트: 이것이 이 구현에서 '웜스탠바이'가 실질적으로 의미하는 바다.
// 컨테이너 자체를 미리 띄워 재사용하는 방식(진짜 웜 컨테이너)은 상태 오염 위험이 있어
// 애초에 지양하기로 설계했었다 (아키텍처 문서 4️⃣ 참고). 대신 "이미지가 이미 로컬에 있어
// 매 요청마다 수십 초~수 분씩 걸리는 최초 pull 지연을 없애는 것"을 웜스탠바이의 실질적
// 목표로 재정의했다 - 첫 요청부터 빠르게 응답하면서도, 컨테이너는 여전히 매번 새로 만든다.
func (d *DockerExecutor) EnsureImages(ctx context.Context) error {
	for lang, runner := range languageRunners {
		log.Printf("[샌드박스 매니저] 이미지 준비 확인: %s (%s)", lang, runner.Image)
		cmd := exec.CommandContext(ctx, "docker", "pull", runner.Image)
		out, err := cmd.CombinedOutput()
		if err != nil {
			return fmt.Errorf("이미지 %s(%s) 준비 실패: %w\n%s", lang, runner.Image, err, string(out))
		}
	}
	return nil
}

// Run: SandboxExecutor 인터페이스 구현. event_relay_api.go의 runSession()이 이 함수를 호출한다.
//
// 세션 생명주기: [명령 조립] -> [docker run 실행] -> [대기] -> [자동 폐기(--rm)]
// 포인트: --rm 플래그가 있으면 컨테이너 프로세스가 끝나는 즉시 Docker가 알아서 컨테이너를
// 제거해준다. "하나의 세션 = 하나의 컨테이너, 재사용 금지" 원칙을 별도 정리 코드 없이
// Docker 자체 기능으로 강제하는 셈이다.
func (d *DockerExecutor) Run(ctx context.Context, tool string, args map[string]interface{}) (string, error) {
	if tool != "run_code" {
		// 화이트리스트에 없는 도구 요청 - 파서 단계에서 이미 걸러졌어야 하지만,
		// 계층 간 신뢰를 100%로 가정하지 않고 여기서도 한 번 더 확인한다 (심층 방어).
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

	dockerArgs := []string{
		"run", "--rm", // 종료 즉시 자동 삭제 (재사용 금지 원칙을 Docker 기능으로 강제)

		// --- 네트워크 정책: 기본 차단 ---
		// 포인트: "none"은 화이트리스트 이전에 네트워크 인터페이스 자체가 없는 상태다.
		// run_code는 애초에 네트워크가 필요 없는 순수 계산 작업을 전제로 하므로 안전하다.
		"--network", "none",

		// --- 리소스 리미터: cgroup_setup.sh의 host 레벨 설정과 짝을 이룬다 ---
		// 포인트: 원래는 --cgroup-parent로 호스트의 /sys/fs/cgroup/sandbox 산하에
		// 컨테이너를 편입시켜 이중 방어를 하려 했으나, systemd cgroup 드라이버를 쓰는
		// 환경(Ubuntu 24.04 기본값)에서는 --cgroup-parent에 "sandbox.slice"처럼
		// systemd 슬라이스 이름 규칙을 따라야만 동작한다. 이 프로젝트 규모에서는
		// 별도의 systemd 슬라이스를 만드는 복잡도를 감수할 실익이 적어서, 이 플래그는
		// 제거하고 컨테이너 자체의 cpuset/memory/pids 제한만으로 리소스를 강제하기로 했다.
		// (cgroup_setup.sh가 만든 llm/sandbox 그룹은 여전히 유효하며, Ollama 프로세스를
		//  llm 그룹에 배정하는 용도로는 그대로 사용한다.)
		"--cpuset-cpus", SandboxCpuset,
		"--memory", SandboxMemory,
		"--pids-limit", SandboxPidsLimit,

		// --- 탈옥 피해 최소화 (심층 방어) ---
		"--read-only",                       // 루트 파일시스템 쓰기 금지
		"--tmpfs", "/tmp:rw,size=64m",       // 코드 실행에 필요한 최소 쓰기 공간만 예외 허용
		"--cap-drop", "ALL",                  // 모든 리눅스 capability 제거
		"--security-opt", "no-new-privileges", // setuid 등을 통한 권한 상승 금지

		runner.Image,
	}
	dockerArgs = append(dockerArgs, runner.Build(code)...)

	// context에 데드라인이 걸려 있으면(게이트웨이의 SessionTimeout), CommandContext가
	// 그 시점에 프로세스를 자동으로 Kill한다 - 별도 타이머 코드를 직접 짤 필요가 없다.
	cmd := exec.CommandContext(ctx, "docker", dockerArgs...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	runErr := cmd.Run()

	if ctx.Err() == context.DeadlineExceeded {
		return "", fmt.Errorf("실행 시간 초과로 컨테이너를 강제 종료했습니다")
	}

	if stderr.Len() > 0 {
		// 포인트: stderr에 내용이 있으면 실패로 간주한다. LLM이 생성한 코드에 문법 오류가
		// 있었다면 바로 여기서 걸러진다 - 파서는 형식만 검증하고, 코드 내용의 실제
		// 정확성은 이 실행 단계에서 자연스럽게 드러나도록 설계했다 (설계문서 9️⃣ 참고).
		return "", fmt.Errorf("실행 중 오류 출력: %s", stderr.String())
	}
	if runErr != nil {
		return "", fmt.Errorf("컨테이너 실행 실패: %w", runErr)
	}

	return stdout.String(), nil
}
