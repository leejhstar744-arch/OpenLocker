# sandbox-python-datasci.Dockerfile
#
# 데이터셋 분석/시각화 시나리오 전용 이미지.
#
# 왜 별도 이미지가 필요한가:
#   run_code 컨테이너는 --network none으로 실행되므로, 실행 도중에는
#   pip install이 원천적으로 불가능하다. 그래서 pandas/matplotlib/numpy를
#   "이미지 빌드 시점"에 미리 넣어둔다 - 빌드는 인터넷이 연결된 상태로 한 번만 하고,
#   실제 코드 실행(컨테이너 런타임)은 여전히 완전히 네트워크 차단 상태로 유지된다.
#
# 빌드:
#   docker build -t sandbox-python-datasci:latest -f sandbox-python-datasci.Dockerfile .
FROM python:3.12-slim

# 한글 폰트 설치: matplotlib 기본 폰트(DejaVu Sans)는 한글 글리프가 없어서
# 그래프에 한글 라벨을 쓰면 빈 네모(tofu)로 깨진다.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-nanum \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -fv

RUN pip install --no-cache-dir \
    pandas==2.2.2 \
    matplotlib==3.9.0 \
    numpy==1.26.4

# 디스플레이가 없는 컨테이너이므로 Agg 백엔드(이미지/메모리 출력 전용)로 고정한다.
ENV MPLBACKEND=Agg

# --read-only 루트 파일시스템이라 $HOME/.config/matplotlib에 쓸 수 없다.
# 항상 --tmpfs로 마운트되는 /tmp를 캐시 위치로 명시해 불필요한 경고를 없앤다.
ENV MPLCONFIGDIR=/tmp/matplotlib-cache
