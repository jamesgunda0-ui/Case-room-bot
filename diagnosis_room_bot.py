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

# ==============================================================================
# CONFIGURATION & LOGGING
# ==============================================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# .strip() removes any accidental trailing spaces added by mobile keyboards
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_ID = "gemini-2.5-flash"


# ==============================================================================
# BUILT-IN RENDER HEALTH CHECK SERVER (KEEPS RENDER WEB SERVICE HAPPY)
# ==============================================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"The Diagnosis Room Bot is running!")

    def log_message(self, format, *args):
        return  # Silence HTTP logs


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Health check server listening on port {port}")
    server.serve_forever()


# ==============================================================================
# SYSTEM PROMPT FOR UNDERGRADUATE MBBS CLINICAL CASES
# ==============================================================================
CASE_GENERATION_PROMPT = """
You are an expert medical educator creating interactive clinical cases for clinical-year 
undergraduate medical (MBBS) students (400L - 600L level).

Generate a realistic, high-yield clinical case for the specialty: {specialty}.
The difficulty MUST be strictly tailored to an undergraduate medical student preparing for 
ward rounds and MBBS exams (e.g., Acute Appendicitis, Intestinal Obstruction, Peptic Ulcer Perforation, 
Cholecystitis, Testicular Torsion, Strangulated Hernia, Diabetic Ketoacidosis, Severe Malaria, 
Community-Acquired Pneumonia, Congestive Heart Failure, Acute Severe Asthma, Ectopic Pregnancy, 
Pre-eclampsia, Meningitis, Dehydration/Hypovolemia, Upper GI Bleeding, etc.).
Do NOT generate ultra-rare fellowship-level zebras.

Return ONLY valid JSON matching this exact structure:
{{
  "case_number": {case_number},
  "specialty": "{specialty}",
  "patient_info": {{
    "age": "e.g., 24",
    "gender": "Male or Female",
    "history": "Brief relevant past medical/surgical history or 'No significant medical history'"
  }},
  "presenting_symptoms": [
    "Symptom 1 (with duration/character)",
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
  "step1_correct": "A, B, C, or D",
  "step1_explanation": "Clear, concise undergraduate-level explanation of why the correct initial step is prioritized and why the others are less appropriate right now.",
  "clinical_assessment": {{
    "vitals": [
      "Heart Rate: e.g., 112 bpm",
      "Blood Pressure: e.g., 100/65 mmHg",
      "Temperature: e.g., 38.2°C",
      "Respiratory Rate: e.g., 20/min",
      "SpO2: e.g., 98%"
    ],
    "physical_exam": [
      "Exam finding 1",
      "Exam finding 2",
      "Exam finding 3",
      "Exam finding 4"
    ]
  }},
  "investigation_results": [
    "Key lab or bedside result 1 (with normal/abnormal arrow like ↑ or ↓)",
    "Key lab or imaging result 2",
    "Key lab or imaging result 3"
  ],
  "step2_question": "WHAT'S YOUR DIAGNOSIS?",
  "step2_options": {{
    "A": "Diagnosis A",
    "B": "Diagnosis B",
    "C": "Diagnosis C",
    "D": "Diagnosis D"
  }},
  "step2_correct": "A, B, C, or D",
  "final_diagnosis": "EXACT NAME OF DIAGNOSIS IN CAPS",
  "diagnosis_summary": "1-2 sentences explaining the core pathophysiology in simple, clear terms.",
  "key_features_explained": [
    "Feature 1 -> brief mechanism",
    "Feature 2 -> brief mechanism",
    "Feature 3 -> brief mechanism",
    "Feature 4 -> brief mechanism"
  ],
  "next_step_management": "Clear summary of immediate resuscitation/definitive management plan (e.g., IV fluids, NPO, antibiotics, surgical intervention).",
  "illustration_prompt_1": "minimalist flat vector medical icon of a stethoscope and clipboard on a pure solid white background, simple clean lines, no text",
  "illustration_prompt_2": "minimalist flat vector medical icon of a patient monitor and blood sample tube on a pure solid white background, simple clean lines, no text",
  "illustration_prompt_3": "minimalist flat vector medical icon of a red first aid kit and medical cross on a pure solid white background, simple clean lines, no text"
}}
"""


