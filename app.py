
import os
import io
import html
import hashlib

import streamlit as st
from openai import OpenAI
from streamlit_mic_recorder import mic_recorder
import json
import base64
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from identity_settings_addon import show_identity_settings_dialog


# ==========================================
# 1. 페이지 설정
# ==========================================
st.set_page_config(
    page_title="ThumbsUp AI",
    page_icon="👍🏻",
    layout="wide"
)


# ==========================================
# 2. CSS 디자인
# ==========================================
st.markdown("""
<style>
.block-container {
    max-width: 1000px;
    padding-top: 2rem;
    padding-bottom: 3rem;
}

header[data-testid="stHeader"] {
    background: transparent;
}

.voice-title {
    text-align: center;
    font-size: 32px;
    font-weight: 750;
    margin-top: 35px;
}

.voice-subtitle {
    text-align: center;
    color: #888;
    margin-bottom: 24px;
}

.mic-caption {
    text-align: center;
    color: #858b97;
    margin: 10px 0 26px;
}

.answer-box {
    background: #f7f8fc;
    border: 1px solid #e9ecf3;
    border-radius: 18px;
    padding: 20px 24px;
    line-height: 1.75;
    color: #303744;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
}

.answer-collapsed {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    overflow: hidden;
}
</style>
""", unsafe_allow_html=True)


# ==========================================
# 3. 세션 상태 초기화
# ==========================================
defaults = {
    "answer": "",
    "transcript": "",
    "answer_audio": None,
    "answer_expanded": False,
    "history": [],
    "last_processed_audio": None,
}

for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ==========================================
# 4. OpenAI 클라이언트
# ==========================================
def get_client():
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        try:
            api_key = st.secrets.get("OPENAI_API_KEY")
        except Exception:
            api_key = None

    if not api_key:
        return None

    return OpenAI(api_key=api_key)


# ==========================================
# 5. AI 시스템 역할
# ==========================================
SYSTEM_PROMPT = """
당신은 ThumpsUp AI라는 한국어 음성 비서입니다.

역할:
1. 사용자의 음성 명령을 이해합니다.
2. 질문에 친절하고 정확하게 답변합니다.
3. 음성으로 듣기 편하도록 간결하게 설명합니다.
4. 첨부된 텍스트 파일을 참고할 수 있습니다.
5. 확실하지 않은 정보는 추측하지 않습니다.

주의:
웹 검색, 실시간 주식 조회, Google Calendar 등록 및
조회 기능은 아직 연결되지 않았습니다.

실제로 실행하지 않은 작업을 완료했다고 말하지 마세요.
"""


# [명함 연락처 기능]
CONTACT_SCOPES = ["https://www.googleapis.com/auth/contacts"]

CARD_PROMPT = """
사용자가 명함 이미지의 연락처 저장을 요청하면 save_contact 도구를 사용합니다.
명함에 적힌 정보만 사용하고, 명함에 없는 항목은 빈 문자열로 입력합니다.
저장 요청이 아니면 도구를 사용하지 않습니다.
도구 결과가 failed이면 저장에 실패했다고 답합니다.
"""

CONTACT_TOOLS = [{
    "type": "function",
    "name": "save_contact",
    "description": "명함에서 읽은 정보를 Google 연락처에 저장합니다.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "이름"},
            "phone": {"type": "string", "description": "전화번호. 없으면 빈 문자열"},
            "email": {"type": "string", "description": "이메일. 없으면 빈 문자열"},
            "company": {"type": "string", "description": "회사명. 없으면 빈 문자열"},
            "title": {"type": "string", "description": "직책. 없으면 빈 문자열"}
        },
        "required": ["name", "phone", "email", "company", "title"],
        "additionalProperties": False
    },
    "strict": True
}]


