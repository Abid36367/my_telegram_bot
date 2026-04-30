import logging
import sqlite3
import httpx
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, PreCheckoutQueryHandler,
    filters, ContextTypes
)

# ==================== CONFIG ====================
BOT_TOKEN          = "8661488734:AAH-BV1Qh4gISloOehIKKwV-0IF7JlFbBEg"
GROQ_API_KEY       = "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"
ADMIN_ID           = 6761561876
FREE_LIMIT         = 5
STARS_PER_QUESTION = 10

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ==================== DATABASE ====================
DB = "study_bot.db"

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id          INTEGER PRIMARY KEY,
        username         TEXT,
        full_name        TEXT,
        question_count   INTEGER DEFAULT 0,
        paid_credits     INTEGER DEFAULT 0,
        total_stars_paid INTEGER DEFAULT 0,
        created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # paid_credits column পুরনো DB-তে না থাকলে যোগ করো
    try:
        c.execute("ALTER TABLE users ADD COLUMN paid_credits INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column আগে থেকেই আছে

    c.execute("""CREATE TABLE IF NOT EXISTS pending_payments (
        user_id          INTEGER PRIMARY KEY,
        pending_question TEXT,
        created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()

def upsert_user(user_id, username, full_name):
    conn = sqlite3.connect(DB)
    conn.execute("""
        INSERT INTO users (user_id, username, full_name) VALUES (?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            full_name=excluded.full_name
    """, (user_id, username, full_name))
    conn.commit()
    conn.close()

def get_question_count(user_id):
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT question_count FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else 0

def get_paid_credits(user_id):
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT paid_credits FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else 0

def add_paid_credit(user_id):
    """পেমেন্ট সফল হলে ১টি credit যোগ করো"""
    conn = sqlite3.connect(DB)
    conn.execute(
        "UPDATE users SET paid_credits=paid_credits+1 WHERE user_id=?", (user_id,)
    )
    conn.commit()
    conn.close()

def use_paid_credit(user_id):
    """পেইড প্রশ্নের উত্তর দেওয়ার সময় ১টি credit কমাও"""
    conn = sqlite3.connect(DB)
    conn.execute(
        "UPDATE users SET paid_credits=paid_credits-1 WHERE user_id=?", (user_id,)
    )
    conn.commit()
    conn.close()

def increment_question(user_id):
    conn = sqlite3.connect(DB)
    conn.execute(
        "UPDATE users SET question_count=question_count+1 WHERE user_id=?", (user_id,)
    )
    conn.commit()
    conn.close()

def add_stars_paid(user_id, stars):
    conn = sqlite3.connect(DB)
    conn.execute(
        "UPDATE users SET total_stars_paid=total_stars_paid+? WHERE user_id=?",
        (stars, user_id)
    )
    conn.commit()
    conn.close()

def save_pending(user_id, question):
    conn = sqlite3.connect(DB)
    conn.execute("""
        INSERT INTO pending_payments (user_id, pending_question) VALUES (?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            pending_question=excluded.pending_question,
            created_at=CURRENT_TIMESTAMP
    """, (user_id, question))
    conn.commit()
    conn.close()

def get_pending(user_id):
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT pending_question FROM pending_payments WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None

def clear_pending(user_id):
    conn = sqlite3.connect(DB)
    conn.execute("DELETE FROM pending_payments WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def get_user_stats(user_id):
    conn = sqlite3.connect(DB)
    row = conn.execute(
        "SELECT question_count, paid_credits, total_stars_paid FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    return row if row else (0, 0, 0)

# ==================== GROQ REST API ====================
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
GROQ_HEADERS = {
    "Authorization": f"Bearer {GROQ_API_KEY}",
    "Content-Type": "application/json"
}
GROQ_MODEL = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = (
    "তুমি একজন বুদ্ধিমান ও বন্ধুত্বপূর্ণ AI সহকারী। "
    "তুমি যেকোনো বিষয়ে প্রশ্নের উত্তর দিতে পারো — পড়াশোনা, বিজ্ঞান, প্রযুক্তি, "
    "রান্না, খেলাধুলা, বিনোদন, ব্যবসা, স্বাস্থ্য, ভ্রমণ, সাধারণ জ্ঞান — সব বিষয়ে।\n\n"
    "নিয়মাবলী:\n"
    "১. যেকোনো প্রশ্নের উত্তর সহজ, স্পষ্ট ও বাংলায় দাও।\n"
    "২. প্রয়োজনে উদাহরণ দাও এবং ধাপে ধাপে বুঝিয়ে দাও।\n"
    "৩. সবসময় সৎ ও নির্ভুল তথ্য দাও।\n"
    "৪. ব্যবহারকারীকে সম্মান করো এবং ইতিবাচক মনোভাব রাখো।\n"
    "৫. যদি কোনো প্রশ্নের উত্তর জানা না থাকে, সৎভাবে বলো।"
)

async def ask_groq(question: str) -> str:
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": question},
        ],
        "max_tokens": 1024,
        "temperature": 0.7,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(GROQ_URL, headers=GROQ_HEADERS, json=payload)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as e:
        logger.error(f"Groq HTTP {e.response.status_code}: {e.response.text}")
        return f"❌ API ত্রুটি ({e.response.status_code})। একটু পরে চেষ্টা করুন।"
    except httpx.TimeoutException:
        return "❌ উত্তর পেতে সময় বেশি লাগছে। আবার চেষ্টা করুন।"
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return "❌ সমস্যা হয়েছে। একটু পরে আবার চেষ্টা করুন।"

# ==================== HELPERS ====================

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def pay_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            text=f"⭐ {STARS_PER_QUESTION} Stars দিয়ে উত্তর নিন",
            callback_data="pay_now"
        )
    ]])

async def send_invoice(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_invoice(
        chat_id=chat_id,
        title="🤖 AI সহকারী",
        description=f"১টি প্রশ্নের উত্তরের জন্য {STARS_PER_QUESTION} Telegram Stars",
        payload=f"q_{user_id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="প্রশ্নের উত্তর", amount=STARS_PER_QUESTION)],
    )

async def process_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    question: str,
    user_id: int,
    is_paid: bool = False,  # ← পেইড প্রশ্ন কিনা
):
    thinking = await update.effective_message.reply_text(
        "🤔 উত্তর তৈরি করছি, একটু অপেক্ষা করুন..."
    )
    answer = await ask_groq(question)

    # question count বাড়াও
    increment_question(user_id)

    # পেইড হলে credit কমাও
    if is_paid:
        use_paid_credit(user_id)

    if is_admin(user_id):
        footer = "\n\n👑 *Admin — আনলিমিটেড ফ্রি*"
    else:
        count   = get_question_count(user_id)
        credits = get_paid_credits(user_id)
        remaining = max(0, FREE_LIMIT - count)
        if remaining > 0:
            footer = f"\n\n📊 ফ্রি প্রশ্ন বাকি: {remaining}টি"
        elif credits > 0:
            footer = f"\n\n⭐ Stars credit বাকি: {credits}টি"
        else:
            footer = f"\n\n⭐ পরবর্তী প্রশ্নে {STARS_PER_QUESTION} Stars লাগবে"

    await thinking.delete()
    await update.effective_message.reply_text(
        f"💬 *উত্তর:*\n\n{answer}{footer}",
        parse_mode="Markdown"
    )

# ==================== COMMAND HANDLERS ====================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username or "", u.full_name or "")
    count, credits, _ = get_user_stats(u.id)

    if is_admin(u.id):
        status_text = "👑 *Admin — আনলিমিটেড ফ্রি*"
    else:
        remaining   = max(0, FREE_LIMIT - count)
        status_text = (
            f"✅ মোট প্রশ্ন: {count}টি\n"
            f"🎁 ফ্রি প্রশ্ন বাকি: {remaining}টি\n"
            f"⭐ Stars credit: {credits}টি"
        )

    await update.message.reply_text(
        f"🤖 *স্বাগতম, {u.first_name}!*\n\n"
        "আমি যেকোনো বিষয়ে তোমার প্রশ্নের উত্তর দিতে পারি!\n\n"
        f"📊 *তোমার অবস্থা:*\n{status_text}\n\n"
        "💡 /stats — পরিসংখ্যান | /help — সাহায্য\n\n"
        "যেকোনো প্রশ্ন টাইপ করো! 👇",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *কীভাবে ব্যবহার করবে:*\n\n"
        "১. যেকোনো বিষয়ে প্রশ্ন টাইপ করো\n"
        f"২. প্রথম {FREE_LIMIT}টি প্রশ্ন সম্পূর্ণ ফ্রি\n"
        f"৩. এরপর প্রতি প্রশ্নে {STARS_PER_QUESTION} ⭐ Stars লাগবে\n\n"
        "🌍 *যে বিষয়ে প্রশ্ন করতে পারো:*\n"
        "• পড়াশোনা, বিজ্ঞান, গণিত, প্রযুক্তি\n"
        "• রান্না, স্বাস্থ্য, খেলাধুলা\n"
        "• বিনোদন, ভ্রমণ, ব্যবসা\n"
        "• সাধারণ জ্ঞান ও যেকোনো কিছু!",
        parse_mode="Markdown"
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username or "", u.full_name or "")
    count, credits, stars = get_user_stats(u.id)

    if is_admin(u.id):
        await update.message.reply_text(
            f"📊 *Admin পরিসংখ্যান:*\n\n"
            f"👑 Status: আনলিমিটেড ফ্রি\n"
            f"🔢 মোট প্রশ্ন: {count}টি\n"
            f"⭐ মোট Stars আয়: {stars}",
            parse_mode="Markdown"
        )
    else:
        remaining = max(0, FREE_LIMIT - count)
        paid_q    = max(0, count - FREE_LIMIT)
        await update.message.reply_text(
            f"📊 *তোমার পরিসংখ্যান:*\n\n"
            f"🔢 মোট প্রশ্ন: {count}টি\n"
            f"🎁 ফ্রি প্রশ্ন বাকি: {remaining}টি\n"
            f"⭐ Stars credit বাকি: {credits}টি\n"
            f"💰 পেইড প্রশ্ন: {paid_q}টি\n"
            f"💫 মোট Stars খরচ: {stars}",
            parse_mode="Markdown"
        )

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    conn = sqlite3.connect(DB)
    row  = conn.execute(
        "SELECT COUNT(*), SUM(question_count), SUM(total_stars_paid) FROM users"
    ).fetchone()
    conn.close()
    users, questions, stars = row
    await update.message.reply_text(
        f"🔧 *Admin Panel:*\n\n"
        f"👥 মোট ইউজার: {users or 0}\n"
        f"❓ মোট প্রশ্ন: {questions or 0}\n"
        f"⭐ মোট Stars আয়: {stars or 0}",
        parse_mode="Markdown"
    )

# ==================== MESSAGE / PAYMENT HANDLERS ====================

async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u        = update.effective_user
    user_id  = u.id
    question = update.message.text.strip()

    upsert_user(user_id, u.username or "", u.full_name or "")

    # Admin — সরাসরি উত্তর
    if is_admin(user_id):
        await process_question(update, context, question, user_id)
        return

    count   = get_question_count(user_id)
    credits = get_paid_credits(user_id)

    if count < FREE_LIMIT:
        # ফ্রি প্রশ্ন
        await process_question(update, context, question, user_id, is_paid=False)
    elif credits > 0:
        # Stars credit আছে — সেটা ব্যবহার করো
        await process_question(update, context, question, user_id, is_paid=True)
    else:
        # কোনো credit নেই — পেমেন্ট চাও
        save_pending(user_id, question)
        await update.message.reply_text(
            f"🚫 *আপনার {FREE_LIMIT}টি ফ্রি প্রশ্ন শেষ!*\n\n"
            f"পরবর্তী উত্তরের জন্য *{STARS_PER_QUESTION} ⭐ Telegram Stars* পে করুন।\n\n"
            "নিচের বাটনে ক্লিক করুন 👇",
            parse_mode="Markdown",
            reply_markup=pay_keyboard()
        )

async def handle_pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_invoice(query.message.chat_id, query.from_user.id, context)

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    stars   = update.message.successful_payment.total_amount

    # Stars সংরক্ষণ করো
    add_stars_paid(user_id, stars)

    # ✅ এখানেই credit যোগ হবে
    add_paid_credit(user_id)

    logger.info(f"✅ Payment received: user={user_id}, stars={stars}")

    await update.message.reply_text(
        f"✅ *{stars} ⭐ Stars পেমেন্ট সফল! Credit যোগ হয়েছে।*\n\nএখন উত্তর দিচ্ছি...",
        parse_mode="Markdown"
    )

    pending_q = get_pending(user_id)
    if pending_q:
        clear_pending(user_id)
        # is_paid=True দিলে process_question নিজেই credit কমাবে
        await process_question(update, context, pending_q, user_id, is_paid=True)
    else:
        await update.message.reply_text(
            "✅ Credit যোগ হয়েছে! এখন প্রশ্নটি টাইপ করুন।"
        )

# ==================== MAIN ====================

def main():
    init_db()
    logger.info("🤖 AI Bot চালু হচ্ছে...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(handle_pay_callback, pattern="^pay_now$"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_question))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
