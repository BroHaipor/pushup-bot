import os
import json
import logging
from datetime import datetime
from pathlib import Path

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

from keep_alive import keep_alive

# ─── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Storage ─────────────────────────────────────────────────────────────────

DATA_FILE = Path("data.json")

# ConversationHandler states
WAITING_PUSHUPS_AMOUNT = 1
WAITING_NEW_NAME = 2


def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user(data: dict, user_id: int, telegram_name: str) -> dict:
    key = str(user_id)
    if key not in data:
        data[key] = {
            "name": telegram_name,
            "pushups": 0,
            "last_updated": None,
        }
    return data[key]


# ─── Keyboards ────────────────────────────────────────────────────────────────

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 Топ участников", callback_data="top")],
        [InlineKeyboardButton("💪 Изменить отжимания", callback_data="edit_pushups")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
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

def get_user_rank(data: dict, user_id: int) -> int:
    sorted_users = sorted(data.values(), key=lambda u: u["pushups"], reverse=True)
    user_pushups = data[str(user_id)]["pushups"]
    for rank, user in enumerate(sorted_users, start=1):
        if user["pushups"] == user_pushups and user["name"] == data[str(user_id)]["name"]:
            return rank
    return len(data)


def format_date(iso_date: str | None) -> str:
    if not iso_date:
        return "никогда"
    dt = datetime.fromisoformat(iso_date)
    return dt.strftime("%d.%m.%Y %H:%M")


def build_top_text(data: dict) -> str:
    if not data:
        return "Пока никого нет 😴"

    sorted_users = sorted(data.values(), key=lambda u: u["pushups"], reverse=True)
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["<b>🏆 Топ участников</b>\n"]

    for rank, user in enumerate(sorted_users, start=1):
        medal = medals.get(rank, f"{rank}.")
        date = format_date(user["last_updated"])
        lines.append(
            f"{medal} <b>{user['name']}</b> — {user['pushups']} отж.\n"
            f"    📅 {date}"
        )

    return "\n".join(lines)


def build_profile_text(user: dict, rank: int) -> str:
    return (
        f"👤 <b>Профиль</b>\n\n"
        f"Имя: <b>{user['name']}</b>\n"
        f"Отжиманий: <b>{user['pushups']}</b>\n"
        f"Место в топе: <b>#{rank}</b>\n"
        f"Последнее обновление: <b>{format_date(user['last_updated'])}</b>"
    )


# ─── Handlers ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    tg_user = update.effective_user
    get_user(data, tg_user.id, tg_user.first_name)
    save_data(data)

    await update.message.reply_text(
        f"Привет, <b>{tg_user.first_name}</b>! 👋\nВыбери действие:",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )


async def callback_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Главное меню:",
        reply_markup=main_menu_keyboard(),
    )


async def callback_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = load_data()
    await query.edit_message_text(
        build_top_text(data),
        reply_markup=back_keyboard(),
        parse_mode="HTML",
    )


async def callback_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = load_data()
    tg_user = update.effective_user
    user = get_user(data, tg_user.id, tg_user.first_name)
    save_data(data)

    rank = get_user_rank(data, tg_user.id)
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

    context.user_data["pushups_action"] = query.data  # "pushups_add" or "pushups_sub"

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

    data = load_data()
    tg_user = update.effective_user
    user = get_user(data, tg_user.id, tg_user.first_name)

    if action == "pushups_add":
        user["pushups"] += amount
        verb = f"➕ Добавлено <b>{amount}</b> отжиманий"
    else:
        user["pushups"] = max(0, user["pushups"] - amount)
        verb = f"➖ Убрано <b>{amount}</b> отжиманий"

    user["last_updated"] = datetime.now().isoformat()
    save_data(data)

    await update.message.reply_text(
        f"{verb}\nВсего: <b>{user['pushups']}</b> 💪",
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

    if len(new_name) > 30:
        await update.message.reply_text("Слишком длинное имя, попробуй покороче:")
        return WAITING_NEW_NAME

    if not new_name:
        await update.message.reply_text("Имя не может быть пустым:")
        return WAITING_NEW_NAME

    data = load_data()
    tg_user = update.effective_user
    user = get_user(data, tg_user.id, tg_user.first_name)
    user["name"] = new_name
    save_data(data)

    await update.message.reply_text(
        f"✅ Имя изменено на <b>{new_name}</b>",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Срабатывает если пользователь нажал 'Отмена' во время диалога."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Главное меню:",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


# ─── App Setup ───────────────────────────────────────────────────────────────

def build_application() -> Application:
    token = os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).build()

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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(pushups_conversation)
    app.add_handler(name_conversation)
    app.add_handler(CallbackQueryHandler(callback_main_menu, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(callback_top, pattern="^top$"))
    app.add_handler(CallbackQueryHandler(callback_profile, pattern="^profile$"))
    app.add_handler(CallbackQueryHandler(callback_edit_pushups, pattern="^edit_pushups$"))

    return app


def main() -> None:
    keep_alive()
    logger.info("Starting bot...")
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