def save_contact(name, phone, email, company, title):
    try:
        creds = Credentials.from_authorized_user_info(
            json.loads(st.secrets["GOOGLE_TOKEN"]),
            CONTACT_SCOPES
        )
        service = build("people", "v1", credentials=creds)

        contact = {"names": [{"givenName": name}]}
        if phone:
            contact["phoneNumbers"] = [{"value": phone}]
        if email:
            contact["emailAddresses"] = [{"value": email}]
        if company or title:
            contact["organizations"] = [{"name": company, "title": title}]

        service.people().createContact(body=contact).execute()
        return {"status": "success"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


def create_answer(client, conversation, image):
    st.session_state.contact_results = []

    # 명함이 없으면 기존과 동일하게 답변
    if image is None:
        response = client.responses.create(
            model="gpt-4o-mini",
            input=conversation
        )
        return response.output_text.strip()

    # 1단계: 질문 + 명함 이미지 + 도구 정의 전달
    encoded = base64.b64encode(image.getvalue()).decode("utf-8")
    response = client.responses.create(
        model="gpt-4o-mini",
        input=conversation[:-1] + [
            {"role": "system", "content": CARD_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": conversation[-1]["content"]},
                    {"type": "input_image", "image_url": f"data:{image.type};base64,{encoded}"}
                ]
            }
        ],
        tools=CONTACT_TOOLS,
        tool_choice="auto"
    )

    tool_calls = [
        item for item in response.output
        if item.type == "function_call"
    ]

    if not tool_calls:
        return response.output_text.strip()

    # 2단계: 모델이 요청한 tool 실행
    tool_outputs = []
    for item in tool_calls:
        args = json.loads(item.arguments)
        result = save_contact(**args)
        st.session_state.contact_results.append((args, result))
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
    return final_response.output_text.strip()


# ==========================================
# 6. 음성 처리 함수
# ==========================================
def process_audio(audio_bytes, reference_text):

    client = get_client()

    if client is None:
        st.error("OPENAI_API_KEY가 설정되지 않았습니다.")
        return

    try:
        # ----------------------------------
        # STT: 음성 -> 텍스트
        # ----------------------------------
        with st.spinner("음성을 인식하고 있습니다..."):

            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = "recording.wav"

            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )

            question = result.text.strip()

        if not question:
            st.warning("음성이 인식되지 않았습니다.")
            return

        # ----------------------------------
        # AI 대화 메시지 구성
        # ----------------------------------
        conversation = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            }
        ]

        if reference_text:
            conversation.append({
                "role": "system",
                "content": (
                    "참고 파일 내용:\n"
                    + reference_text[:15000]
                )
            })

        # 이전 대화 유지
        conversation.extend(
            st.session_state.history[-8:]
        )

        conversation.append({
            "role": "user",
            "content": question
        })

        # ----------------------------------
        # LLM 답변 생성
        # ----------------------------------
        with st.spinner("ThumpsUp AI가 답변 중입니다..."):

            answer = create_answer(client, conversation, card_image)

        # ----------------------------------
        # TTS: 텍스트 -> 음성
        # ----------------------------------
        answer_audio = None

        try:
            with st.spinner("음성 답변을 생성하고 있습니다..."):

                speech = client.audio.speech.create(
                    model="tts-1",
                    voice="nova",
                    input=answer[:4000]
                )

                answer_audio = speech.read()

        except Exception as e:
            st.warning(f"음성 합성 오류: {e}")

        # ----------------------------------
        # 결과 저장
        # ----------------------------------
        st.session_state.transcript = question
        st.session_state.answer = answer
        st.session_state.answer_audio = answer_audio
        st.session_state.answer_expanded = False

        st.session_state.history.extend([
            {
                "role": "user",
                "content": question
            },
            {
                "role": "assistant",
                "content": answer
            }
        ])

    except Exception as e:
        st.error(f"AI 처리 중 오류: {e}")


# ==========================================
# 7. 상단 헤더 + 설정 버튼
# ==========================================
brand_col, settings_col = st.columns(
    [12, 1],
    vertical_alignment="center"
)

with brand_col:
    st.markdown("### 👍🏻 ThumpsUp AI")

with settings_col:
    if st.button(
        "⚙️",
        key="settings_button",
        help="설정"
    ):
        show_identity_settings_dialog()


# ==========================================
# 8. 중앙 안내 문구
# ==========================================
st.markdown("""
<div class="voice-title">
    무엇을 도와드릴까요?
</div>

<div class="voice-subtitle">
    음성으로 말씀하시면 ThumpsUp AI가 답변합니다.
</div>
""", unsafe_allow_html=True)


# ==========================================
# 9. TXT 파일 업로드
# ==========================================
txt_col, card_col = st.columns(2)

