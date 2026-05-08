import os
import json
import logging
from datetime import datetime, timezone, timedelta
from io import BytesIO

import psycopg
from psycopg.rows import dict_row
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
IDEA_CHAT_ID = -5142351517
KYIV_TZ = timezone(timedelta(hours=3))

WAITING_PUSHUPS_AMOUNT = 1
WAITING_NEW_NAME = 2
WAITING_IDEA_TEXT = 3

# ─── Database ─────────────────────────────────────────────────────────────────

def get_connection():
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return psycopg.connect(url, row_factory=dict_row)


def init_db():
    """Создаёт таблицу если её нет. Безопасно вызывать при каждом старте."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      BIGINT PRIMARY KEY,
                name         TEXT NOT NULL,
                pushups      INTEGER NOT NULL DEFAULT 0,
                last_updated TIMESTAMPTZ,
                joined_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        """)
        conn.commit()
    logger.info("Database ready.")


def db_get_or_create_user(user_id: int, telegram_name: str) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = %s", (user_id,)
        ).fetchone()
        if row:
            return dict(row)
        row = conn.execute(
            "INSERT INTO users (user_id, name, pushups) VALUES (%s, %s, 0) RETURNING *",
            (user_id, telegram_name),
        ).fetchone()
        conn.commit()
        return dict(row)


def db_get_user(user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = %s", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def db_update_pushups(user_id: int, new_pushups: int):
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET pushups = %s, last_updated = %s WHERE user_id = %s",
            (new_pushups, datetime.now(KYIV_TZ), user_id),
        )
        conn.commit()


def db_update_name(user_id: int, new_name: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET name = %s WHERE user_id = %s",
            (new_name, user_id),
        )
        conn.commit()


