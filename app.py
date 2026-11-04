
import os, json, smtplib
from email.mime.text import MIMEText
import streamlit as st
from openai import OpenAI

# OpenAI 클라이언트 생성
client = OpenAI()

# 페이지 설정
st.set_page_config(page_title="음성 AI 서비스", page_icon="🤖", layout="wide")
st.title("음성 인식 AI 서비스")
st.write("음성으로 질문하시면 답변해 드립니다.")

# 역할 지정
sys_role = """
당신은 '김 대리'의 개인 비서 '에이블'입니다.
김 대리가 음성으로 요청한 업무를 최선을 다해 처리합니다.
미팅 내용이 주어지면 핵심 내용을 요약하고, 김 대리에게 배정된 업무를 자세히 정리합니다.
이메일로 보내 달라는 요청을 받으면 send_email 도구를 사용합니다.
김 대리의 이메일 주소는 rumeri46@gmail.com 입니다.
답변은 한국어로 작성하고, 없는 내용이나 모르는 내용은 지어내지 마십시오.
"""

# 도구 정의
tools = [{
    "type": "function",
    "name": "send_email",
    "description": "이메일을 발송합니다.",
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "이메일 수신자. 예: 'test@gmail.com'"
            },
            "subject": {
                "type": "string",
                "description": "이메일 제목. 예: '마케팅 전략 초안'"
            },
            "body": {
                "type": "string",
                "description": "이메일 본문. 예: '안녕하세요? 김대리입니다.'"
            }
        },
        "required": ["to", "subject", "body"],
        "additionalProperties": False
    },
    "strict": True
}]

# 함수 만들기
def send_email(to, subject, body):
    sender = os.getenv("GMAIL_ADDRESS")
    password = os.getenv("GMAIL_APP_PASSWORD")

    # 메일 구성 (본문, 제목, 발신자, 수신자)
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to

    # 메일 보내기 (with 구문: 오류가 나도 연결이 닫힘)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(sender, password)
            s.sendmail(sender, to, msg.as_string())
        return {"status": "success"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}

# 상태 초기화
if "messages" not in st.session_state:
    st.session_state.messages = []

# 지시 영역
with st.form("task_form"):
    uploaded_file = st.file_uploader("텍스트 파일 업로드", type=["txt"])
    audio_value = st.audio_input("무엇을 도와드릴까요?")
    submit = st.form_submit_button("실행")

# 버튼을 눌렀을 때만 실행
if submit:
    # 업로드 된 파일이 있으면 디코딩
    text = ""
    if uploaded_file is not None:
        text = uploaded_file.read().decode("utf-8")

    # 녹음이 없으면 녹음 요청
    if audio_value is None:
        st.warning("음성을 녹음해주세요.")
        st.stop()

    with st.spinner("답변을 준비하고 있습니다..."):
        # STT: 음성 → 텍스트
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=("audio.wav", audio_value, "audio/wav")
        )

        # 프롬프트 구성
        prompt = transcript.text
        if text:
            prompt += f"\n\n[미팅 내용]\n{text}"

        # 1단계: 모델에게 질문 + 도구 정의 전달
        response = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {"role": "system", "content": sys_role},
                {"role": "user", "content": prompt}
            ],
            tools=tools,
            tool_choice="auto"
        )

        # 도구 필요 여부 확인
        tool_calls = [
            item for item in response.output
            if item.type == "function_call"
        ]

        if not tool_calls:
            # 도구가 필요 없으면 답변 사용
            answer = response.output_text
        else:
            # 2단계: 모델이 요청한 tool 실행
            tool_outputs = []
            for item in tool_calls:
                args = json.loads(item.arguments)

                if item.name == "send_email":
                    result = send_email(**args)

                    tool_outputs.append({
                        "type": "function_call_output",
                        "call_id": item.call_id,
                        "output": json.dumps(result, ensure_ascii=False)
                    })

            # 3단계: tool 결과를 모델에 다시 전달
            final_response = client.responses.create(
                model="gpt-4o-mini",
                previous_response_id=response.id,
                input=tool_outputs
            )
            answer = final_response.output_text

        # TTS: 답변 → 음성
        speech = client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="nova",
            input=answer
        )

    # 결과 출력
    st.subheader("요청 내용")
    st.write(transcript.text)

    st.subheader("AI 답변")
    st.write(answer)

    st.subheader("음성으로 듣기")
    st.audio(speech.content, format="audio/mp3")
