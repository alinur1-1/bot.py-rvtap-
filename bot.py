import os
import re
import asyncio
import logging
import json
import time
import html
import asyncpg  # <-- Вместо aiosqlite используем asyncpg для Supabase/PostgreSQL
from dotenv import load_dotenv

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from google import genai

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
DATABASE_URL = os.getenv("DATABASE_URL")  # <-- Ссылка подключения из Supabase

if not BOT_TOKEN:
    raise ValueError("Ошибка: BOT_TOKEN не найден в .env!")

ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW and ADMIN_ID_RAW.isdigit() else 5904604582

MANAGERS = {
    "legenda": 5904604582,
    "default": 5904604582,
    "voronka": 5904604582,
    "teddys_coffee": 1046554693,
    "mondo_coffee": 1046554693,
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GEMINI_MODEL = "gemini-3.8-flash"

recent_submissions = {}

# --- ИНИЦИАЛИЗАЦИЯ ОБЛАЧНОЙ БД SUPABASE (POSTGRESQL) ---
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS feedbacks (
                id SERIAL PRIMARY KEY,
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
        print("🗄 Облачная база данных Supabase успешно инициализирована!")
    finally:
        await conn.close()

def local_classify(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["блюд", "еда", "холодн", "вкус", "невкус", "сыр", "мясо", "пицц", "кофе", "чай", "суп", "солон", "пересол", "порци"]):
        return "Кухня"
    if any(w in t for w in ["долго", "ждал", "минут", "медлен", "опоздал", "время", "час"]):
        return "Скорость"
    if any(w in t for w in ["гряз", "стол", "волос", "туал", "посуд", "мусор", "салфет", "пыл", "таракан"]):
        return "Чистота"
    if any(w in t for w in ["официант", "оф ", "персонал", "хам", "груб", "счет", "чек", "администратор", "не подошел", "игнор", "афк", "обслуживан"]):
        return "Сервис"
    if any(w in t for w in ["шум", "музык", "громк", "темн", "душно", "холодно в зале", "кондиционер", "диван"]):
        return "Атмосфера"
    return "Общее"

# --- МОМЕНТАЛЬНОЕ СОХРАНЕНИЕ В SUPABASE ---
async def process_incoming_feedback(comment: str, contact: str, place_id: str = "legenda", place_name: str = "Legenda", custom_chat_id: int = None):
    place_id_clean = (place_id or "legenda").strip().lower()
    category = local_classify(comment)
    sentiment = "Недовольство"

    feedback_id = 0
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO feedbacks (place_id, place_name, source, comment, contact, category, sentiment, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'новый')
            RETURNING id
            """,
            place_id_clean, place_name, "NFC Стойка", comment, contact, category, sentiment
        )
        feedback_id = row['id']
        print(f"💾 СОХРАНЕНО В SUPABASE! ID: #{feedback_id}")
    except Exception as db_err:
        print(f"❌ ОШИБКА ЗАПИСИ В SUPABASE: {db_err}")
    finally:
        await conn.close()

    display_comment = comment if len(comment) <= 1800 else comment[:1800] + "... [текст сокращен]"

    safe_place = html.escape(str(place_name))
    safe_id = html.escape(str(place_id_clean))
    safe_cat = html.escape(str(category))
    safe_contact = html.escape(str(contact or "Не указаны"))
    safe_comment = html.escape(str(display_comment))

    notification_html = (
        f"🚨 <b>Новая жалоба #{feedback_id}</b>\n"
        f"📍 <b>Заведение:</b> {safe_place} (<code>{safe_id}</code>)\n\n"
        f"🏷 <b>Категория:</b> #{safe_cat}\n"
        f"📝 <b>Суть проблемы:</b>\n{safe_comment}\n\n"
        f"👤 <b>Контакт:</b> <code>{safe_contact}</code>"
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
        except Exception as e:
            logging.error(f"❌ Ошибка отправки в Telegram: {e}")
