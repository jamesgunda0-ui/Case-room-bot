import os
import io
import json
import random
import logging
import textwrap
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests
from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from google import genai
from google.genai import types

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_ID = "gemini-2.5-flash"

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"The Diagnosis Room Bot is running!")
    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

CASE_GENERATION_PROMPT = """
You are an expert medical educator creating interactive clinical cases for clinical-year undergraduate medical (MBBS) students (400L - 600L level).
Generate a realistic, high-yield clinical case for the specialty: {specialty}.
Return ONLY valid JSON matching this exact structure:
{{
  "case_number": {case_number},
  "specialty": "{specialty}",
  "patient_info": {{
    "age": "24",
    "gender": "Male",
    "history": "No significant medical history"
  }},
  "presenting_symptoms": [
    "Symptom 1",
    "Symptom 2",
    "Symptom 3",
    "Symptom 4",
    "Symptom 5"
  ],
  "step1_question": "WHAT WOULD YOU DO NEXT?",
  "step1_options": {{
    "A": "Option A text",
    "B": "Option B text",
    "C": "Option C text",
    "D": "Option D text"
  }},
  "step1_correct": "A",
  "step1_explanation": "Explanation of why this step is correct.",
  "clinical_assessment": {{
    "vitals": [
      "Heart Rate: 112 bpm",
      "Blood Pressure: 100/65 mmHg",
      "Temperature: 38.2°C",
      "Respiratory Rate: 20/min",
      "SpO2: 98%"
    ],
    "physical_exam": [
      "Exam finding 1",
      "Exam finding 2",
      "Exam finding 3",
      "Exam finding 4"
    ]
  }},
  "investigation_results": [
    "Key result 1 ↑",
    "Key result 2",
    "Key result 3"
  ],
  "step2_question": "WHAT'S YOUR DIAGNOSIS?",
  "step2_options": {{
    "A": "Diagnosis A",
    "B": "Diagnosis B",
    "C": "Diagnosis C",
    "D": "Diagnosis D"
  }},
  "step2_correct": "A",
  "final_diagnosis": "EXACT DIAGNOSIS IN CAPS",
  "diagnosis_summary": "1-2 sentences explaining pathophysiology.",
  "key_features_explained": [
    "Feature 1 -> mechanism",
    "Feature 2 -> mechanism",
    "Feature 3 -> mechanism",
    "Feature 4 -> mechanism"
  ],
  "next_step_management": "Clear summary of immediate management.",
  "illustration_prompt_1": "minimalist flat vector medical icon of a stethoscope on pure solid white background",
  "illustration_prompt_2": "minimalist flat vector medical icon of a patient monitor on pure solid white background",
  "illustration_prompt_3": "minimalist flat vector medical icon of a first aid kit on pure solid white background"
}}
"""

def fetch_ai_illustration(prompt: str, size=(260, 260)):
    try:
        encoded = urllib.parse.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=512&height=512&nologo=true&seed={random.randint(1, 99999)}"
        res = requests.get(url, timeout=12)
        if res.status_code == 200:
            return Image.open(io.BytesIO(res.content)).convert("RGBA").resize(size, Image.Resampling.LANCZOS)
    except Exception:
        pass
    return None

def get_font(size: int, bold: bool = False):
    for path in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arialbd.ttf" if bold else "arial.ttf"]:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()

