import os
import re
import asyncio
import logging
import json
import time
import html
import aiosqlite
from dotenv import load_dotenv

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from google import genai

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    raise ValueError("Ошибка: BOT_TOKEN не найден в .env!")

ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else 5904604582

# ================= МАРШРУТИЗАЦИЯ МЕНЕДЖЕРОВ =================
MANAGERS = {
    "legenda": 5904604582,       # Legenda
    "default": 5904604582,       # Дефолтный вход
    "voronka": 5904604582,       # Кульджо хого
    "teddys_coffee": 1046554693, # Teddy s Coffee
    "mondo_coffee": 1046554693,  # Mondo Coffee
}
# =============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
DB_NAME = "reviews_tap.db"

recent_submissions = {}

# --- ИНИЦИАЛИЗАЦИЯ БД ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS feedbacks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                place_id TEXT DEFAULT 'legenda',
                place_name TEXT,
                source TEXT,
                comment TEXT,
                contact TEXT,
                category TEXT,
                sentiment TEXT,
                status TEXT DEFAULT 'новый',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        async with db.execute("PRAGMA table_info(feedbacks)") as cur:
            columns = [row[1] for row in await cur.fetchall()]
        
        if "place_id" not in columns:
            await db.execute("ALTER TABLE feedbacks ADD COLUMN place_id TEXT DEFAULT 'legenda'")
        if "place_name" not in columns:
            await db.execute("ALTER TABLE feedbacks ADD COLUMN place_name TEXT")
            
        await db.commit()
    print("🗄 База данных успешно инициализирована!")

def local_classify(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["блюд", "еда", "холодн", "вкус", "сыр", "мясо", "пицц", "кофе", "чай", "суп", "солон"]):
        return "Кухня"
    if any(w in t for w in ["долго", "ждал", "минут", "медлен", "опоздал", "время"]):
        return "Скорость"
    if any(w in t for w in ["гряз", "стол", "волос", "туал", "посуд", "мусор"]):
        return "Чистота"
    if any(w in t for w in ["официант", "оф ", "персонал", "хам", "груб", "счет", "чек", "администратор", "не подошел"]):
        return "Сервис"
    if any(w in t for w in ["шум", "музык", "громк", "темн", "душно", "холодно в зале"]):
        return "Атмосфера"
    return "Общее"

# --- AI АНАЛИЗАТОР ---
async def analyze_complaint_with_ai(complaint_text: str):
    cat_default = local_classify(complaint_text)
    reply_default = (
        "Здравствуйте! Приносим свои извинения за сложившуюся ситуацию. "
        "Мы передали отзыв руководству и подготовим комплимент при следующем визите."
    )

    if not ai_client:
        return cat_default, "Недовольство", reply_default

    prompt = (
        "Ты — AI-ассистент контроля сервиса заведений. "
        "Проанализируй жалобу клиента: " + complaint_text + "\n\n"
        "Выдели: 1) Категорию (Кухня, Сервис, Чистота, Скорость, Атмосфера). "
        "2) Эмоциональный тон (Критический, Недовольство, Разочарование). "
        "3) Предложи вежливый ответ от управляющего с комплиментом (без символов разметки Markdown).\n"
        "Формат строго:\n"
        "КАТЕГОРИЯ: <категория>\n"
        "ТОН: <тон>\n"
        "ОТВЕТ: <текст ответа>"
    )

    try:
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt
        )
        content = response.text or ""
        category, sentiment, reply_draft = cat_default, "Недовольство", reply_default

        for line in content.split("\n"):
            if line.startswith("КАТЕГОРИЯ:"):
                category = line.replace("КАТЕГОРИЯ:", "").strip()
            elif line.startswith("ТОН:"):
                sentiment = line.replace("ТОН:", "").strip()
            elif line.startswith("ОТВЕТ:"):
                reply_draft = content.split("ОТВЕТ:")[1].strip()

        return category, sentiment, reply_draft
    except Exception as e:
        logging.warning(f"Gemini API пропущен: {e}")
        return cat_default, "Недовольство", reply_default

