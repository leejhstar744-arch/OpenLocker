#!/usr/bin/env bash
#
# 리소스 리미터: cgroup v2 기반 CPU 코어 분리 스크립트
# =====================================================
# 목적: LLM 추론 프로세스(Ollama)와 Docker 샌드박스 컨테이너가
#       같은 CPU를 두고 서로 자원을 뺏는 상황(경합)을 물리적으로 막는다.
#
# 왜 이 스크립트가 필요한가:
#   GPU가 있으면 "LLM=GPU, 샌드박스=CPU"로 자연히 자원이 나뉘지만,
#   이 노트북은 CPU 전용이라 두 워크로드가 같은 자원을 공유한다.
#   cgroup의 cpuset 기능으로 "이 프로세스 그룹은 반드시 이 코어에서만 돈다"를
#   커널 레벨에서 강제하면, 소프트웨어 스케줄링에 맡기는 것보다 훨씬 예측 가능해진다.
#
# 참고: systemd cgroup 드라이버 환경에서는 docker --cgroup-parent와 충돌하므로,
#   실제 샌드박스 컨테이너의 자원 제한은 docker_executor.go의 컨테이너 레벨 옵션
#   (--cpuset-cpus/--memory/--pids-limit)이 담당한다. 이 스크립트는 호스트 레벨 참고용이다.
#
# 전제 조건:
#   - 물리 코어 4개 이상 (0-1: LLM용, 2-3: 샌드박스용으로 나누기 때문)
#   - cgroup v2가 마운트되어 있어야 함 (Ubuntu 22.04+는 기본적으로 v2 사용)
#
# 사용법: sudo ./cgroup_setup.sh

set -euo pipefail
# set -e: 명령어 하나라도 실패하면 즉시 중단 ("일부만 적용된 상태" 방지)
# set -u: 정의되지 않은 변수 참조를 에러로 처리 (오타로 인한 사고 방지)
# set -o pipefail: 파이프라인 중간 명령 실패도 감지

CGROOT="/sys/fs/cgroup"
LLM_GROUP="${CGROOT}/llm"
SANDBOX_GROUP="${CGROOT}/sandbox"

echo "[1/5] cgroup v2 마운트 확인"
if ! mount | grep -q "cgroup2 on ${CGROOT}"; then
  echo "오류: cgroup v2가 ${CGROOT}에 마운트되어 있지 않습니다." >&2
  exit 1
fi

echo "[2/5] llm / sandbox 서브그룹 생성"
# cgroup v2에서는 디렉토리를 만드는 것 자체가 곧 새로운 cgroup을 만드는 것이다.
mkdir -p "${LLM_GROUP}"
mkdir -p "${SANDBOX_GROUP}"

echo "[3/5] CPU 코어 파티셔닝 (cpuset)"
# 겹치지 않게 나누는 것이 핵심이다. 겹치면 결국 같은 코어를 두고 다시 경합하게 된다.
echo "0-1" > "${LLM_GROUP}/cpuset.cpus"
echo "2-3" > "${SANDBOX_GROUP}/cpuset.cpus"

# cpuset.mems: 일반 노트북은 NUMA 노드가 1개(0번)뿐이라 형식적인 설정이지만,
# 비어있으면 cpuset 컨트롤러가 동작을 거부하는 경우가 있어 명시해 둔다.
echo "0" > "${LLM_GROUP}/cpuset.mems" 2>/dev/null || true
echo "0" > "${SANDBOX_GROUP}/cpuset.mems" 2>/dev/null || true

echo "[4/5] 메모리 상한 설정"
# 이 상한이 없으면, 샌드박스 안의 메모리 폭주 코드가 호스트 전체 메모리를 고갈시켜
# LLM 프로세스까지 함께 죽일 수 있다. 아래 값은 8GB RAM 기준 예시이므로 실제 RAM에 맞춰 조정할 것.
echo "4G" > "${LLM_GROUP}/memory.max"
echo "2G" > "${SANDBOX_GROUP}/memory.max"

echo "[5/5] 완료. 프로세스/컨테이너 배정 방법 안내:"
cat <<'EOF'

# 1) Ollama 프로세스를 llm cgroup에 배정하는 방법
#    ollama_pid=$(pgrep -f "ollama serve" | head -n1)
#    echo "$ollama_pid" > /sys/fs/cgroup/llm/cgroup.procs

# 2) Docker 컨테이너를 sandbox 전용 코어에 고정해서 실행하는 방법
#    docker run --cpuset-cpus="2,3" --memory="2g" <image>

# --- 참고: 향후 GPU 확보 시 VRAM 제한 ---
#   nvidia-container-toolkit 설치 후
#   docker run --gpus '"device=0"' <image>
#   다만 이것만으로는 VRAM 사용량 상한까지 강제되진 않으며,
#   NVIDIA MPS(시간분할) 같은 별도 설정이 추가로 필요하다.
EOF

echo "cgroup 설정 완료: llm(코어0-1, 4G) / sandbox(코어2-3, 2G)"