# ==============================================================================
# AI ILLUSTRATION + "THE DIAGNOSIS ROOM" SLIDE GENERATOR
# ==============================================================================
def fetch_ai_illustration(prompt: str, size=(260, 260)):
    """Fetches a free AI-generated minimalist medical illustration."""
    try:
        encoded_prompt = urllib.parse.quote(prompt)
        seed = random.randint(1, 99999)
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=512&height=512&nologo=true&seed={seed}"
        response = requests.get(url, timeout=12)
        if response.status_code == 200:
            img = Image.open(io.BytesIO(response.content)).convert("RGBA")
            return img.resize(size, Image.Resampling.LANCZOS)
    except Exception as e:
        logger.warning(f"Could not fetch AI illustration: {e}")
    return None


def get_font(size: int, bold: bool = False):
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "arialbd.ttf" if bold else "arial.ttf",
    ]
    for path in font_candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def create_slide_image(
    header_title: str,
    sections: list,
    footer_text: str,
    illustration_prompt: str = None,
) -> io.BytesIO:
    width, height = 1080, 1600
    bg_color = (255, 255, 255)
    navy = (8, 20, 72)
    muted_gray = (120, 130, 150)

    img = Image.new("RGB", (width, height), color=bg_color)
    draw = ImageDraw.Draw(img)

    font_tag = get_font(28, bold=True)
    font_header = get_font(52, bold=True)
    font_section = get_font(36, bold=True)
    font_body_bold = get_font(30, bold=True)
    font_footer = get_font(24, bold=False)

    draw.text((width // 2, 70), "THE DIAGNOSIS ROOM", fill=muted_gray, font=font_tag, anchor="mm")
    draw.text((width // 2, 145), header_title.upper(), fill=navy, font=font_header, anchor="mm")

    y_cursor = 230
    for sec_title, items, is_centered in sections:
        if sec_title:
            if is_centered:
                draw.text((width // 2, y_cursor), sec_title, fill=navy, font=font_section, anchor="mm")
            else:
                draw.text((110, y_cursor), sec_title, fill=navy, font=font_section)
            y_cursor += 58

        for item in items:
            wrapped_lines = textwrap.wrap(str(item), width=42)
            for i, line in enumerate(wrapped_lines):
                if is_centered:
                    draw.text((width // 2, y_cursor), line, fill=navy, font=font_body_bold, anchor="mm")
                else:
                    no_bullet = str(item).startswith(("A)", "B)", "C)", "D)", "Age:", "Gender:", "History:"))
                    prefix = "" if no_bullet else ("• " if i == 0 else "   ")
                    draw.text((130, y_cursor), f"{prefix}{line}", fill=navy, font=font_body_bold)
                y_cursor += 44
            y_cursor += 8
        y_cursor += 32

    if illustration_prompt:
        illust = fetch_ai_illustration(illustration_prompt, size=(260, 260))
        if illust:
            paste_y = max(y_cursor + 10, height - 340)
            if paste_y + 260 < height - 60:
                img.paste(illust, ((width - 260) // 2, paste_y), illust if illust.mode == "RGBA" else None)

    draw.text((width // 2, height - 40), footer_text, fill=muted_gray, font=font_footer, anchor="mm")

    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio


def render_card_1_presentation(case: dict) -> io.BytesIO:
    p_info = case["patient_info"]
    sections = [
        (
            "PATIENT INFO",
            [
                f"Age: {p_info['age']}",
                f"Gender: {p_info['gender']}",
                f"History: {p_info['history']}",
            ],
            False,
        ),
        ("PRESENTING WITH", case["presenting_symptoms"], False),
        (
            case["step1_question"],
            [f"{k}) {v}" for k, v in case["step1_options"].items()],
            False,
        ),
    ]
    return create_slide_image(
        header_title=f"CASE #{case['case_number']} ({case['specialty'].upper()})",
        sections=sections,
        footer_text=f"Clinical Case #{case['case_number']} • Step 1 of 3",
        illustration_prompt=case.get("illustration_prompt_1"),
    )


def render_card_2_assessment(case: dict) -> io.BytesIO:
    assess = case["clinical_assessment"]
    sections = [
        ("VITALS:", assess["vitals"], False),
        ("PHYSICAL EXAM:", assess["physical_exam"], False),
        ("RESULTS:", case["investigation_results"], False),
        (
            case["step2_question"],
            [f"{k}) {v}" for k, v in case["step2_options"].items()],
            False,
        ),
    ]
    return create_slide_image(
        header_title="CLINICAL ASSESSMENT",
        sections=sections,
        footer_text=f"Clinical Case #{case['case_number']} • Step 2 of 3",
        illustration_prompt=case.get("illustration_prompt_2"),
    )


def render_card_3_diagnosis(case: dict) -> io.BytesIO:
    sections = [
        (case["final_diagnosis"], [case["diagnosis_summary"]], True),
        ("KEY CLINICAL FEATURES:", case["key_features_explained"], False),
        ("NEXT STEP / MANAGEMENT:", [case["next_step_management"]], True),
    ]
    return create_slide_image(
        header_title="THE DIAGNOSIS IS...",
        sections=sections,
        footer_text=f"Clinical Case #{case['case_number']} • Step 3 of 3",
        illustration_prompt=case.get("illustration_prompt_3"),
    )


# ==============================================================================
# KEYBOARD BUILDERS (EACH OPTION ON ITS OWN ROW)
# ==============================================================================
def build_specialty_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🎲 Mixed (Surgery + Medicine + All)", callback_data="spec_Mixed")],
        [InlineKeyboardButton("🔪 General & Clinical Surgery", callback_data="spec_Surgery")],
        [InlineKeyboardButton("🩺 Internal Medicine", callback_data="spec_Internal Medicine")],
        [InlineKeyboardButton("👶 Pediatrics", callback_data="spec_Pediatrics")],
        [InlineKeyboardButton("🤰 Obstetrics & Gynecology", callback_data="spec_Obstetrics and Gynecology")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_mcq_keyboard(options: dict, prefix: str) -> InlineKeyboardMarkup:
    keyboard = []
    for key, text in options.items():
        keyboard.append([InlineKeyboardButton(f"{key}) {text}", callback_data=f"{prefix}_{key}")])
    return InlineKeyboardMarkup(keyboard)


# ==============================================================================
# TELEGRAM BOT HANDLERS
# ==============================================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = """🩺 *Welcome to The Diagnosis Room!*

Practice your clinical reasoning, clerking, and diagnostic skills with interactive undergraduate MBBS cases.

How it works:
1️⃣ Review the *Patient Info & Presenting Complaints* slide.
2️⃣ Decide *What to do next*.
3️⃣ Analyze the *Vitals, Physical Exam & Lab Results* slide.
4️⃣ Lock in your *Final Diagnosis*.
5️⃣ Ask any follow-up questions about the case!

👇 *Choose a posting/specialty below to start a new case:*"""
    await update.message.reply_text(
        welcome_text, parse_mode="Markdown", reply_markup=build_specialty_keyboard()
    )


async def new_case_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👇 *Select a specialty for your next clinical case:*",
        parse_mode="Markdown",
        reply_markup=build_specialty_keyboard(),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("spec_"):
        specialty = data.replace("spec_", "")
        if specialty == "Mixed":
            specialty = random.choice(
                ["General Surgery", "Internal Medicine", "Emergency Medicine", "Pediatrics", "Obstetrics & Gynecology"]
            )

        case_num = context.user_data.get("case_counter", 0) + 1
        context.user_data["case_counter"] = case_num

        status_msg = await query.message.reply_text(
            f"⏳ *Preparing Case #{case_num} ({specialty})...*\nGenerating clinical slides & AI illustration...",
            parse_mode="Markdown",
        )

        try:
            prompt = CASE_GENERATION_PROMPT.format(specialty=specialty, case_number=case_num)
            response = client.models.generate_content(
                model=MODEL_ID,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.7,
                ),
            )
            case_data = json.loads(response.text)
            context.user_data["current_case"] = case_data
            context.user_data["stage"] = "WAIT_STEP_1"

            slide_1 = render_card_1_presentation(case_data)
            caption = (
                f"📋 *Case #{case_num} — {specialty}*\n"
                f"*{case_data['step1_question']}*\n\n"
                f"Select your next clinical step below:"
            )
            await query.message.reply_photo(
                photo=slide_1,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=build_mcq_keyboard(case_data["step1_options"], "step1"),
            )
            await status_msg.delete()

        except Exception as e:
            logger.error(f"Error generating case: {e}")
            await status_msg.edit_text(
                "⚠️ Sorry, there was an error generating the case. Please tap /newcase to try again."
            )

    elif data.startswith("step1_"):
        choice = data.replace("step1_", "")
        case_data = context.user_data.get("current_case")
        if not case_data:
            await query.message.reply_text("Please start a new case with /newcase.")
            return

        correct = case_data["step1_correct"]
        correct_text = case_data["step1_options"].get(correct, "")
        user_text = case_data["step1_options"].get(choice, "")

        if choice == correct:
            feedback = f"✅ *Spot on! You chose {choice}) {user_text}*\n\n"
        else:
            feedback = (
                f"❌ *Not quite! You chose {choice}) {user_text}*\n"
                f"✔️ *Best initial step:* {correct}) {correct_text}\n\n"
            )
        feedback += (
            f"💡 *Clinical Reasoning:*\n{case_data['step1_explanation']}\n\n"
            f"⏳ *Loading Clinical Assessment & Results...*"
        )

        await query.message.reply_text(feedback, parse_mode="Markdown")

        slide_2 = render_card_2_assessment(case_data)
        context.user_data["stage"] = "WAIT_STEP_2"
        caption = (
            f"🩺 *Clinical Assessment & Investigation Results*\n\n"
            f"*{case_data['step2_question']}*\n"
            f"Lock in your diagnosis below 👀:"
        )
        await query.message.reply_photo(
            photo=slide_2,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=build_mcq_keyboard(case_data["step2_options"], "step2"),
        )

    elif data.startswith("step2_"):
        choice = data.replace("step2_", "")
        case_data = context.user_data.get("current_case")
        if not case_data:
            await query.message.reply_text("Please start a new case with /newcase.")
            return

        correct = case_data["step2_correct"]
        correct_text = case_data["step2_options"].get(correct, "")
        user_text = case_data["step2_options"].get(choice, "")

        if choice == correct:
            verdict = f"🎉 *Correct Diagnosis! ({choice}) {user_text})*"
        else:
            verdict = (
                f"❌ *You selected {choice}) {user_text}*\n"
                f"🎯 *The correct diagnosis is {correct}) {correct_text}*"
            )

        await query.message.reply_text(verdict, parse_mode="Markdown")

        slide_3 = render_card_3_diagnosis(case_data)
        context.user_data["stage"] = "QA_MODE"

        next_keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➡️ Start Next Case", callback_data="spec_Mixed")],
                [InlineKeyboardButton("🗂 Choose Specialty", callback_data="choose_spec")],
            ]
        )

        caption = (
            f"🏁 *Diagnosis: {case_data['final_diagnosis']}*\n\n"
            f"💬 *Have questions about this case?*\n"
            f"Just type your question below (e.g., _'Why was this finding present?'_, "
            f"_'How do we differentiate this from other differentials?'_, or _'What fluids/drugs do we give?'_) "
            f"and I will explain!\n\n"
            f"Or tap below when you are ready for the next case:"
        )
        await query.message.reply_photo(
            photo=slide_3,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=next_keyboard,
        )

    elif data == "choose_spec":
        await query.message.reply_text(
            "👇 *Choose a specialty for your next case:*",
            parse_mode="Markdown",
            reply_markup=build_specialty_keyboard(),
        )


# ==============================================================================
# INTERACTIVE POST-CASE Q&A HANDLER
# ==============================================================================
async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_question = update.message.text
    case_data = context.user_data.get("current_case")

    if not case_data:
        await update.message.reply_text(
            "You don't have an active case right now! Tap /start or /newcase to begin one."
        )
        return

    qa_prompt = f"""
    You are a warm, supportive clinical tutor teaching an undergraduate MBBS medical student.
    Here is the clinical case the student just worked on:
    {json.dumps(case_data, indent=2)}

    The student is asking this follow-up question about the case:
    "{user_question}"

    Provide a clear, mechanism-based, step-by-step explanation tailored to an undergraduate medical student.
‎    Keep it well-structured, friendly, and easy to read on Telegram.
‎    """
‎
‎    try:
‎        response = client.models.generate_content(
‎            model=MODEL_ID,
‎            contents=qa_prompt,
‎        )
‎        next_keyboard = InlineKeyboardMarkup(
‎            [
‎                [InlineKeyboardButton("➡️ Next Case", callback_data="spec_Mixed")],
‎                [InlineKeyboardButton("🗂 Choose Specialty", callback_data="choose_spec")],
‎            ]
‎        )
‎        await update.message.reply_text(
‎response.text,
‎            reply_markup=next_keyboard,
‎        )
‎    except Exception as e:
‎        logger.error(f"Error in Q&A: {e}")
‎        await update.message.reply_text(
‎            "⚠️ Sorry, I couldn't process that question right now. Please try asking again!"
‎        )
‎
‎
‎# ==============================================================================
‎# MAIN ENTRYPOINT
‎# ==============================================================================
‎def main():
‎    # Start the built-in health server in the background for Render
‎    threading.Thread(target=start_health_server, daemon=True).start()
‎
‎    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
‎
‎    app.add_handler(CommandHandler("start", start_command))
‎    app.add_handler(CommandHandler("newcase", new_case_command))
‎    app.add_handler(CallbackQueryHandler(handle_callback))
‎    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_question))
‎
‎    print("🩺 The Diagnosis Room Bot is running!")
‎    app.run_polling(drop_pending_updates=True)
‎if __name__ == "__main__":
‎    main()
