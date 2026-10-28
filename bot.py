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
        f"• Лично связаться с гостями, оставившими контакты, принести извинения и пригласить снова с комплиментом (чтобы перехватить негатив до публикации в 2ГИС).\n"
        f"• Усилить контроль управляющего в часы пик по направлению «{top_cat}»."
    )

# --- AI АНАЛИЗАТОР И ГЕНЕРАТОР ОТВЕТОВ ---
async def analyze_complaint_with_ai(complaint_text: str, place_name: str = "Заведение"):
    cat_default = local_classify(complaint_text)
    reply_default = build_smart_fallback_reply(place_name, cat_default, complaint_text)

    if not ai_client:
        return cat_default, "Недовольство", reply_default

    prompt = (
        f"Ты — опытный управляющий по контролю качества и заботе о гостях заведения «{place_name}».\n"
        f"Гость оставил негативный отзыв:\n«{complaint_text}»\n\n"
        "Твоя задача — перехватить негатив, чтобы гость почувствовал искреннюю заботу и не пошел писать плохую оценку в 2ГИС.\n"
        "Выполни 3 пункта:\n"
        "1) Определи категорию (выбери строго одно слово: Кухня, Сервис, Чистота, Скорость, Атмосфера, Общее).\n"
        "2) Определи эмоциональный тон (Критический, Недовольство, Разочарование).\n"
        "3) Напиши живой, персонализированный ответ гостю от лица управляющего (3–4 предложения) для отправки в WhatsApp/Instagram. "
        "Обязательно упомяни конкретную деталь из жалобы клиента (покажи, что его сообщение внимательно прочитали, а не ответили шаблоном), "
        "объясни, какие меры уже приняты, и предложи приятный бонус/комплимент или решение ситуации.\n"
        "Пиши чистым текстом без символов разметки Markdown (* или _).\n\n"
        "Ответ верни строго в таком формате:\n"
        "КАТЕГОРИЯ: <категория>\n"
        "ТОН: <тон>\n"
        "ОТВЕТ: <текст ответа гостю>"
    )

    try:
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model=GEMINI_MODEL,
            contents=prompt
        )
        content = (response.text or "").replace("**", "").replace("*", "").strip()
        
        m_cat = re.search(r"КАТЕГОРИЯ:\s*(.+)", content, re.IGNORECASE)
        m_ton = re.search(r"ТОН:\s*(.+)", content, re.IGNORECASE)
        m_rep = re.search(r"ОТВЕТ:\s*(.+)", content, re.IGNORECASE | re.DOTALL)

        category = m_cat.group(1).strip() if m_cat else cat_default
        sentiment = m_ton.group(1).strip() if m_ton else "Недовольство"
        reply_draft = m_rep.group(1).strip() if m_rep else content

        if not reply_draft:
            reply_draft = reply_default

        return category, sentiment, reply_draft
    except Exception as e:
        logging.error(f"Ошибка Gemini API при анализе жалобы: {e}")
        return cat_default, "Недовольство", reply_default

# --- ОТДЕЛЬНАЯ ГЕНЕРАЦИЯ НОВОГО ВАРИАНТА ОТВЕТА ПО КНОПКЕ ---
async def generate_custom_reply_with_ai(complaint_text: str, place_name: str, contact: str) -> str:
    cat = local_classify(complaint_text)
    if not ai_client:
        return (
            build_smart_fallback_reply(place_name, cat, complaint_text)
            + "\n\n⚠️ (Примечание: GEMINI_API_KEY не задан в Render Environment)"
        )

    prompt = (
        f"Ты — управляющий заведения «{place_name}». Сгенерируй новый, убедительный и очень вежливый вариант сообщения "
        f"для отправки клиенту в WhatsApp/Instagram (контакт: {contact}) в ответ на его жалобу:\n"
        f"«{complaint_text}»\n\n"
        "Требования:\n"
        "- Обратись вежливо, упомяни конкретную суть его жалобы (не используй общие фразы-отписки).\n"
        "- Сообщи, что руководство уже разобрало эту ситуацию со сменой.\n"
        "- Предложи конкретное решение или комплимент/скидку от заведения, чтобы вернуть лояльность гостя.\n"
        "- Пиши обычным текстом без символов * и _, 3-5 предложений, готовых к копированию и отправке."
    )
    try:
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model=GEMINI_MODEL,
            contents=prompt
        )
        text = (response.text or "").replace("**", "").replace("*", "").strip()
        return text if text else build_smart_fallback_reply(place_name, cat, complaint_text)
    except Exception as e:
        logging.error(f"Ошибка Gemini при генерации ответа по кнопке: {e}")
        return (
            build_smart_fallback_reply(place_name, cat, complaint_text)
            + f"\n\n⚠️ (Ошибка ключа Gemini API: проверьте, что GEMINI_API_KEY в Render начинается с AIza... Детали: {e})"
        )