# --- СОХРАНЕНИЕ И ОТПРАВКА УВЕДОМЛЕНИЯ ---
async def process_incoming_feedback(comment: str, contact: str, place_id: str = "legenda", place_name: str = "Legenda", custom_chat_id: int = None):
    place_id_clean = place_id.strip().lower()
    category, sentiment, reply_draft = await analyze_complaint_with_ai(comment)

    try:
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute(
                "INSERT INTO feedbacks (place_id, place_name, source, comment, contact, category, sentiment, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'новый')",
                (place_id_clean, place_name, "NFC Стойка", comment, contact, category, sentiment)
            )
            await db.commit()
            feedback_id = cursor.lastrowid
        print(f"💾 УСПЕШНО СОХРАНЕНО В БД! ID: #{feedback_id} | Точка: {place_id_clean} ({place_name})")
    except Exception as db_err:
        print(f"❌ ОШИБКА ЗАПИСИ В БД: {db_err}")
        return

    safe_place = html.escape(str(place_name))
    safe_id = html.escape(str(place_id_clean))
    safe_cat = html.escape(str(category))
    safe_sent = html.escape(str(sentiment))
    safe_contact = html.escape(str(contact or "Не указаны"))
    safe_comment = html.escape(str(comment))
    safe_reply = html.escape(str(reply_draft))

    notification_html = (
        f"🚨 <b>Негативный отзыв / Жалоба #{feedback_id}</b>\n"
        f"📍 <b>Заведение:</b> {safe_place} (<code>{safe_id}</code>)\n\n"
        f"🏷 <b>Категория:</b> #{safe_cat} ({safe_sent})\n"
        f"📝 <b>Проблема:</b>\n{safe_comment}\n\n"
        f"👤 <b>Контакт клиента:</b> {safe_contact}\n\n"
        f"🤖 <b>Рекомендуемый ответ гостю:</b>\n<i>{safe_reply}</i>"
    )

    target_chat_id = MANAGERS.get(place_id_clean) or custom_chat_id or ADMIN_ID
    if target_chat_id:
        try:
            await bot.send_message(
                chat_id=int(target_chat_id),
                text=notification_html,
                reply_markup=complaint_action_keyboard(feedback_id),
                parse_mode="HTML"
            )
            print(f"✅ Уведомление #{feedback_id} с кнопками отправлено в Telegram (ID: {target_chat_id})")
        except Exception as e:
            logging.error(f"Ошибка отправки в Telegram: {e}")

# --- ПРОВЕРКА ЖИЗНИ СЕРВЕРА ДЛЯ RENDER ---
async def handle_health_check(request: web.Request):
    return web.Response(text="Reviews Tap API & Bot are running 24/7!")

