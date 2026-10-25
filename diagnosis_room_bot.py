import os
import io
import json
import random
import logging
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
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

# Fallback model chain: tries the newest stable models first, falling back if unavailable
MODEL_CHAIN = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-2.5-flash"]

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

def generate_gemini_response(prompt_text):
    """Loops through the model fallback chain with automatic retries for high demand / errors."""
    for model_id in MODEL_CHAIN:
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_id,
                    contents=prompt_text,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.7,
                    ),
                )
                if response and response.text:
                    return response.text
            except Exception as e:
                logger.warning(f"Model {model_id} attempt {attempt + 1} failed: {e}")
                if attempt < 1:
                    time.sleep(1)
        logger.info(f"Switching away from model {model_id}...")
    raise RuntimeError("All Gemini model fallback attempts failed due to high traffic.")

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
    "Symptom 1 with duration",
    "Symptom 2",
    "Symptom 3",
    "Symptom 4"
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
      "Exam finding 3"
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
    "Feature 3 -> mechanism"
  ],
  "next_step_management": "Clear summary of immediate management."
}}
"""

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
        "🩺 *Welcome to The Diagnosis Room!*\n\n"
        "Practice your clinical reasoning for MBBS exams with instant text-based cases.\n\n"
        "👇 *Select a specialty below to begin:*",
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
            prompt_text = CASE_GENERATION_PROMPT.format(specialty=spec, case_number=num)
            raw_json = generate_gemini_response(prompt_text)
            case_data = json.loads(raw_json)
            context.user_data["current_case"] = case_data

            p = case_data["patient_info"]
            symptoms_list = "\n".join([f"• {sym}" for sym in case_data["presenting_symptoms"]])
            
            case_text = (
                f"🏥 *THE DIAGNOSIS ROOM — CASE #{num}* ({spec.upper()})\n\n"
                f"👤 *PATIENT INFO*\n"
                f"• Age: {p['age']} | Gender: {p['gender']}\n"
                f"• History: {p['history']}\n\n"
                f"📋 *PRESENTING WITH*\n"
                f"{symptoms_list}\n\n"
                f"❓ *{case_data['step1_question']}*"
            )

            await query.message.reply_text(
                case_text, 
                parse_mode="Markdown", 
                reply_markup=build_mcq_keyboard(case_data["step1_options"], "step1")
            )
            await msg.delete()
        except Exception as e:
            logger.error(f"Error generation: {e}")
            await msg.edit_text("⚠️ High server traffic or connection delay. Tap /start to try a new case!")

    elif data.startswith("step1_"):
        choice = data.replace("step1_", "")
        c = context.user_data.get("current_case")
        if not c: return
        correct = c["step1_correct"]
        user_text = c["step1_options"].get(choice, "")
        correct_text = c["step1_options"].get(correct, "")

        if choice == correct:
            fb = f"✅ *Spot on! You chose {choice}) {user_text}*\n\n"
        else:
            fb = f"❌ *Not quite! You chose {choice}) {user_text}*\n✔️ *Best initial step:* {correct}) {correct_text}\n\n"
        fb += f"💡 *Clinical Reasoning:*\n{c['step1_explanation']}"
        await query.message.reply_text(fb, parse_mode="Markdown")

        assess = c["clinical_assessment"]
        vitals_list = "\n".join([f"• {v}" for v in assess["vitals"]])
        exam_list = "\n".join([f"• {e}" for e in assess["physical_exam"]])
        results_list = "\n".join([f"• {r}" for r in c["investigation_results"]])

        assessment_text = (
            f"🩺 *CLINICAL ASSESSMENT & RESULTS*\n\n"
            f"⚡ *VITALS*\n{vitals_list}\n\n"
            f"🔍 *PHYSICAL EXAM*\n{exam_list}\n\n"
            f"🧪 *RESULTS*\n{results_list}\n\n"
            f"❓ *{c['step2_question']}*"
        )

        await query.message.reply_text(
            assessment_text, 
            parse_mode="Markdown", 
            reply_markup=build_mcq_keyboard(c["step2_options"], "step2")
        )

    elif data.startswith("step2_"):
        choice = data.replace("step2_", "")
        c = context.user_data.get("current_case")
        if not c: return
        correct = c["step2_correct"]
        user_text = c["step2_options"].get(choice, "")
        correct_text = c["step2_options"].get(correct, "")

        if choice == correct:
            verdict = f"🎉 *Correct Diagnosis! ({choice}) {user_text}*"
        else:
            verdict = f"❌ *You selected {choice}) {user_text}*\n🎯 *The correct diagnosis is {correct}) {correct_text}*"
        await query.message.reply_text(verdict, parse_mode="Markdown")

        features_list = "\n".join([f"• {f}" for f in c["key_features_explained"]])

        final_text = (
            f"🏁 *THE DIAGNOSIS IS: {c['final_diagnosis']}*\n\n"
            f"📖 *Pathophysiology Summary:*\n{c['diagnosis_summary']}\n\n"
            f"🔑 *Key Features Explained:*\n{features_list}\n\n"
            f"💊 *Next Step / Management:*\n{c['next_step_management']}\n\n"
            f"💬 *Have questions about this case? Just type them below!*"
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➡️ Next Case", callback_data="spec_Mixed")],
            [InlineKeyboardButton("🗂 Choose Specialty", callback_data="start_menu")]
        ])
        await query.message.reply_text(final_text, parse_mode="Markdown", reply_markup=kb)

    elif data == "start_menu":
        await query.message.reply_text("👇 *Select a specialty:*", parse_mode="Markdown", reply_markup=build_specialty_keyboard())

async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = context.user_data.get("current_case")
    if not c:
        await update.message.reply_text("Tap /start to begin a case first!")
        return
    try:
        qa_text = generate_gemini_response(f"You are a warm clinical tutor helping an MBBS student. Case: {json.dumps(c)}. Student asks: {update.message.text}. Answer step-by-step.")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Next Case", callback_data="spec_Mixed")]])
        await update.message.reply_text(qa_text, reply_markup=kb)
    except Exception:
        await update.message.reply_text("⚠️ High server traffic. Please try asking your question again!")

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
