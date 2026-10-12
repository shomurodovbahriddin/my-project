import os
import asyncio
import sqlite3
from datetime import time as dtime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from deep_translator import GoogleTranslator
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from content import TOPICS

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

DB = "bot.db"
TZ = ZoneInfo("Asia/Tashkent")

DIRECTIONS = {
    "uz_en": ("uz", "en", "🇺🇿 O'zbekcha → 🇬🇧 Inglizcha"),
    "en_uz": ("en", "uz", "🇬🇧 Inglizcha → 🇺🇿 O'zbekcha"),
}

HELP_TEXT = (
    "📖 Bot imkoniyatlari:\n\n"
    "📚 Darslar — beginner mavzular\n"
    "🧠 Quiz — mavzu bo'yicha test\n"
    "⏰ Eslatma — har kuni belgilangan vaqtda eslatadi\n"
    "🌍 Tarjimon — oddiy matn yozsangiz, tarjima qiladi\n\n"
    "Buyruqlar:\n"
    "/start — bosh menyu\n"
    "/remind 20:30 — eslatma vaqtini belgilash\n"
    "/remind_off — eslatmani o'chirish\n"
    "/name Ism Familya — ismingizni o'zgartirish\n"
    "/help — yordam\n"
    "/about — bot haqida"
)


# ---------- Ma'lumotlar bazasi ----------
def db_exec(query, params=()):
    con = sqlite3.connect(DB)
    try:
        cur = con.execute(query, params)
        rows = cur.fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def init_db():
    db_exec(
        "CREATE TABLE IF NOT EXISTS reminders ("
        "chat_id INTEGER PRIMARY KEY, hour INTEGER, minute INTEGER, "
        "next_topic INTEGER DEFAULT 0)"
    )
    db_exec(
        "CREATE TABLE IF NOT EXISTS users ("
        "chat_id INTEGER PRIMARY KEY, name TEXT)"
    )


def get_name(chat_id: int) -> str:
    rows = db_exec("SELECT name FROM users WHERE chat_id=?", (chat_id,))
    return rows[0][0] if rows else "do'stim"


def set_name(chat_id: int, name: str):
    db_exec(
        "INSERT INTO users (chat_id, name) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name",
        (chat_id, name),
    )


def remember_user(update: Update):
    """Ism hali saqlanmagan bo'lsa, Telegram profilidan oladi."""
    chat_id = update.effective_chat.id
    if db_exec("SELECT 1 FROM users WHERE chat_id=?", (chat_id,)):
        return
    u = update.effective_user
    full = " ".join(p for p in [u.first_name, u.last_name] if p)
    set_name(chat_id, full or "do'stim")


# ---------- Klaviaturalar ----------
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📚 Darslar", callback_data="lessons"),
                InlineKeyboardButton("🧠 Quiz", callback_data="quizmenu"),
            ],
            [
                InlineKeyboardButton("⏰ Eslatma", callback_data="remmenu"),
                InlineKeyboardButton("🌍 Tarjimon", callback_data="trans"),
            ],
        ]
    )


def lessons_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(t["title"], callback_data=f"lesson:{i}")]
        for i, t in enumerate(TOPICS)
    ]
    return InlineKeyboardMarkup(rows)


def quizmenu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(t["title"], callback_data=f"quiz:{i}")]
        for i, t in enumerate(TOPICS)
    ]
    return InlineKeyboardMarkup(rows)


def lesson_keyboard(i: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧠 Shu mavzu bo'yicha quiz", callback_data=f"quiz:{i}")],
            [InlineKeyboardButton("📚 Mavzular ro'yxati", callback_data="lessons")],
        ]
    )


def trans_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = []
    for key, (_, _, label) in DIRECTIONS.items():
        mark = "✅ " if key == current else ""
        rows.append([InlineKeyboardButton(mark + label, callback_data=f"dir:{key}")])
    return InlineKeyboardMarkup(rows)


def swap_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Yo'nalishni almashtirish", callback_data="swap")]]
    )


def remind_keyboard() -> InlineKeyboardMarkup:
    times = ["07:00", "09:00", "12:00", "18:00", "20:00", "21:00"]
    rows = []
    for i in range(0, len(times), 3):
        rows.append(
            [InlineKeyboardButton(t, callback_data=f"rem:{t}") for t in times[i : i + 3]]
        )
    rows.append([InlineKeyboardButton("❌ Eslatmani o'chirish", callback_data="rem:off")])
    return InlineKeyboardMarkup(rows)