def db_get_all_users() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY pushups DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def db_get_user_rank(user_id: int) -> int:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT rank FROM (
                SELECT user_id, RANK() OVER (ORDER BY pushups DESC) AS rank
                FROM users
            ) ranked
            WHERE user_id = %s
        """, (user_id,)).fetchone()
        return row["rank"] if row else 0


# ─── Keyboards ────────────────────────────────────────────────────────────────

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 Топ участников", callback_data="top")],
        [InlineKeyboardButton("💪 Изменить отжимания", callback_data="edit_pushups")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
        [InlineKeyboardButton("💡 Предложить идею", callback_data="suggest_idea")],
    ])


def edit_pushups_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Добавить", callback_data="pushups_add"),
            InlineKeyboardButton("➖ Убрать", callback_data="pushups_sub"),
        ],
        [InlineKeyboardButton("« Назад", callback_data="main_menu")],
    ])


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить имя", callback_data="change_name")],
        [InlineKeyboardButton("« Назад", callback_data="main_menu")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("« Назад в меню", callback_data="main_menu")],
    ])


# ─── Helpers ─────────────────────────────────────────────────────────────────

def format_date(dt) -> str:
    if not dt:
        return "никогда"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is not None:
        dt = dt.astimezone(KYIV_TZ)
    return dt.strftime("%d.%m.%Y %H:%M")


def build_top_text(users: list[dict]) -> str:
    if not users:
        return "Пока никого нет 😴"

    total = sum(u["pushups"] for u in users)
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"<b>Топ участников</b>\n💪 Всего отжиманий: <b>{total}</b>\n"]

    for rank, user in enumerate(users, start=1):
        medal = medals.get(rank, f"{rank}.")
        lines.append(
            f"{medal} <b>{user['name']}</b> — {user['pushups']} отж.\n"
            f"{format_date(user['last_updated'])}"
        )

    return "\n\n".join(lines)


def build_profile_text(user: dict, rank: int) -> str:
    return (
        f"👤 <b>Профиль</b>\n\n"
        f"Имя: <b>{user['name']}</b>\n"
        f"Отжиманий: <b>{user['pushups']}</b>\n"
        f"Место в топе: <b>#{rank}</b>\n"
        f"Дата старта: <b>{format_date(user.get('joined_at'))}</b>\n"
        f"Последнее обновление: <b>{format_date(user['last_updated'])}</b>"
    )


# ─── Handlers ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    db_get_or_create_user(tg_user.id, tg_user.first_name)

    await update.message.reply_text(
        f"Привет, <b>{tg_user.first_name}</b>! 👋\nВыбери действие:",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет доступа.")
        return

    users = db_get_all_users()

    for user in users:
        if user.get("last_updated"):
            user["last_updated"] = user["last_updated"].isoformat()
        if user.get("joined_at"):
            user["joined_at"] = user["joined_at"].isoformat()

    json_bytes = json.dumps(users, ensure_ascii=False, indent=2).encode("utf-8")
    file = BytesIO(json_bytes)
    file.name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    await update.message.reply_document(document=file, caption="📦 Бэкап базы данных")


async def callback_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Главное меню:", reply_markup=main_menu_keyboard())


async def callback_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    users = db_get_all_users()
    await query.edit_message_text(
        build_top_text(users),
        reply_markup=back_keyboard(),
        parse_mode="HTML",
    )


async def callback_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    tg_user = update.effective_user
    user = db_get_or_create_user(tg_user.id, tg_user.first_name)
    rank = db_get_user_rank(tg_user.id)

    await query.edit_message_text(
        build_profile_text(user, rank),
        reply_markup=profile_keyboard(),
        parse_mode="HTML",
    )


async def callback_edit_pushups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "💪 Что хочешь сделать с отжиманиями?",
        reply_markup=edit_pushups_keyboard(),
    )


async def callback_pushups_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    context.user_data["pushups_action"] = query.data

    action_text = "добавить" if query.data == "pushups_add" else "убрать"
    await query.edit_message_text(
        f"Введи количество отжиманий, которое хочешь {action_text}:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Отмена", callback_data="main_menu")]
        ]),
    )
    return WAITING_PUSHUPS_AMOUNT


async def receive_pushups_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("Введи положительное целое число:")
        return WAITING_PUSHUPS_AMOUNT

    amount = int(text)
    action = context.user_data.get("pushups_action")
    tg_user = update.effective_user

    user = db_get_or_create_user(tg_user.id, tg_user.first_name)

    if action == "pushups_add":
        new_total = user["pushups"] + amount
        verb = f"➕ Добавлено <b>{amount}</b> отжиманий"
    else:
        new_total = max(0, user["pushups"] - amount)
        verb = f"➖ Убрано <b>{amount}</b> отжиманий"

    db_update_pushups(tg_user.id, new_total)

    await update.message.reply_text(
        f"{verb}\nВсего: <b>{new_total}</b> 💪",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def callback_change_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "Введи новое имя (до 30 символов):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Отмена", callback_data="main_menu")]
        ]),
    )
    return WAITING_NEW_NAME


async def receive_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_name = update.message.text.strip()

    if len(new_name) > 30 or not new_name:
        await update.message.reply_text("Имя должно быть от 1 до 30 символов:")
        return WAITING_NEW_NAME

    db_update_name(update.effective_user.id, new_name)

    await update.message.reply_text(
        f"✅ Имя изменено на <b>{new_name}</b>",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def callback_suggest_idea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "💡 Напиши свою идею — она улетит в общий чат:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Отмена", callback_data="main_menu")]
        ]),
    )
    return WAITING_IDEA_TEXT


async def receive_idea_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_user = update.effective_user
    user = db_get_or_create_user(tg_user.id, tg_user.first_name)
    idea = update.message.text.strip()

    await context.bot.send_message(
        chat_id=IDEA_CHAT_ID,
        text=f"💡 <b>Идея от {user['name']}</b>\n\n{idea}",
        parse_mode="HTML",
    )

    await update.message.reply_text(
        "✅ Идея отправлена в чат!",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Главное меню:", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


# ─── App Setup ───────────────────────────────────────────────────────────────

def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    pushups_conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(callback_pushups_action, pattern="^pushups_(add|sub)$"),
        ],
        states={
            WAITING_PUSHUPS_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pushups_amount),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_conversation, pattern="^main_menu$"),
        ],
    )

    name_conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(callback_change_name, pattern="^change_name$"),
        ],
        states={
            WAITING_NEW_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_name),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_conversation, pattern="^main_menu$"),
        ],
    )

    idea_conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(callback_suggest_idea, pattern="^suggest_idea$"),
        ],
        states={
            WAITING_IDEA_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_idea_text),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_conversation, pattern="^main_menu$"),
        ],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(pushups_conversation)
    app.add_handler(name_conversation)
    app.add_handler(idea_conversation)
    app.add_handler(CallbackQueryHandler(callback_main_menu, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(callback_top, pattern="^top$"))
    app.add_handler(CallbackQueryHandler(callback_profile, pattern="^profile$"))
    app.add_handler(CallbackQueryHandler(callback_edit_pushups, pattern="^edit_pushups$"))

    return app


def main() -> None:
    init_db()
    logger.info("Starting bot...")
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
