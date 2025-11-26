import asyncio
import os
from datetime import datetime, timedelta, date
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message
from aiogram.filters import Command
import aiosqlite
from dotenv import load_dotenv
from fastapi import FastAPI, Request
import uvicorn
from aiogram.types import Update

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "kakunchik.db"

router = Router()

# ---------- DB helpers ----------

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            created_at TEXT NOT NULL
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS poops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            day TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_poops_chat_day ON poops(chat_id, day)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_poops_chat_ts ON poops(chat_id, ts)")
        await db.commit()


async def ensure_user(db, user_id: int, chat_id: int, username: str | None, first_name: str | None):
    cur = await db.execute("SELECT 1 FROM users WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
    row = await cur.fetchone()
    if not row:
        await db.execute("""
            INSERT INTO users (user_id, chat_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, chat_id, username, first_name, datetime.utcnow().isoformat()))
        await db.commit()


async def add_poop(user_id: int, chat_id: int):
    ts = datetime.utcnow()
    day = ts.date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id, chat_id, None, None)
        await db.execute("""
            INSERT INTO poops (user_id, chat_id, ts, day)
            VALUES (?, ?, ?, ?)
        """, (user_id, chat_id, ts.isoformat(), day))
        await db.commit()


async def count_by_users(chat_id: int, start_day: date, end_day: date):
    """
    Возвращает dict user_id -> count по событиям day BETWEEN start_day AND end_day.
    end_day включительно.
    """
    sd = start_day.isoformat()
    ed = end_day.isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT user_id, COUNT(*) 
            FROM poops
            WHERE chat_id = ?
              AND day BETWEEN ? AND ?
            GROUP BY user_id
            ORDER BY COUNT(*) DESC
        """, (chat_id, sd, ed))
        rows = await cur.fetchall()

        # достанем имена
        if not rows:
            return []

        user_ids = [r[0] for r in rows]
        q_marks = ",".join("?" for _ in user_ids)
        cur2 = await db.execute(f"""
            SELECT user_id, COALESCE(username, first_name, CAST(user_id AS TEXT)) 
            FROM users
            WHERE chat_id = ? AND user_id IN ({q_marks})
        """, (chat_id, *user_ids))
        name_rows = await cur2.fetchall()
        id_to_name = {uid: name for uid, name in name_rows}

    result = []
    for uid, cnt in rows:
        result.append((uid, id_to_name.get(uid, str(uid)), cnt))
    return result


# ---------- Commands ----------

@router.message(Command("start"))
async def cmd_start(msg: Message):
    await msg.answer(
        "Я Какунчик 💩\n\n"
        "Команды:\n"
        "/poop — покакать\n"
        "/today — кто сколько сегодня\n"
        "/week — статистика за 7 дней\n"
        "/month — статистика за 30 дней\n"
        "/leaderboard — лидерборд за неделю\n"
    )


@router.message(Command("poop"))
async def cmd_poop(msg: Message):
    user = msg.from_user
    chat_id = msg.chat.id

    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user.id, chat_id, user.username, user.first_name)
        ts = datetime.utcnow()
        await db.execute("""
            INSERT INTO poops (user_id, chat_id, ts, day)
            VALUES (?, ?, ?, ?)
        """, (user.id, chat_id, ts.isoformat(), ts.date().isoformat()))
        await db.commit()

        # посчитаем сколько у него сегодня
        cur = await db.execute("""
            SELECT COUNT(*) FROM poops
            WHERE chat_id = ? AND user_id = ? AND day = ?
        """, (chat_id, user.id, ts.date().isoformat()))
        cnt_today = (await cur.fetchone())[0]

    await msg.answer(f"{user.first_name or user.username or 'Ты'} покакал(а)! 💩\nСегодня уже: {cnt_today}")


@router.message(Command("today"))
async def cmd_today(msg: Message):
    chat_id = msg.chat.id
    d = datetime.utcnow().date()
    stats = await count_by_users(chat_id, d, d)

    if not stats:
        await msg.answer("Сегодня пока никто не какал. Тишина в эфире 🤫")
        return

    lines = [f"Статистика за сегодня ({d.isoformat()}):"]
    for i, (_, name, cnt) in enumerate(stats, start=1):
        lines.append(f"{i}. {name}: {cnt}")
    await msg.answer("\n".join(lines))


@router.message(Command("week"))
async def cmd_week(msg: Message):
    chat_id = msg.chat.id
    end_d = datetime.utcnow().date()
    start_d = end_d - timedelta(days=6)  # 7 дней включая сегодня
    stats = await count_by_users(chat_id, start_d, end_d)

    if not stats:
        await msg.answer("За неделю никто не какал. Вы там живы? 😶")
        return

    lines = [f"Статистика за неделю ({start_d.isoformat()} — {end_d.isoformat()}):"]
    for i, (_, name, cnt) in enumerate(stats, start=1):
        lines.append(f"{i}. {name}: {cnt}")
    await msg.answer("\n".join(lines))


@router.message(Command("month"))
async def cmd_month(msg: Message):
    chat_id = msg.chat.id
    end_d = datetime.utcnow().date()
    start_d = end_d - timedelta(days=29)  # 30 дней включая сегодня
    stats = await count_by_users(chat_id, start_d, end_d)

    if not stats:
        await msg.answer("За месяц пусто. Питаетесь святым духом? ✨")
        return

    lines = [f"Статистика за 30 дней ({start_d.isoformat()} — {end_d.isoformat()}):"]
    for i, (_, name, cnt) in enumerate(stats, start=1):
        lines.append(f"{i}. {name}: {cnt}")
    await msg.answer("\n".join(lines))


@router.message(Command("leaderboard"))
async def cmd_leaderboard(msg: Message):
    chat_id = msg.chat.id
    end_d = datetime.utcnow().date()
    start_d = end_d - timedelta(days=6)

    stats = await count_by_users(chat_id, start_d, end_d)

    if not stats:
        await msg.answer("Лидерборд пуст. Никто не отметился 💀")
        return

    medal = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"Лидерборд недели ({start_d.isoformat()} — {end_d.isoformat()}):"]
    for i, (_, name, cnt) in enumerate(stats, start=1):
        m = medal.get(i, "💩")
        lines.append(f"{m} {i}. {name}: {cnt}")
    await msg.answer("\n".join(lines))


# ---------- Main ----------

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

app = FastAPI()
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
dp.include_router(router)

@app.on_event("startup")
async def on_startup():
    await init_db()
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL + WEBHOOK_PATH)

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

