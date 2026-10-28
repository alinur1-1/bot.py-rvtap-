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
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()

if not BOT_TOKEN:
    raise ValueError("Ошибка: BOT_TOKEN не найден в .env!")

ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW and ADMIN_ID_RAW.isdigit() else 5904604582

# ================= МАРШРУТИЗАЦИЯ МЕНЕДЖЕРОВ =================
MANAGERS = {
    "legenda": 5904604582,       # Legenda
    "default": 5904604582,       # Дефолтный вход
    "voronka": 5904604582,       # Voronka / Кульджо хого
    "teddys_coffee": 1046554693, # Teddy's Coffee
    "mondo_coffee": 1046554693,  # Mondo Coffee
}
# =============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GEMINI_MODEL = "gemini-2.5-flash"
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

# --- УМНАЯ ЛОКАЛЬНАЯ КЛАССИФИКАЦИЯ И ШАБЛОН (если AI недоступен) ---
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

def build_smart_fallback_reply(place_name: str, category: str, comment: str) -> str:
    short_issue = comment.strip()
    if len(short_issue) > 90:
        short_issue = short_issue[:90].rstrip() + "..."
    
    cat_apology = {
        "Кухня": "Нам очень жаль, что качество блюд и кухни не оправдало ваших ожиданий — мы уже передали ваше замечание шеф-повару на разбор.",
        "Сервис": "Приносим искренние извинения за работу персонала — такое отношение к гостям недопустимо, управляющий уже проводит разбор смены.",
        "Скорость": "Простите за долгое ожидание заказа — мы разбираемся, почему возникла задержка на смене, чтобы исключить такие накладки.",
        "Чистота": "Приносим извинения за упущение по чистоте — мы уже усилили контроль за состоянием зала и рабочих зон.",
        "Атмосфера": "Спасибо, что обратили внимание на комфорт в зале — мы обязательно скорректируем эти детали.",
        "Общее": f"Нам очень жаль, что у вас сложилось такое впечатление («{short_issue}»). Мы уже передали ваше обращение руководству для детальной проверки."
    }
    apology_line = cat_apology.get(category, cat_apology["Общее"])
    return (
        f"Здравствуйте! Спасибо за ваш честный сигнал по заведению «{place_name}». "
        f"{apology_line} "
        f"Мы очень дорожим вашей обратной связью и будем рады лично исправить впечатление и подготовить для вас комплимент от заведения при следующем визите!"
    )

def build_smart_fallback_analytics(place_title: str, total: int, unresolved: int, categories: list, reviews: list) -> str:
    top_cat = categories[0][0] if categories else "Общее"
    sample_issues = "\n".join([f"• «{r[0][:100]}...» ({r[1]})" if len(r[0]) > 100 else f"• «{r[0]}» ({r[1]})" for r in reviews[:5]])
    return (
        f"1. Где проседает «{place_title}»:\n"
        f"Основной удар приходится на категорию «{top_cat}». Из {total} обращений не закрыто {unresolved}.\n"
        f"Ключевые сигналы от гостей:\n{sample_issues}\n\n"
        f"2. Как исправить ситуацию на этой неделе:\n"
        f"• Разобрать каждый открытый инцидент с персоналом смены по факту конкретных жалоб выше.\n"
        f"• Ли