# ---------- Eslatma ----------
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    rows = db_exec("SELECT next_topic FROM reminders WHERE chat_id=?", (chat_id,))
    if not rows:
        return
    idx = rows[0][0] % len(TOPICS)
    db_exec(
        "UPDATE reminders SET next_topic=? WHERE chat_id=?",
        ((idx + 1) % len(TOPICS), chat_id),
    )
    await context.bot.send_message(
        chat_id,
        f"⏰ Bugun ingliz tilini qilishimiz kerak, {get_name(chat_id)}!\n\n"
        f"Bugungi mavzu: {TOPICS[idx]['title']}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📖 Darsni ochish", callback_data=f"lesson:{idx}")]]
        ),
    )


def schedule_reminder(job_queue, chat_id: int, hour: int, minute: int):
    for job in job_queue.get_jobs_by_name(f"rem_{chat_id}"):
        job.schedule_removal()
    job_queue.run_daily(
        reminder_job,
        time=dtime(hour, minute, tzinfo=TZ),
        chat_id=chat_id,
        name=f"rem_{chat_id}",
    )


def save_reminder(job_queue, chat_id: int, hour: int, minute: int):
    db_exec(
        "INSERT INTO reminders (chat_id, hour, minute) VALUES (?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET hour=excluded.hour, minute=excluded.minute",
        (chat_id, hour, minute),
    )
    schedule_reminder(job_queue, chat_id, hour, minute)


def remove_reminder(job_queue, chat_id: int):
    db_exec("DELETE FROM reminders WHERE chat_id=?", (chat_id,))
    for job in job_queue.get_jobs_by_name(f"rem_{chat_id}"):
        job.schedule_removal()


async def post_init(app):
    init_db()
    for chat_id, hour, minute in db_exec("SELECT chat_id, hour, minute FROM reminders"):
        schedule_reminder(app.job_queue, chat_id, hour, minute)


# ---------- Quiz ----------
async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data["quiz"]
    topic = TOPICS[state["t"]]
    qi = state["q"]
    question, options, _ = topic["quiz"][qi]
    buttons = [
        [InlineKeyboardButton(opt, callback_data=f"ans:{qi}:{oi}")]
        for oi, opt in enumerate(options)
    ]
    await update.effective_message.reply_text(
        f"🧠 {topic['title']}\nSavol {qi + 1}/{len(topic['quiz'])}\n\n{question}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, qi: int, oi: int):
    query = update.callback_query
    state = context.user_data.get("quiz")
    if not state or state["q"] != qi:
        await query.answer("Bu savolga allaqachon javob berilgan")
        return

    topic = TOPICS[state["t"]]
    question, options, correct = topic["quiz"][qi]
    total = len(topic["quiz"])

    if oi == correct:
        state["s"] += 1
        verdict = "✅ To'g'ri!"
    else:
        verdict = f"❌ Noto'g'ri. To'g'ri javob: {options[correct]}"

    await query.answer()
    await query.edit_message_text(
        f"🧠 {topic['title']}\nSavol {qi + 1}/{total}\n\n{question}\n\nSiz: {options[oi]}\n{verdict}"
    )

    state["q"] += 1
    if state["q"] < total:
        await send_question(update, context)
    else:
        score = state["s"]
        if score == total:
            comment = "Ajoyib! 🏆"
        elif score >= total / 2:
            comment = "Yaxshi, davom eting! 👍"
        else:
            comment = "Darsni yana bir bor o'qib chiqing 📖"
        context.user_data.pop("quiz", None)
        await update.effective_message.reply_text(
            f"🏁 Quiz tugadi!\nNatija: {score}/{total}\n{comment}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔁 Qayta topshirish", callback_data=f"quiz:{state['t']}")],
                    [InlineKeyboardButton("📚 Darslar", callback_data="lessons")],
                ]
            ),
        )


# ---------- Tarjima ----------
def get_direction(context) -> str:
    return context.user_data.get("direction", "uz_en")


def translate_sync(text: str, src: str, tgt: str) -> str:
    return GoogleTranslator(source=src, target=tgt).translate(text)


async def translate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) > 4000:
        await update.message.reply_text("⚠️ Matn juda uzun. 4000 belgidan kamroq yuboring.")
        return

    src, tgt, label = DIRECTIONS[get_direction(context)]
    await update.message.chat.send_action("typing")
    try:
        result = await asyncio.to_thread(translate_sync, text, src, tgt)
    except Exception as e:
        print("Tarjima xatosi:", e)
        await update.message.reply_text("😕 Tarjima qilib bo'lmadi. Keyinroq urinib ko'ring.")
        return

    await update.message.reply_text(f"{label}\n\n{result}", reply_markup=swap_keyboard())


