"""
test_datasci_direct.py
=======================
LLM을 완전히 건너뛰고, 검증된 코드를 게이트웨이에 직접 제출해서
Docker 샌드박스 + matplotlib 파이프라인 자체가 정상인지 확인하는 스크립트.

목적: "LLM이 가끔 형식을 깬다"는 변수를 제거하고, 나머지 파이프라인
(게이트웨이 -> Docker exec -> matplotlib -> base64 -> 결과 반환)이
정말 문제없이 동작하는지 독립적으로 검증한다. 이게 통과하면 문제는
100% AI/LLM 계층(프롬프트/모델)에 있는 것이고, 이것도 실패하면
인프라 쪽(이미지, gVisor, 리소스 제한 등)을 봐야 한다는 뜻이다.

사용법:
    python3 test_datasci_direct.py
(gateway_client.py와 같은 폴더에 있어야 하고, 게이트웨이가 떠 있어야 함)
"""

import base64
import uuid

from gateway_client import GatewayError, submit_and_wait

# 이미 검증된 패턴(작은따옴표만 사용, 데이터 직접 정의, Agg 백엔드)의 코드.
# ai_llm_layer.py 시스템 프롬프트의 예시 3과 완전히 동일한 구조다.
CODE = """
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.rcParams['font.family'] = 'NanumGothic'
import io, base64

months = ['1월','2월','3월','4월','5월','6월','7월','8월','9월','10월','11월','12월']
sales = [320, 298, 350, 410, 380, 420, 390, 405, 460, 510, 550, 620]
df = pd.DataFrame({'month': months, 'sales': sales})
print(df.describe())

fig, ax = plt.subplots(figsize=(8, 4))
ax.bar(df['month'], df['sales'], color='#4ec9b0')
ax.set_title('월별 매출 (테스트 데이터)')
ax.set_ylabel('매출 (만원)')

buf = io.BytesIO()
fig.savefig(buf, format='png', bbox_inches='tight')
buf.seek(0)
print('PLOT_BASE64:' + base64.b64encode(buf.read()).decode())
""".strip()


def main() -> None:
    session_id = str(uuid.uuid4())
    payload = {
        "session_id": session_id,
        "tool": "run_code",
        "args": {"language": "python", "code": CODE},
    }

    print(f"제출 중... (session={session_id})")
    try:
        result = submit_and_wait(payload)
    except GatewayError as e:
        print(f"[게이트웨이 오류] {e}")
        print("-> 게이트웨이(cd gateway && go run .)가 떠 있는지 확인하세요.")
        return

    print(f"상태: {result.status}")

    if result.status != "completed":
        print(f"[실패] {result.error}")
        print("-> Docker 이미지(sandbox-python-datasci:latest)가 빌드됐는지,")
        print("   컨테이너 실행 로그(게이트웨이 터미널)에 에러가 없는지 확인하세요.")
        return

    plot_prefix = "PLOT_BASE64:"
    output = result.result or ""
    idx = output.find(plot_prefix)

    if idx != -1:
        # 포인트: PLOT_BASE64가 출력 맨 앞에 오라는 법은 없다 - 이번 코드처럼
        # df.describe() 텍스트가 먼저 출력되고, 그 뒤에 그래프가 이어질 수도 있다.
        # 그래서 startswith(맨 앞부터 일치)가 아니라 find(어디에 있든 찾기)로 판단해야 한다.
        text_part = output[:idx].strip()
        b64_data = output[idx + len(plot_prefix):].strip()

        if text_part:
            print("--- 텍스트 출력 ---")
            print(text_part)

        out_path = "test_plot_output.png"
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(b64_data))
        print(f"[성공] 그래프가 정상 생성됐습니다. 저장 위치: {out_path}")
        print("-> 이 파일을 열어서 실제로 막대그래프가 보이고 한글이 깨지지 않았다면,")
        print("   인프라/샌드박스 쪽은 완전히 정상입니다.")
    else:
        print("[의심] PLOT_BASE64가 출력에 포함되어 있지 않습니다:")
        print(output)


if __name__ == "__main__":
    main()