# --- СОХРАНЕНИЕ И ОТПРАВКА УВЕДОМЛЕНИЯ ---
async def process_incoming_feedback(comment: str, contact: str, place_id: str = "legenda", place_name: str = "Legenda", custom_chat_id: int = None):
    place_id_clean = place_id.strip().lower()
    category, sentiment, reply_draft = await analyze_complaint_with_ai(comment, place_name)

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
        f"👤 <b>Контакт клиента:</b> <code>{safe_contact}</code>\n\n"
        f"🤖 <b>Рекомендуемый ответ гостю (нажмите, чтобы скопировать):</b>\n"
        f"<code>{safe_reply}</code>"
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

# --- API ПРИЕМА ДАННЫХ ---
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
            place_name = "Кульджо хого" if place_id == "kuldjo_hogo" else place_id.capitalize()

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
        [types.KeyboardButton(text="📊 Аналитика по точкам"), types.KeyboardButton(text="⚠️ Открытые жалобы")],
        [types.KeyboardButton(text="⚙️ Помощь")]
    ]
    return types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def complaint_action_keyboard(feedback_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📞 Связались с гостем", callback_data=f"status_contacted_{feedback_id}"),
            InlineKeyboardButton(text="✅ Закрыть инцидент", callback_data=f"status_resolved_{feedback_id}")
        ],
        [
            InlineKeyboardButton(text="🔄 Сгенерировать ответ AI", callback_data=f"genreply_{feedback_id}")
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
        "Система перехвата негативных отзывов, AI-отработки и бизнес-аналитики.\n"
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
    ai_status = "✅ Подключен" if GEMINI_API_KEY.startswith("AIza") else "⚠️ Требует проверки (ключ должен начинаться с AIza...)"
    await message.answer(
        "⚙️ <b>Панель управления Reviews Tap:</b>\n\n"
        "• <b>📊 Аналитика по точкам</b> — глубокий AI-разбор всех негативных отзывов, выявление слабых мест заведения и план исправления.\n"
        "• <b>⚠️ Открытые жалобы</b> — список необработанных инцидентов с кнопками смены статуса и генерации ответа.\n"
        "• <b>🔄 Сгенерировать ответ AI</b> — индивидуальный ответ гостю под его проблему.\n"
        "• <b>/my_id</b> — ваш Telegram ID.\n"
        "• <b>/debug_db</b> — последние записи в БД.\n\n"
        f"🤖 <b>Статус Gemini AI ключа:</b> {ai_status}",
        parse_mode="HTML"
    )

@dp.message(F.text == "📊 Аналитика по точкам")
@dp.message(Command("stats"))
async def cmd_stats_router(message: types.Message):
    user_id = message.from_user.id
    managed_places = [p_id.lower() for p_id, m_id in MANAGERS.items() if m_id == user_id]

    if user_id == ADMIN_ID or len(managed_places) > 1:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT DISTINCT LOWER(place_id), place_name FROM feedbacks") as cur:
                places = await cur.fetchall()

        if not places:
            await message.answer("📊 Отзывов в базе пока нет ни по одной точке.")
            return

        buttons = []
        seen_ids = set()
        for p_id, p_name in places:
            if p_id not in seen_ids:
                seen_ids.add(p_id)
                buttons.append([InlineKeyboardButton(text=f"📍 {p_name or p_id}", callback_data=f"selplace_{p_id}")])

        buttons.append([InlineKeyboardButton(text="🌐 Все заведения (общая сводка)", callback_data="selplace_all")])
        await message.answer("📍 Выберите заведение для глубокого AI-разбора:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        return

    if managed_places:
        await message.answer("🗓 Выберите период отчёта:", reply_markup=period_selection_keyboard(managed_places[0]))
    else:
        await message.answer("⛔ Нет доступа к аналитике заведений.")

@dp.callback_query(F.data.startswith("selplace_"))
async def process_place_selection(callback: CallbackQuery):
    place_id = callback.data.replace("selplace_", "").lower()
    await callback.message.edit_text("🗓 Выберите период отчёта:", reply_markup=period_selection_keyboard(place_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("rep_"))
async def process_report_period(callback: CallbackQuery):
    parts = callback.data.split("_")
    place_id = "_".join(parts[1:-1]).lower()
    period = parts[-1]

    period_labels = {"today": "за сегодня", "7days": "за последние 7 дней", "all": "за всё время"}
    period_title = period_labels.get(period, "период")

    where_clauses, params = [], []
    if place_id != "all":
        if place_id in ("legenda", "default"):
            where_clauses.append("LOWER(place_id) IN ('legenda', 'default')")
        else:
            where_clauses.append("LOWER(place_id) = ?")
            params.append(place_id)

    if period == "today":
        where_clauses.append("date(created_at) = date('now')")
    elif period == "7days":
        where_clauses.append("created_at >= datetime('now', '-7 days')")

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    async with aiosqlite.connect(DB_NAME) as db:
        if place_id == "all":
            place_title = "Все заведения сети"
        elif place_id in ("legenda", "default"):
            place_title = "Legenda"
        else:
            async with db.execute("SELECT place_name FROM feedbacks WHERE LOWER(place_id) = ? LIMIT 1", (place_id,)) as cur:
                row = await cur.fetchone()
                place_title = row[0] if row and row[0] else place_id

        async with db.execute(f"SELECT COUNT(*) FROM feedbacks {where_sql}", tuple(params)) as cur:
            total = (await cur.fetchone())[0]

        unresolved_sql = where_sql + (" AND status = 'новый'" if where_sql else "WHERE status = 'новый'")
        async with db.execute(f"SELECT COUNT(*) FROM feedbacks {unresolved_sql}", tuple(params)) as cur:
            unresolved = (await cur.fetchone())[0]

        cat_group_sql = where_sql + " GROUP BY category ORDER BY COUNT(*) DESC" if where_sql else "GROUP BY category ORDER BY COUNT(*) DESC"
        async with db.execute(f"SELECT category, COUNT(*) FROM feedbacks {cat_group_sql}", tuple(params)) as cur:
            categories = await cur.fetchall()

        recent_sql = where_sql + " ORDER BY id DESC LIMIT 50" if where_sql else "ORDER BY id DESC LIMIT 50"
        async with db.execute(f"SELECT comment, category, status, created_at FROM feedbacks {recent_sql}", tuple(params)) as cur:
            recent_reviews = await cur.fetchall()

    if total == 0:
        await callback.message.edit_text(f"📊 По точке <b>{html.escape(str(place_title))}</b> ({period_title}) обращений в базе не найдено.", parse_mode="HTML")
        await callback.answer()
        return

    cat_text = "\n".join([f"• {c[0]}: {c[1]} шт." for c in categories]) if categories else "Категории не определены."
    reviews_summary = "\n".join([
        f"{idx+1}. [Категория: {r[1]} | Статус: {r[2]}] Текст жалобы: «{r[0]}»"
        for idx, r in enumerate(recent_reviews)
    ])

    await callback.message.edit_text(
        f"⏳ <b>AI анализирует все жалобы ({total} шт.)</b> по точке <b>{html.escape(str(place_title))}</b> ({period_title})...\n"
        f"Выявляем узкие места и готовим план действий.",
        parse_mode="HTML"
    )

    report_prompt = (
        f"Ты — ведущий кризис-менеджер и бизнес-аналитик в сфере ресторанов, кафе и сервиса.\n"
        f"Проанализируй реальные негативные отзывы гостей по заведению «{place_title}» ({period_title}).\n\n"
        f"Статистика: Всего жалоб: {total}, Не отработано: {unresolved}.\n"
        f"Распределение по категориям:\n{cat_text}\n\n"
        f"Полный список поступивших жалоб от клиентов:\n{reviews_summary}\n\n"
        "На основе КОНКРЕТНЫХ текстов этих жалоб (обязательно ссылайся на реальные проблемы и детали, которые упоминали гости!) "
        "составь глубокий и практичный разбор:\n"
        "1. В ЧЕМ И ГДЕ ПРОСЕДАЕТ ЗАВЕДЕНИЕ: выдели главные боли гостей, повторяющиеся косяки и их корневые причины (с примерами из жалоб).\n"
        "2. КАК ЭТО ИСПРАВИТЬ (ПЛАН ДЕЙСТВИЙ): конкретные шаги для управляющего, шеф-повара и персонала на эту неделю.\n"
        "3. ЧТО СКАЗАТЬ НА ПЛАНЕРКЕ СМЕНЫ: 2-3 главных акцента для команды, чтобы сократить поток негатива.\n\n"
        "Пиши чётко, структурированно, обычным текстом без символов Markdown (*, **, #, _)."
    )

    ai_text = build_smart_fallback_analytics(place_title, total, unresolved, categories, recent_reviews)
    if ai_client:
        try:
            response = await asyncio.to_thread(
                ai_client.models.generate_content,
                model=GEMINI_MODEL,
                contents=report_prompt
            )
            if response.text:
                ai_text = response.text.replace("**", "").replace("*", "•").replace("###", "").replace("##", "").strip()
        except Exception as e:
            logging.error(f"Ошибка вызова Gemini при аналитике: {e}")
            ai_text += f"\n\n⚠️ (ИИ-модуль не смог подключиться: проверьте GEMINI_API_KEY в Render. Ключ должен начинаться с AIza... Ошибка: {e})"

    resolved_count = total - unresolved
    report = (
        f"📊 <b>Аналитический разбор: {html.escape(str(place_title))} ({period_title})</b>\n\n"
        f"📥 Всего обращений: <b>{total}</b>\n"
        f"✅ Отработано: <b>{resolved_count}</b> | ⚠️ Требуют внимания: <b>{unresolved}</b>\n\n"
        f"🏷 <b>Срез по категориям:</b>\n{html.escape(cat_text)}\n\n"
        f"🧠 <b>Глубокий AI-аудит и план исправления:</b>\n\n{html.escape(ai_text)}"
    )
    if len(report) > 4000:
        report = report[:4000] + "..."
    await callback.message.answer(report, parse_mode="HTML")
    await callback.answer()

@dp.message(F.text == "⚠️ Открытые жалобы")
@dp.message(Command("open"))
async def cmd_open(message: types.Message):
    user_id = message.from_user.id
    managed_places = [p_id.lower() for p_id, m_id in MANAGERS.items() if m_id == user_id]

    async with aiosqlite.connect(DB_NAME) as db:
        if user_id == ADMIN_ID:
            query = "SELECT id, place_name, comment, contact, created_at FROM feedbacks WHERE status = 'новый' ORDER BY id DESC LIMIT 10"
            params = ()
        elif managed_places:
            place_placeholders = ','.join(['?'] * len(managed_places))
            query = f"SELECT id, place_name, comment, contact, created_at FROM feedbacks WHERE status = 'новый' AND LOWER(place_id) IN ({place_placeholders}) ORDER BY id DESC LIMIT 10"
            params = tuple(managed_places)
        else:
            await message.answer("⛔ Нет доступа.")
            return

        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await message.answer("🎉 Все жалобы обработаны! Открытых инцидентов нет.")
        return

    for row in rows:
        fid, pname, comment, contact, created_at = row
        text = (
            f"⚠️ <b>Жалоба #{fid} ({html.escape(str(pname or 'Точка'))})</b>\n"
            f"⏰ Время: {html.escape(str(created_at))}\n"
            f"📞 Контакт: <code>{html.escape(str(contact or 'Не указан'))}</code>\n"
            f"💬 Суть: {html.escape(str(comment))}"
        )
        await message.answer(text, reply_markup=complaint_action_keyboard(fid), parse_mode="HTML")

# --- ОБРАБОТЧИК КНОПКИ "🔄 Сгенерировать ответ AI" ---
@dp.callback_query(F.data.startswith("genreply_"))
async def process_generate_reply(callback: CallbackQuery):
    fid = int(callback.data.split("_")[1])
    await callback.answer("⏳ Генерирую персональный ответ для гостя...")

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT place_name, comment, contact FROM feedbacks WHERE id = ?", (fid,)) as cur:
            row = await cur.fetchone()

    if not row:
        await callback.message.answer("❌ Жалоба не найдена в базе данных.")
        return

    place_name, comment, contact = row[0] or "Заведение", row[1] or "", row[2] or "Не указан"
    new_reply = await generate_custom_reply_with_ai(comment, place_name, contact)

    reply_msg = (
        f"🤖 <b>Сгенерированный ответ по жалобе #{fid} ({html.escape(str(place_name))}):</b>\n"
        f"👤 Контакт гостя: <code>{html.escape(str(contact))}</code>\n\n"
        f"📋 <i>Нажмите на текст ниже, чтобы скопировать и отправить гостю:</i>\n"
        f"<code>{html.escape(new_reply)}</code>"
    )
    await callback.message.answer(
        reply_msg,
        reply_markup=complaint_action_keyboard(fid),
        parse_mode="HTML"
    )

# --- ОБРАБОТЧИК СМЕНЫ СТАТУСА ЖАЛОБЫ ---
@dp.callback_query(F.data.startswith("status_"))
async def process_status(callback: CallbackQuery):
    action, fid = callback.data.split("_")[1], int(callback.data.split("_")[2])
    new_status = "связались" if action == "contacted" else "закрыто"

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE feedbacks SET status = ? WHERE id = ?", (new_status, fid))
        await db.commit()

    label = "📞 Статус: с гостем связались" if action == "contacted" else "✅ Статус: инцидент закрыт"
    try:
        await callback.message.edit_text(
            callback.message.text + f"\n\n<b>{label}</b>",
            reply_markup=complaint_action_keyboard(fid) if action == "contacted" else None,
            parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer(f"Жалоба #{fid}: <b>{label}</b>", parse_mode="HTML")
    await callback.answer("Статус обновлен!")

# --- ЗАПУСК ---
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()

    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_route("*", "/api/send-lead", handle_feedback_api)
    app.router.add_route("*", "/api/feedback", handle_feedback_api)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"🚀 API сервер запущен на порту {port} (/api/send-lead)!")
    print("🤖 Reviews Tap бот готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