# ---------- Buyruqlar ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"Salom, {name}! 👋\nMen ingliz tili yordamchi botman. Nimadan boshlaymiz?",
        reply_markup=main_menu(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌍 Ingliz tili o'rganish boti: darslar, quiz, kunlik eslatma va tarjimon.\n"
        "Python va python-telegram-bot bilan Bahriddin tomonidan yozilgan. 🚀"
    )


async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    if not context.args:
        await update.message.reply_text(
            "Vaqtni shunday yozing: /remind 20:30\nYoki tugmalardan tanlang:",
            reply_markup=remind_keyboard(),
        )
        return
    try:
        hour, minute = map(int, context.args[0].split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Format noto'g'ri. Masalan: /remind 20:30")
        return
    save_reminder(context.job_queue, update.effective_chat.id, hour, minute)
    await update.message.reply_text(
        f"✅ Eslatma o'rnatildi: har kuni {hour:02d}:{minute:02d} (Toshkent vaqti)"
    )


async def remind_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remove_reminder(context.job_queue, update.effective_chat.id)
    await update.message.reply_text("❌ Eslatma o'chirildi.")


async def name_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    remember_user(update)
    if not context.args:
        await update.message.reply_text(
            f"Hozirgi ismingiz: {get_name(chat_id)}\n"
            "O'zgartirish uchun: /name Bahriddin Shomurodov"
        )
        return
    name = " ".join(context.args)[:50]
    set_name(chat_id, name)
    await update.message.reply_text(f"✅ Yaxshi, endi sizni {name} deb chaqiraman.")


# ---------- Tugmalar ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    msg = update.effective_message
    chat_id = update.effective_chat.id

    if data == "lessons":
        await query.answer()
        await msg.reply_text("📚 Mavzuni tanlang:", reply_markup=lessons_keyboard())

    elif data.startswith("lesson:"):
        i = int(data.split(":")[1])
        await query.answer()
        await msg.reply_text(TOPICS[i]["lesson"], reply_markup=lesson_keyboard(i))

    elif data == "quizmenu":
        await query.answer()
        await msg.reply_text("🧠 Qaysi mavzu bo'yicha quiz?", reply_markup=quizmenu_keyboard())

    elif data.startswith("quiz:"):
        i = int(data.split(":")[1])
        context.user_data["quiz"] = {"t": i, "q": 0, "s": 0}
        await query.answer()
        await send_question(update, context)

    elif data.startswith("ans:"):
        _, qi, oi = data.split(":")
        await handle_answer(update, context, int(qi), int(oi))

    elif data == "remmenu":
        await query.answer()
        await msg.reply_text(
            "⏰ Har kuni qaysi vaqtda eslatay? (Toshkent vaqti)\n"
            "Boshqa vaqt uchun: /remind 20:30",
            reply_markup=remind_keyboard(),
        )

    elif data == "rem:off":
        remove_reminder(context.job_queue, chat_id)
        await query.answer("Eslatma o'chirildi")
        await msg.reply_text("❌ Eslatma o'chirildi.")

    elif data.startswith("rem:"):
        remember_user(update)
        hour, minute = map(int, data[4:].split(":"))
        save_reminder(context.job_queue, chat_id, hour, minute)
        await query.answer("Saqlandi ✅")
        await msg.reply_text(f"✅ Eslatma o'rnatildi: har kuni {hour:02d}:{minute:02d}")

    elif data == "trans":
        await query.answer()
        await msg.reply_text(
            "🌍 Tarjimon: yo'nalishni tanlang, so'ng matn yozing:",
            reply_markup=trans_keyboard(get_direction(context)),
        )

    elif data.startswith("dir:"):
        key = data.split(":")[1]
        context.user_data["direction"] = key
        await query.answer("Yo'nalish o'zgardi")
        await query.edit_message_reply_markup(reply_markup=trans_keyboard(key))

    elif data == "swap":
        new_key = "en_uz" if get_direction(context) == "uz_en" else "uz_en"
        context.user_data["direction"] = new_key
        await query.answer()
        await msg.reply_text(f"Yo'nalish almashtirildi:\n{DIRECTIONS[new_key][2]}")

    else:
        await query.answer()


def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("remind", remind_command))
    app.add_handler(CommandHandler("remind_off", remind_off))
    app.add_handler(CommandHandler("name", name_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, translate_message))
    print("Bot ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()