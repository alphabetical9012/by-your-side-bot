import os
import asyncio
import threading
import logging
import random
from datetime import datetime, timedelta
import pytz
import psycopg2
import json
from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)
import google.generativeai as genai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", "8080"))
RENDER_URL = os.getenv("RENDER_URL", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

genai.configure(api_key=GEMINI_API_KEY)

with open("system_prompt.txt", "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

# --- Фразы чек-инов ---
CHECKIN_PHRASES = [
    "Мягко напоминаю о нашем чек-ине. Когда будешь готов(а) — я здесь.",
    "Привет! Остановись на минутку. Последний час жизни — каким он был для тебя?",
    "Момент присутствия. Давай вместе вспомним прошедший час?",
    "Пауза всё ещё открыта. Хочешь остановиться и заметить свой час?",
    "Последнее напоминание на этот час. Если сейчас не получается — ничего, скоро новый чек-ин.",
    "Три минуты для себя. Остановись и почувствуй — что было этот час?",
    "Остановка. Вдох. Что происходило с тобой это время?",
    "Просто напоминаю — твои ответы ждут. Три минуты для себя?",
    "Ещё одна попытка достучаться. Но если сейчас не время — понимаю.",
]

MORNING_MESSAGE = "☀️ Ты уже несколько дней в практике осознанности. Что-то меняется? Может быть, пока незаметно. Но оно меняется.\n\nСегодня — ещё один день, чтобы быть рядом с собой. Я буду напоминать тебе об этом.\n\nПусть этот день будет прожит."

# --- Flask ---
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot is running!", 200

# --- Автопинг ---
def keep_alive():
    import time
    import requests
    while True:
        try:
            if RENDER_URL:
                requests.get(RENDER_URL, timeout=10)
        except Exception as e:
            logger.warning(f"Пинг не удался: {e}")
        time.sleep(300)

# --- База данных ---
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS marathon_users (
                    user_id BIGINT PRIMARY KEY,
                    timezone TEXT NOT NULL,
                    sleep_time TEXT NOT NULL,
                    interval_hours REAL NOT NULL,
                    day_number INTEGER DEFAULT 1,
                    start_date DATE,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS marathon_checkins (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    day_number INTEGER NOT NULL,
                    messages JSONB DEFAULT '[]',
                    checkin_count INTEGER DEFAULT 0,
                    answered_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS marathon_messages (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    day_number INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS marathon_state (
                    user_id BIGINT PRIMARY KEY,
                    state TEXT NOT NULL,
                    data JSONB DEFAULT '{}'
                )
            """)
        conn.commit()
    logger.info("База данных инициализирована")

def get_user(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM marathon_users WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

def save_user(user_id, timezone, sleep_time, interval_hours):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO marathon_users (user_id, timezone, sleep_time, interval_hours, day_number, start_date, is_active)
                VALUES (%s, %s, %s, %s, 1, CURRENT_DATE, TRUE)
                ON CONFLICT (user_id) DO UPDATE SET
                    timezone = EXCLUDED.timezone,
                    sleep_time = EXCLUDED.sleep_time,
                    interval_hours = EXCLUDED.interval_hours,
                    day_number = 1,
                    start_date = CURRENT_DATE,
                    is_active = TRUE
            """, (user_id, timezone, sleep_time, interval_hours))
        conn.commit()

def get_state(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state, data FROM marathon_state WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                return None, {}
            return row[0], row[1]

def set_state(user_id, state, data=None):
    if data is None:
        data = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO marathon_state (user_id, state, data)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state, data = EXCLUDED.data
            """, (user_id, state, json.dumps(data)))
        conn.commit()

def save_message(user_id, role, content, day_number):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO marathon_messages (user_id, role, content, day_number)
                VALUES (%s, %s, %s, %s)
            """, (user_id, role, content, day_number))
        conn.commit()

def get_today_messages(user_id, day_number):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT role, content FROM marathon_messages
                WHERE user_id = %s AND day_number = %s
                ORDER BY created_at ASC
            """, (user_id, day_number))
            rows = cur.fetchall()
    return [{"role": r[0], "parts": [r[1]]} for r in rows]

def get_all_messages(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT day_number, role, content FROM marathon_messages
                WHERE user_id = %s
                ORDER BY created_at ASC
            """, (user_id,))
            return cur.fetchall()

def get_checkin_stats(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(checkin_count), 0), COALESCE(SUM(answered_count), 0)
                FROM marathon_checkins WHERE user_id = %s
            """, (user_id,))
            return cur.fetchone()

def update_checkin_stats(user_id, day_number, sent=False, answered=False):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO marathon_checkins (user_id, day_number, checkin_count, answered_count)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (user_id, day_number, 0, 0))
            if sent:
                cur.execute("""
                    UPDATE marathon_checkins SET checkin_count = checkin_count + 1
                    WHERE user_id = %s AND day_number = %s
                """, (user_id, day_number))
            if answered:
                cur.execute("""
                    UPDATE marathon_checkins SET answered_count = answered_count + 1
                    WHERE user_id = %s AND day_number = %s
                """, (user_id, day_number))
        conn.commit()

def reset_user(user_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM marathon_users WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM marathon_messages WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM marathon_checkins WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM marathon_state WHERE user_id = %s", (user_id,))
        conn.commit()

# --- Gemini ---
def ask_gemini(user_id, user_message, day_number, mode="checkin"):
    history = get_today_messages(user_id, day_number)
    
    if mode == "summary":
        all_msgs = get_all_messages(user_id)
        total, answered = get_checkin_stats(user_id)
        context = f"РЕЖИМ 2: ИТОГ ДНЯ {day_number}\n\nВсего чек-инов за день: {total}\nОтветов: {answered}\n\nОтветы пользователя за день:\n"
        for _, role, content in all_msgs:
            if role == "user":
                context += f"- {content}\n"
        prompt = context
    elif mode == "marathon_end":
        all_msgs = get_all_messages(user_id)
        total, answered = get_checkin_stats(user_id)
        context = f"РЕЖИМ 3: ИТОГ МАРАФОНА\n\nВсего чек-инов за 7 дней: {total}\nПользователь откликнулся на: {answered}\n\nВсе ответы пользователя:\n"
        for day, role, content in all_msgs:
            if role == "user":
                context += f"День {day}: {content}\n"
        prompt = context
    else:
        prompt = f"РЕЖИМ 1: УГЛУБЛЯЮЩИЙ ВОПРОС\n\nПользователь написал: {user_message}"

    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        system_instruction=SYSTEM_PROMPT
    )
    chat = model.start_chat(history=history if mode == "checkin" else [])
    response = chat.send_message(prompt)
    return response.text

# --- Планировщик ---
bot_app = None

async def send_checkin(user_id, day_number):
    if bot_app is None:
        return
    phrase = random.choice(CHECKIN_PHRASES)
    try:
        await bot_app.bot.send_message(chat_id=user_id, text=phrase)
        update_checkin_stats(user_id, day_number, sent=True)
        set_state(user_id, "waiting_checkin", {"day": day_number, "reminder_sent": False})
    except Exception as e:
        logger.error(f"Ошибка отправки чек-ина {user_id}: {e}")

async def send_reminder(user_id, day_number):
    if bot_app is None:
        return
    try:
        await bot_app.bot.send_message(
            chat_id=user_id,
            text="Пауза всё ещё открыта. Хочешь остановиться и заметить свой час?"
        )
    except Exception as e:
        logger.error(f"Ошибка напоминания {user_id}: {e}")

async def send_daily_summary(user_id, day_number):
    if bot_app is None:
        return
    try:
        summary = ask_gemini(user_id, "", day_number, mode="summary")
        await bot_app.bot.send_message(chat_id=user_id, text=summary)
    except Exception as e:
        logger.error(f"Ошибка итога дня {user_id}: {e}")

async def send_morning(user_id, day_number):
    if bot_app is None:
        return
    try:
        await bot_app.bot.send_message(
            chat_id=user_id,
            text=f"☀️ Утро дня {day_number}.\n\n{MORNING_MESSAGE}"
        )
    except Exception as e:
        logger.error(f"Ошибка утреннего сообщения {user_id}: {e}")

async def send_marathon_end(user_id):
    if bot_app is None:
        return
    try:
        ending = ask_gemini(user_id, "", 7, mode="marathon_end")
        await bot_app.bot.send_message(chat_id=user_id, text=ending)
        await asyncio.sleep(2)
        await bot_app.bot.send_message(
            chat_id=user_id,
            text="Хочешь пройти ещё одну неделю? Напиши /start чтобы начать заново."
        )
    except Exception as e:
        logger.error(f"Ошибка финала марафона {user_id}: {e}")

def schedule_user(user_id, tz_str, sleep_time_str, interval_hours, day_number):
    tz = pytz.timezone(tz_str if "/" in tz_str else f"Etc/GMT{-int(float(tz_str))}")
    now = datetime.now(tz)
    sleep_h, sleep_m = map(int, sleep_time_str.split(":"))
    sleep_dt = now.replace(hour=sleep_h, minute=sleep_m, second=0, microsecond=0)
    if sleep_dt <= now:
        sleep_dt += timedelta(days=1)
    summary_dt = sleep_dt - timedelta(minutes=30)

    # Первый чек-ин — через интервал от текущего момента
    first_checkin = now + timedelta(hours=interval_hours)
    asyncio.get_event_loop().create_task(schedule_checkins_loop(user_id, first_checkin, sleep_dt, summary_dt, interval_hours, day_number, tz_str))

async def schedule_checkins_loop(user_id, first_checkin, sleep_dt, summary_dt, interval_hours, day_number, tz_str):
    tz = pytz.timezone(tz_str if "/" in tz_str else f"Etc/GMT{-int(float(tz_str))}")
    current = first_checkin
    while current < sleep_dt - timedelta(minutes=30):
        wait = (current - datetime.now(tz)).total_seconds()
        if wait > 0:
            await asyncio.sleep(wait)
        user = get_user(user_id)
        if not user or not user["is_active"]:
            return
        await send_checkin(user_id, day_number)
        # Напоминание через 15 минут
        await asyncio.sleep(15 * 60)
        state, data = get_state(user_id)
        if state == "waiting_checkin":
            await send_reminder(user_id, day_number)
        current += timedelta(hours=interval_hours)

    # Итог дня
    wait = (summary_dt - datetime.now(tz)).total_seconds()
    if wait > 0:
        await asyncio.sleep(wait)
    await send_daily_summary(user_id, day_number)

    # После итога — тишина до 7:00 следующего дня
    user = get_user(user_id)
    if not user:
        return

    if day_number >= 7:
        await send_marathon_end(user_id)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE marathon_users SET is_active = FALSE WHERE user_id = %s", (user_id,))
            conn.commit()
    else:
        new_day = day_number + 1
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE marathon_users SET day_number = %s WHERE user_id = %s", (new_day, user_id))
            conn.commit()

        # Ждём 7:00 по времени пользователя
        now = datetime.now(tz)
        morning_dt = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if morning_dt <= now:
            morning_dt += timedelta(days=1)
        wait = (morning_dt - datetime.now(tz)).total_seconds()
        if wait > 0:
            await asyncio.sleep(wait)

        await send_morning(user_id, new_day)

        # Первый чек-ин через интервал после утреннего сообщения
        first_checkin_new = datetime.now(tz) + timedelta(hours=interval_hours)
        tz_str_user = user["timezone"]
        sleep_time_str = user["sleep_time"]
        sleep_h, sleep_m = map(int, sleep_time_str.split(":"))
        now2 = datetime.now(tz)
        sleep_dt_new = now2.replace(hour=sleep_h, minute=sleep_m, second=0, microsecond=0)
        if sleep_dt_new <= now2:
            sleep_dt_new += timedelta(days=1)
        summary_dt_new = sleep_dt_new - timedelta(minutes=30)
        asyncio.get_event_loop().create_task(schedule_checkins_loop(user_id, first_checkin_new, sleep_dt_new, summary_dt_new, interval_hours, new_day, tz_str_user))


# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reset_user(user_id)
    set_state(user_id, "setup_timezone")
    await update.message.reply_text(
        "Привет! Я рад, что ты здесь.\n\n"
        "Этот марафон — про то, чтобы замечать свою жизнь. Не анализировать, не оценивать — просто маленькие остановки внутри большого дня.\n\n"
        "Каждый час я буду спрашивать тебя о прошедшем времени, и ты будешь останавливаться на три минуты, чтобы побыть с собой.\n\n"
        "Здесь не существует правильных ответов. Есть только твои настоящие мысли, чувства, переживания.\n\n"
        "В каком ты часовом поясе?\n\n"
        "⚠️ Это смещение относительно UTC, а не «твоего нуля»!\n"
        "Москва, Минск, Турция = +3 (не +0!)\n\n"
        "Примеры:\n"
        "• +3 — Москва, Минск, Стамбул\n"
        "• +5 — Екатеринбург\n"
        "• +7 — Новосибирск, Бангкок\n"
        "• +10 — Владивосток\n"
        "• -5 — Нью-Йорк\n\n"
        "Напиши свой часовой пояс (например: +3)"
    )

async def restart_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только для администратора.")
        return
    if not context.args:
        await update.message.reply_text("Напиши: /restart_user 123456789")
        return
    target_id = int(context.args[0])
    reset_user(target_id)
    await update.message.reply_text(f"✅ Пользователь {target_id} сброшен. Он может начать заново с /start")
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="Твой марафон был перезапущен. Напиши /start чтобы начать заново."
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить пользователя: {e}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    state, data = get_state(user_id)

    # --- Настройка: часовой пояс ---
    if state == "setup_timezone":
        try:
            offset = float(text.replace("+", ""))
            set_state(user_id, "setup_sleep", {"timezone": str(int(offset)) if offset == int(offset) else str(offset)})
            await update.message.reply_text(
                "Принял.\n\nВо сколько ты обычно ложишься спать?\n"
                "Напиши время в формате ЧЧ:ММ (например: 23:00)"
            )
        except:
            await update.message.reply_text("Напиши цифру со знаком, например: +3 или -5")
        return

    # --- Настройка: время сна ---
    if state == "setup_sleep":
        try:
            parts = text.split(":")
            if len(parts) != 2:
                raise ValueError
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
            tz = data.get("timezone", "+3")
            set_state(user_id, "setup_interval", {"timezone": tz, "sleep_time": text})
            await update.message.reply_text(
                "Отлично!\n\nКак часто присылать чек-ины?\n\n"
                "1 час* — рекомендую. Это всего 15-20 минут осознанности в день.\n"
                "1.5 часа — более мягкий ритм\n"
                "2 часа — минимальный режим\n\n"
                "Напиши 1, 1.5 или 2"
            )
        except:
            await update.message.reply_text("Напиши время в формате ЧЧ:ММ, например: 23:00")
        return

    # --- Настройка: интервал ---
    if state == "setup_interval":
        intervals = {"1": 1.0, "1.5": 1.5, "2": 2.0}
        if text not in intervals:
            await update.message.reply_text("Напиши 1, 1.5 или 2")
            return
        interval = intervals[text]
        tz = data.get("timezone", "+3")
        sleep_time = data.get("sleep_time", "23:00")
        save_user(user_id, tz, sleep_time, interval)
        set_state(user_id, "active")

        # Считаем время первого чек-ина
        try:
            tz_obj = pytz.timezone(f"Etc/GMT{-int(float(tz))}")
            now = datetime.now(tz_obj)
            first = now + timedelta(hours=interval)
            first_str = first.strftime("%H:%M")
        except:
            first_str = "скоро"

        await update.message.reply_text(
            f"Готово! Теперь я знаю твой ритм.\n\n"
            f"Вот как это работает:\n"
            f"• Я буду писать тебе с выбранным интервалом\n"
            f"• У тебя будет пара минут, чтобы остановиться и ответить\n"
            f"• Если не успеваешь — напомню через 15 минут\n"
            f"• За 30 минут до сна — персональный итог дня\n\n"
            f"Марафон начинается. 7 дней осознанности.\n"
            f"Первый чек-ин придёт в {first_str}. ✨"
        )
        asyncio.get_event_loop().create_task(schedule_user_async(user_id, tz, sleep_time, interval, 1))
        return

    # --- Активный чек-ин ---
    if state in ("waiting_checkin", "active"):
        user = get_user(user_id)
        if not user:
            await update.message.reply_text("Напиши /start чтобы начать марафон.")
            return
        day = user["day_number"]
        update_checkin_stats(user_id, day, answered=True)
        save_message(user_id, "user", text, day)
        await update.message.chat.send_action("typing")
        try:
            reply = ask_gemini(user_id, text, day, mode="checkin")
            save_message(user_id, "model", reply, day)
            set_state(user_id, "active")
            await update.message.reply_text(reply)
        except Exception as e:
            logger.error(f"Ошибка Gemini: {e}")
            await update.message.reply_text("Что-то пошло не так. Попробуй ещё раз.")
        return

    # --- Нет активного марафона ---
    await update.message.reply_text("Напиши /start чтобы начать марафон.")

async def schedule_user_async(user_id, tz, sleep_time, interval, day):
    schedule_user(user_id, tz, sleep_time, interval, day)

# --- Запуск ---
async def run_bot():
    global bot_app
    bot_app = ApplicationBuilder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("restart_user", restart_user_cmd))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Бот запущен")
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    await asyncio.Event().wait()

def main():
    if not TOKEN or not GEMINI_API_KEY:
        logger.error("Не заданы BOT_TOKEN или GEMINI_API_KEY")
        return
    init_db()
    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False),
        daemon=True
    ).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