def create_slide(title, sections, footer, illustration=None):
    img = Image.new("RGB", (1080, 1600), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    
    draw.text((540, 70), "THE DIAGNOSIS ROOM", fill=(120, 130, 150), font=get_font(28, True), anchor="mm")
    draw.text((540, 145), title.upper(), fill=(8, 20, 72), font=get_font(52, True), anchor="mm")
    
    y = 230
    for sec_title, items, centered in sections:
        if sec_title:
            if centered:
                draw.text((540, y), sec_title, fill=(8, 20, 72), font=get_font(36, True), anchor="mm")
            else:
                draw.text((110, y), sec_title, fill=(8, 20, 72), font=get_font(36, True))
            y += 58
        for item in items:
            for i, line in enumerate(textwrap.wrap(str(item), width=42)):
                if centered:
                    draw.text((540, y), line, fill=(8, 20, 72), font=get_font(30, True), anchor="mm")
                else:
                    no_bullet = str(item).startswith(("A)", "B)", "C)", "D)", "Age:", "Gender:", "History:"))
                    prefix = "" if no_bullet else ("• " if i == 0 else "   ")
                    draw.text((130, y), f"{prefix}{line}", fill=(8, 20, 72), font=get_font(30, True))
                y += 44
            y += 8
        y += 32

    if illustration:
        ill = fetch_ai_illustration(illustration, (260, 260))
        if ill:
            py = max(y + 10, 1260)
            img.paste(ill, ((1080 - 260) // 2, py), ill)

    draw.text((540, 1560), footer, fill=(120, 130, 150), font=get_font(24), anchor="mm")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

def build_specialty_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Mixed (Surgery + Medicine + All)", callback_data="spec_Mixed")],
        [InlineKeyboardButton("🔪 General & Clinical Surgery", callback_data="spec_Surgery")],
        [InlineKeyboardButton("🩺 Internal Medicine", callback_data="spec_Internal Medicine")],
        [InlineKeyboardButton("👶 Pediatrics", callback_data="spec_Pediatrics")],
        [InlineKeyboardButton("🤰 Obstetrics & Gynecology", callback_data="spec_Obstetrics and Gynecology")],
    ])

def build_mcq_keyboard(options, prefix):
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{k}) {v}", callback_data=f"{prefix}_{k}")] for k, v in options.items()])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🩺 *Welcome to The Diagnosis Room!*\n\nPractice your clinical reasoning for MBBS exams with interactive cases.\n\n👇 *Select a specialty below to begin:*",
        parse_mode="Markdown", reply_markup=build_specialty_keyboard()
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("spec_"):
        spec = data.replace("spec_", "")
        if spec == "Mixed":
            spec = random.choice(["General Surgery", "Internal Medicine", "Emergency Medicine", "Pediatrics", "Obstetrics & Gynecology"])
        
        num = context.user_data.get("case_counter", 0) + 1
        context.user_data["case_counter"] = num
        msg = await query.message.reply_text(f"⏳ *Generating Case #{num} ({spec})...*", parse_mode="Markdown")

        try:
            resp = client.models.generate_content(
                model=MODEL_ID,
                contents=CASE_GENERATION_PROMPT.format(specialty=spec, case_number=num),
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.7)
            )
            case_data = json.loads(resp.text)
            context.user_data["current_case"] = case_data

            p = case_data["patient_info"]
            slide = create_slide(f"CASE #{num} ({spec.upper()})", [
                ("PATIENT INFO", [f"Age: {p['age']}", f"Gender: {p['gender']}", f"History: {p['history']}"], False),
                ("PRESENTING WITH", case_data["presenting_symptoms"], False),
                (case_data["step1_question"], [f"{k}) {v}" for k, v in case_data["step1_options"].items()], False)
            ], f"Clinical Case #{num} • Step 1 of 3", case_data.get("illustration_prompt_1"))

            await query.message.reply_photo(photo=slide, caption=f"📋 *Case #{num} — {spec}*\n\n*{case_data['step1_question']}*", parse_mode="Markdown", reply_markup=build_mcq_keyboard(case_data["step1_options"], "step1"))
            await msg.delete()
        except Exception as e:
            logger.error(f"Error: {e}")
            await msg.edit_text("⚠️ Error generating case. Tap /start to try again.")

    elif data.startswith("step1_"):
        choice = data.replace("step1_", "")
        c = context.user_data.get("current_case")
        if not c: return
        correct = c["step1_correct"]
        fb = f"✅ *Correct!*" if choice == correct else f"❌ *Not quite.* Correct step: {correct})"
        fb += f"\n\n💡 *Reasoning:*\n{c['step1_explanation']}"
        await query.message.reply_text(fb, parse_mode="Markdown")

        assess = c["clinical_assessment"]
        slide = create_slide("CLINICAL ASSESSMENT", [
            ("VITALS:", assess["vitals"], False),
            ("PHYSICAL EXAM:", assess["physical_exam"], False),
            ("RESULTS:", c["investigation_results"], False),
            (c["step2_question"], [f"{k}) {v}" for k, v in c["step2_options"].items()], False)
        ], f"Clinical Case #{c['case_number']} • Step 2 of 3", c.get("illustration_prompt_2"))

        await query.message.reply_photo(photo=slide, caption=f"🩺 *Assessment & Results*\n\n*{c['step2_question']}*", parse_mode="Markdown", reply_markup=build_mcq_keyboard(c["step2_options"], "step2"))

    elif data.startswith("step2_"):
        choice = data.replace("step2_", "")
        c = context.user_data.get("current_case")
        if not c: return
        correct = c["step2_correct"]
        verdict = f"🎉 *Correct Diagnosis!*" if choice == correct else f"❌ *Incorrect.* Correct diagnosis: {correct})"
        await query.message.reply_text(verdict, parse_mode="Markdown")

        slide = create_slide("THE DIAGNOSIS IS...", [
            (c["final_diagnosis"], [c["diagnosis_summary"]], True),
            ("KEY CLINICAL FEATURES:", c["key_features_explained"], False),
            ("NEXT STEP / MANAGEMENT:", [c["next_step_management"]], True)
        ], f"Clinical Case #{c['case_number']} • Step 3 of 3", c.get("illustration_prompt_3"))

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Next Case", callback_data="spec_Mixed")], [InlineKeyboardButton("🗂 Choose Specialty", callback_data="start_menu")]])
        await query.message.reply_photo(photo=slide, caption=f"🏁 *Diagnosis: {c['final_diagnosis']}*\n\n💬 *Have questions? Just type them below!*", parse_mode="Markdown", reply_markup=kb)

    elif data == "start_menu":
        await query.message.reply_text("👇 *Select a specialty:*", parse_mode="Markdown", reply_markup=build_specialty_keyboard())

async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = context.user_data.get("current_case")
    if not c:
        await update.message.reply_text("Tap /start to begin a case first!")
        return
    resp = client.models.generate_content(model=MODEL_ID, contents=f"You are a warm clinical tutor. Case: {json.dumps(c)}. Student asks: {update.message.text}. Answer step-by-step.")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Next Case", callback_data="spec_Mixed")]])
    await update.message.reply_text(resp.text, reply_markup=kb)

def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("newcase", start_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_question))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
