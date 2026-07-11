import os
import json
import re
import requests
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GOOGLE_SHEET_URL = os.environ["GOOGLE_SHEET_URL"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds_dict = json.loads(GOOGLE_CREDS_JSON)
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_url(GOOGLE_SHEET_URL)

sessions = {}


def gemini_generate(prompt, history=None):
    """ต่อท่อตรงผ่านข้อกำหนด HTTP API ของ Google (v1/gemini-1.5-flash)"""
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}

    contents = []
    if history:
        for t in history:
            role = "model" if t["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": t["content"]}]})

    contents.append({"role": "user", "parts": [{"text": prompt}]})

    payload = {"contents": contents}

    response = None
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        res_json = response.json()

        # NOTE: requests.Response is falsy when status_code >= 400, so we must
        # check `response is not None` rather than `if response:` or we silently
        # skip logging the real error body from Google.
        if response.status_code != 200:
            print(f"Gemini HTTP {response.status_code} — Response body: {response.text}")
            return "ขออภัยครับ ระบบประมวลผล AI เกิดข้อผิดพลาดชั่วคราว กรุณาลองใหม่อีกครั้ง"

        return res_json["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"Gemini API Error: {e}")
        if response is not None:
            print(f"Response body: {response.text}")
        return "ขออภัยครับ ระบบประมวลผล AI เกิดข้อผิดพลาดชั่วคราว กรุณาลองใหม่อีกครั้ง"


def get_questions():
    ws = sheet.worksheet("Questions")
    return ws.get_all_records()


def get_categories():
    ws = sheet.worksheet("Categories")
    return ws.get_all_records()


def analyze_jd(jd_text, job_type):
    try:
        questions = get_questions()
        categories = get_categories()
    except Exception as e:
        print(f"Error fetching sheets data: {e}")
        return ("เกิดข้อผิดพลาดในการดึงข้อมูลจาก Google Sheet กรุณาตรวจสอบสิทธิ์และการเชื่อมต่อ", [])

    relevant_q = [q for q in questions if q.get("job_type") == job_type]
    if not relevant_q:
        relevant_q = questions[:10]

    prompt = f"""คุณคือผู้เชี่ยวชาญวิเคราะห์ Job Description เพื่อทำนายคำถามสัมภาษณ์งานสายโรงงาน

Job Description ของบริษัท:
{jd_text}

รายการคำถามในคลัง (job_type: {job_type}):
{json.dumps(relevant_q, ensure_ascii=False)}

หมวดหมู่และน้ำหนักเริ่มต้น:
{json.dumps(categories, ensure_ascii=False)}

งานของคุณ:
1. วิเคราะห์ keyword ใน JD เทียบกับ keywords ของแต่ละคำถาม
2. ปรับ % โอกาสออก (base_prob_%) ขึ้นหรือลงตามความเกี่ยวข้องกับ JD จริง
3. เลือกคำถามที่มีโอกาสออกสูงสุด 5-8 ข้อ เรียงจากมากไปน้อย

ตอบกลับเป็นข้อความสั้น กระชับ อ่านง่ายในแชท LINE รูปแบบนี้:

📋 คำถามที่มีโอกาสออกสูงสุด (เรียงตาม %)

1. [ชื่อคำถาม] — XX%
   💡 แนวทางตอบ: [answer_guide แบบย่อ]

จบด้วยประโยค: "พร้อมเริ่มสัมภาษณ์จริงหรือยัง? พิมพ์ 'พร้อม' เพื่อเริ่ม"
"""
    text = gemini_generate(prompt)
    return text, relevant_q


def conduct_interview_turn(user_id, user_message):
    state = sessions[user_id]
    history = state["history"]

    if not history:
        system_context = f"""คุณคือผู้สัมภาษณ์งานมืออาชีพตำแหน่ง {state['job_type']}
กำลังสัมภาษณ์ผู้สมัครจริงจัง ใช้คำถามจากชุดนี้เป็นแนวทาง (ถามทีละข้อ ไม่ถามซ้ำข้อเดิม):
{json.dumps(state['selected_questions'] if state['selected_questions'] else [], ensure_ascii=False)}

กติกา:
- ถามทีละคำถามเท่านั้น สุภาพแต่จริงจังแบบสัมภาษณ์งานจริง
- หลังผู้สมัครตอบแต่ละข้อ ให้ถามคำถามถัดไปทันที ไม่ต้อง comment คำตอบระหว่างทาง
- ถ้าถามครบ {state['total_questions']} ข้อแล้ว ให้บอกว่า "สัมภาษณ์จบแล้วครับ กำลังประมวลผลคะแนน..." แล้วหยุด
- เริ่มด้วยคำถามแรกได้เลย
"""
        reply = gemini_generate(system_context)
        history.append({"role": "assistant", "content": reply})
        state["q_asked"] += 1
        return reply

    reply = gemini_generate(user_message, history=history)
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    state["q_asked"] += 1

    if state["q_asked"] >= state["total_questions"]:
        state["stage"] = "scoring"

    return reply


def generate_score_report(user_id):
    state = sessions[user_id]
    history = state["history"]

    prompt = f"""นี่คือบทสนทนาการสัมภาษณ์งานตำแหน่ง {state['job_type']} ทั้งหมด:
{json.dumps(history, ensure_ascii=False)}

ช่วยประเมินผลการสัมภาษณ์ทั้งหมด โดยตอบกลับแบบนี้:

📊 สรุปผลการสัมภาษณ์

คะแนนรวม: XX/100

✅ จุดแข็ง:
- ...

⚠️ จุดที่ควรปรับปรุง:
- ...

💡 คำแนะนำเพื่อพัฒนา:
- ...

จบด้วย: "ต้องการฝึกใหม่ไหม? พิมพ์ 'เริ่มใหม่' เพื่อสัมภาษณ์รอบใหม่ทั้งหมด หรือส่ง JD ใหม่ได้เลย"
"""
    report = gemini_generate(prompt)
    score_match = re.search(r"คะแนนรวม[:\s]*([0-9]+)", report)
    score = score_match.group(1) if score_match else "-"

    try:
        ws = sheet.worksheet("Sessions")
        ws.append_row([f"S{user_id[:8]}", user_id, "", state["job_type"], score, state["total_questions"], "จบแล้ว"])
    except Exception as e:
        print("Sheet write error:", e)

    return report


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    reply_token = event.reply_token

    state = sessions.get(user_id)
    reply = ""

    if text in ["เริ่มใหม่", "รีเซ็ต", "reset"]:
        sessions.pop(user_id, None)
        reply = "เริ่มใหม่ทั้งหมดแล้วครับ 🔄\nส่ง Job Description (JD) ของตำแหน่งที่จะสัมภาษณ์มาได้เลย พร้อมระบุว่าเป็น 'ช่างเทคนิค' หรือ 'วิศวกร'"
        sessions[user_id] = {"stage": "awaiting_jd"}

    elif state is None or state["stage"] == "awaiting_jd":
        job_type = "วิศวกร" if "วิศวกร" in text else "ช่างเทคนิค"
        analysis, selected_q = analyze_jd(text, job_type)
        sessions[user_id] = {
            "stage": "awaiting_ready",
            "job_type": job_type,
            "selected_questions": selected_q,
            "history": [],
            "q_asked": 0,
            "total_questions": min(5, len(selected_q)) if selected_q else 3,
        }
        reply = analysis

    elif state["stage"] == "awaiting_ready":
        if any(w in text for w in ["พร้อม", "ok", "ได้"]):
            state["stage"] = "interviewing"
            reply = conduct_interview_turn(user_id, "เริ่มสัมภาษณ์ได้เลยครับ")
        else:
            reply = "พิมพ์ 'พร้อม' เมื่อพร้อมเริ่มสัมภาษณ์ครับ"

    elif state["stage"] == "interviewing":
        reply = conduct_interview_turn(user_id, text)
        if state["stage"] == "scoring":
            score_report = generate_score_report(user_id)
            reply = reply + "\n\n" + score_report
            state["stage"] = "done"
    else:
        reply = "พิมพ์ 'เริ่มใหม่' เพื่อฝึกสัมภาษณ์รอบใหม่ หรือส่ง JD ใหม่ได้เลยครับ"

    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=reply[:4900])],
                )
            )
    except Exception as e:
        print(f"Error sending LINE reply message: {e}")


@app.route("/", methods=["GET"])
def health():
    return "Interview Bot is running on Raw HTTP Mode"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