with txt_col:
    with st.expander("📎 참고 TXT 파일 첨부 (선택)"):

        uploaded_file = st.file_uploader(
            "텍스트 파일",
            type=["txt"],
            key="txt_file"
        )

        if uploaded_file is not None:
            st.success(
                f"{uploaded_file.name} 파일이 준비되었습니다."
            )

with card_col:
    with st.expander("🪪 명함 이미지 첨부 (선택)"):

        card_image = st.file_uploader(
            "명함 이미지",
            type=["png", "jpg", "jpeg"],
            key="card_image"
        )

        if card_image is not None:
            st.image(card_image.getvalue(), width=280)

reference_text = ""

if uploaded_file is not None:
    reference_text = uploaded_file.getvalue().decode(
        "utf-8-sig",
        errors="replace"
    )


# ==========================================
# 10. 중앙 음성 녹음 버튼
# ==========================================
left, mic_col, right = st.columns([3, 2, 3])

with mic_col:

    audio = mic_recorder(
        start_prompt="🎙️ 녹음 시작",
        stop_prompt="⏹️ 녹음 종료",
        just_once=False,
        use_container_width=True,
        key="voice_recorder"
    )

st.markdown("""
<div class="mic-caption">
    마이크를 눌러 녹음을 시작하고,
    다시 눌러 종료하세요.
</div>
""", unsafe_allow_html=True)


# ==========================================
# 11. 음성 녹음 자동 처리
# ==========================================
if audio and audio.get("bytes"):

    audio_bytes = audio["bytes"]

    # 동일한 녹음을 반복 처리하지 않도록 방지
    audio_hash = hashlib.sha256(
        audio_bytes
    ).hexdigest()

    if audio_hash != st.session_state.last_processed_audio:

        st.session_state.last_processed_audio = audio_hash

        process_audio(
            audio_bytes,
            reference_text
        )


# ==========================================
# 12. AI 답변 표시
# ==========================================
if st.session_state.answer:
    for contact, result in st.session_state.get("contact_results", []):
        if result["status"] == "success":
            st.success("Google 연락처에 저장했습니다.")
            st.json(contact)
        else:
            st.error(f"연락처 저장 실패: {result['error']}")

st.subheader("👍🏻 AI 답변")

answer = st.session_state.answer

if answer:

    # HTML 특수문자 변환
    safe_answer = html.escape(answer)

    # 펼침 상태
    if st.session_state.answer_expanded:
        collapsed_class = ""
    else:
        collapsed_class = "answer-collapsed"

    # 답변 출력
    st.markdown(
        f"""
        <div class="answer-box">
            <div class="{collapsed_class}">
                {safe_answer}
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    # 긴 답변에만 더보기 표시
    # 글자 수 기반의 근사 판정
    is_long = (
        len(answer) > 85
        or answer.count("\n") >= 2
    )

    if is_long:

        if st.session_state.answer_expanded:
            button_text = "접기 ▲"
        else:
            button_text = "더보기 ▼"

        if st.button(
            button_text,
            key="toggle_answer"
        ):

            st.session_state.answer_expanded = (
                not st.session_state.answer_expanded
            )

            st.rerun()

    # AI 음성 답변 재생
    if st.session_state.answer_audio:

        st.audio(
            st.session_state.answer_audio,
            format="audio/mp3"
        )

else:
    st.info(
        "음성으로 질문하면 AI 답변이 여기에 표시됩니다."
    )


# ==========================================
# 13. 인식된 음성 확인
# ==========================================
if st.session_state.transcript:

    with st.expander("인식된 음성 확인"):

        st.write(
            st.session_state.transcript
        )


# ==========================================
# 14. 최근 대화 기록
# ==========================================
with st.expander("최근 대화 기록"):

    if not st.session_state.history:

        st.caption("아직 대화 기록이 없습니다.")

    else:

        for message in st.session_state.history[-10:]:

            if message["role"] == "user":
                who = "나"
            else:
                who = "ThumpsUp AI"

            st.markdown(f"**{who}**")
            st.write(message["content"])

        if st.button("대화 초기화"):

            reset_values = {
                "answer": "",
                "transcript": "",
                "answer_audio": None,
                "answer_expanded": False,
                "history": [],
                "last_processed_audio": None
            }

            for key, value in reset_values.items():
                st.session_state[key] = value

            st.rerun()