# --- API ПРИЕМА ДАННЫХ (понимает и новый, и старый формат с сайта) ---
async def handle_feedback_api(request: web.Request):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Allow-Headers": "*"
    }

    if request.method == "OPTIONS":
        return web.Response(status=200, headers=headers)

    try:
        data = {}
        if request.method == "GET":
            data = dict(request.query)
        else:
            try:
                data = await request.json()
            except Exception:
                raw_text = await request.text()
                if raw_text:
                    data = json.loads(raw_text)

        place_id = str(data.get("place_id", "")).strip().lower()
        place_name = str(data.get("place_name", "")).strip()
        comment = str(data.get("comment", "")).strip()
        contact = str(data.get("contact", "")).strip()
        chat_id_raw = data.get("chat_id")
        custom_chat_id = int(chat_id_raw) if str(chat_id_raw).isdigit() else None

        # ФОЛБЭК: если браузер прислал старый формат { chat_id, text: "..." }
        if not comment and "text" in data:
            raw_msg = str(data["text"])
            m_place = re.search(r"Заведение:</b>\s*(.+?)\s*\((?:<code>)?(.+?)(?:</code>)?\)", raw_msg)
            if m_place:
                place_name = m_place.group(1).strip()
                place_id = m_place.group(2).strip().lower()
            m_prob = re.search(r"Проблема:</b>\s*(.+?)\s*👤", raw_msg, re.DOTALL)
            if m_prob:
                comment = m_prob.group(1).strip()
            m_cont = re.search(r"Контакт клиента:</b>\s*(.+)", raw_msg)
            if m_cont:
                contact = m_cont.group(1).strip()
            if not comment:
                comment = raw_msg

        if not place_id or place_id == "default":
            if custom_chat_id == 1046554693:
                place_id = "kuldjo_hogo"
                place_name = place_name or "Кульджо хого"
            else:
                place_id = "legenda"
                place_name = place_name or "Legenda"

        if not place_name:
            place_name = "Кульджо хого" if place_id == "kuldjo_hogo" else "Legenda"

        if not comment:
            return web.json_response({"ok": False, "status": "empty", "description": "Пустой комментарий"}, status=200, headers=headers)

        dedup_key = f"{place_id}:{comment}:{contact}"
        now = time.time()
        if dedup_key in recent_submissions and (now - recent_submissions[dedup_key]) < 10:
            return web.json_response({"ok": True, "status": "duplicate_skipped"}, status=200, headers=headers)
        
        recent_submissions[dedup_key] = now
        print(f"📥 ПРИШЕЛ ЗАПРОС С САЙТА: [{place_name} | ID: {place_id}] {comment} ({contact})")

        asyncio.create_task(process_incoming_feedback(comment, contact, place_id, place_name, custom_chat_id))
        return web.json_response({"ok": True, "status": "ok"}, status=200, headers=headers)
    except Exception as e:
        logging.error(f"Ошибка в handle_feedback_api: {e}")
        return web.json_response({"ok": False, "error": str(e), "description": str(e)}, status=500, headers=headers)

# --- КЛАВИАТУРЫ ---
def main_menu_keyboard():
    kb = [
        [types.KeyboardButton(text="📊 Аналитика"), types.KeyboardButton(text="⚠️ Открытые жалобы")],
        [types.KeyboardButton(text="⚙️ Помощь")]
    ]
    return types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def complaint_action_keyboard(feedback_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📞 Связались с гостем", callback_data=f"status_contacted_{feedback_id}"),
            InlineKeyboardButton(text="✅ Закрыть инцидент", callback_data=f"status_resolved_{feedback_id}")
        ]
    ])

def period_selection_keyboard(place_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 За сегодня", callback_data=f"rep_{place_id}_today")],
        [InlineKeyboardButton(text="🗓 За последние 7 дней", callback_data=f"rep_{place_id}_7days")],
        [InlineKeyboardButton(text="📈 За всё время", callback_data=f"rep_{place_id}_all")]
    ])

# --- ОБРАБОТЧИКИ ТЕЛЕГРАМ ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 <b>Reviews Tap Multi-Place активна!</b>\n\n"
        "Отзывы поступают с сайта, сохраняются в БД и направляются управляющим филиалов.\n"
        "• Узнать персональный ID: <code>/my_id</code>\n"
        "• Проверить базу данных: <code>/debug_db</code>",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML"
    )

@dp.message(Command("my_id"))
async def cmd_my_id(message: types.Message):
    await message.answer(f"🆔 Ваш Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")

@dp.message(Command("debug_db"))
async def debug_db(message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, place_id, status, comment FROM feedbacks ORDER BY id DESC LIMIT 10") as cur:
            rows = await cur.fetchall()
    if not rows:
        await message.answer("🗄 <b>База данных полностью пуста!</b>", parse_mode="HTML")
    else:
        text = "📋 <b>Последние записи в БД:</b>\n\n"
        for r in rows:
            text += f"ID: #{r[0]} | Точка: <code>{r[1]}</code> | Статус: <b>{r[2]}</b>\nТекст: {html.escape(str(r[3]))}\n──────────────\n"
        await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "⚙️ Помощь")
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "⚙️ <b>Панель управления Reviews Tap:</b>\n\n"
        "• <b>📊 Аналитика по точкам</b> — формирование сводок с рекомендациями AI.\n"
        "• <b>⚠️ Открытые жалобы</b> — список необработанных инцидентов.\n"
        "• <b>/my_id</b> — ваш Telegram ID.\n"
        "• <b>/debug_db</b> — проверка записей в базе данных.",
