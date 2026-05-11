import os
import json
import logging
from datetime import datetime, timezone, timedelta, date
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

# ─── Changelog — редактируй сам ──────────────────────────────────────────────
# Добавляй новые записи В НАЧАЛО списка

CHANGELOG = [
    ("11.05.2026", "v1.2", 'Добавил раздел "прочее" и историю изменений, теперь просмотр статистики стал функциональнее + сделал кайфовый апдейт топа отжиманий'),
    ("10.05.2026", "v1.1", "Добавил историю изменений в профиле"),
    ("08.05.2026", "v1.0", "Запуск бота 🎉"),
]

MAX_PUSHUPS_PER_ACTION = 2000

# ─── Conversation states ──────────────────────────────────────────────────────

WAITING_PUSHUPS_AMOUNT = 1
WAITING_NEW_NAME = 2
WAITING_IDEA_TEXT = 3
WAITING_CUSTOM_RANGE = 4

# ─── Database ─────────────────────────────────────────────────────────────────

def get_connection():
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return psycopg.connect(url, row_factory=dict_row)


def init_db():
    """Создаёт таблицы если их нет. Безопасно вызывать при каждом старте."""
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pushup_history (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT NOT NULL REFERENCES users(user_id),
                amount     INTEGER NOT NULL,
                day        DATE NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_user_day
            ON pushup_history(user_id, day)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id   BIGINT PRIMARY KEY,
                banned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
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


def db_update_pushups(user_id: int, new_pushups: int, delta: int):
    """Обновляет общий счёт и пишет запись в историю."""
    now = datetime.now(KYIV_TZ)
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET pushups = %s, last_updated = %s WHERE user_id = %s",
            (new_pushups, now, user_id),
        )
        conn.execute(
            "INSERT INTO pushup_history (user_id, amount, day) VALUES (%s, %s, %s)",
            (user_id, delta, now.date()),
        )
        conn.commit()


def db_update_name(user_id: int, new_name: str):
    with get_connection() as conn:
        conn.execute("UPDATE users SET name = %s WHERE user_id = %s", (new_name, user_id))
        conn.commit()


def db_get_all_users() -> list[dict]:
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM users ORDER BY pushups DESC"
        ).fetchall()]


def db_get_user_rank(user_id: int) -> int:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT rank FROM (
                SELECT user_id, RANK() OVER (ORDER BY pushups DESC) AS rank
                FROM users
            ) ranked WHERE user_id = %s
        """, (user_id,)).fetchone()
        return row["rank"] if row else 0


def db_get_today_pushups(user_id: int) -> int:
    today = datetime.now(KYIV_TZ).date()
    with get_connection() as conn:
        row = conn.execute("""
            SELECT COALESCE(SUM(amount), 0) as total
            FROM pushup_history
            WHERE user_id = %s AND day = %s
        """, (user_id, today)).fetchone()
        return row["total"] if row else 0


def db_get_last_activity(user_id: int) -> date | None:
    """Возвращает дату последней записи в истории."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(day) as last_day FROM pushup_history WHERE user_id = %s",
            (user_id,)
        ).fetchone()
        return row["last_day"] if row and row["last_day"] else None


def db_get_all_users_with_stats() -> list[dict]:
    """Возвращает всех пользователей с отжиманиями за сегодня и датой последней активности."""
    today = datetime.now(KYIV_TZ).date()
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT
                u.*,
                COALESCE(h_today.total, 0) AS today_pushups,
                h_last.last_day AS last_activity_day
            FROM users u
            LEFT JOIN (
                SELECT user_id, SUM(amount) AS total
                FROM pushup_history WHERE day = %s
                GROUP BY user_id
            ) h_today ON u.user_id = h_today.user_id
            LEFT JOIN (
                SELECT user_id, MAX(day) AS last_day
                FROM pushup_history
                GROUP BY user_id
            ) h_last ON u.user_id = h_last.user_id
            ORDER BY u.pushups DESC
        """, (today,)).fetchall()
        return [dict(r) for r in rows]


def db_get_history(user_id: int, from_date: date, to_date: date) -> dict:
    """Возвращает {дата: сумма за день} за указанный промежуток."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT day, SUM(amount) as total
            FROM pushup_history
            WHERE user_id = %s AND day BETWEEN %s AND %s
            GROUP BY day ORDER BY day
        """, (user_id, from_date, to_date)).fetchall()
    return {row["day"]: row["total"] for row in rows}


def db_is_banned(user_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM banned_users WHERE user_id = %s", (user_id,)
        ).fetchone()
        return row is not None


def db_ban_user(user_id: int):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO banned_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (user_id,)
        )
        conn.commit()


def db_unban_user(user_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM banned_users WHERE user_id = %s", (user_id,))
        conn.commit()


# ─── Keyboards ────────────────────────────────────────────────────────────────

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 Топ участников", callback_data="top")],
        [InlineKeyboardButton("💪 Добавить отжимания", callback_data="edit_pushups")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
        [InlineKeyboardButton("⚙️ Прочее", callback_data="misc")],
    ])


TOP_PAGE_SIZE = 15


def top_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"top_page_{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1} / {total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"top_page_{page + 1}"))

    rows = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("« Назад в меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def misc_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💡 Предложить идею", callback_data="suggest_idea")],
        [InlineKeyboardButton("📋 Список изменений", callback_data="changelog")],
        [InlineKeyboardButton("« Назад", callback_data="main_menu")],
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
        [InlineKeyboardButton("📊 Статистика", callback_data="stats_7")],
        [InlineKeyboardButton("✏️ Изменить имя", callback_data="change_name")],
        [InlineKeyboardButton("« Назад", callback_data="main_menu")],
    ])


def stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("7 дней", callback_data="stats_7"),
            InlineKeyboardButton("1 месяц", callback_data="stats_30"),
        ],
        [InlineKeyboardButton("📅 Свой период", callback_data="stats_custom")],
        [InlineKeyboardButton("« Назад", callback_data="profile")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("« Назад в меню", callback_data="main_menu")],
    ])


# ─── Helpers ─────────────────────────────────────────────────────────────────

MAIN_MENU_TEXT = "Главное меню:\n🔖 v1.2 — значительное изменение топа и другое"


def format_date(dt) -> str:
    if not dt:
        return "никогда"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is not None:
        dt = dt.astimezone(KYIV_TZ)
    return dt.strftime("%d.%m.%Y %H:%M")


def build_top_text(users: list[dict], page: int) -> tuple[str, int]:
    """Возвращает (текст, total_pages)."""
    if not users:
        return "Пока никого нет 😴", 1

    today = datetime.now(KYIV_TZ).date()
    total_pushups = sum(u["pushups"] for u in users)
    total_pages = max(1, (len(users) + TOP_PAGE_SIZE - 1) // TOP_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * TOP_PAGE_SIZE
    page_users = users[start: start + TOP_PAGE_SIZE]

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"<b>Топ участников</b>\n💪 Всего отжиманий: <b>{total_pushups}</b>\n"]

    for i, user in enumerate(page_users):
        rank = start + i + 1
        medal = medals.get(rank, f"{rank}.")
        today_amt = user.get("today_pushups", 0)
        last_day = user.get("last_activity_day")

        if today_amt and today_amt > 0:
            sub = f"сегодня +{today_amt} отж."
        elif last_day:
            sub = f"последняя активность: {last_day.strftime('%d.%m.%Y')}"
        else:
            sub = "ещё не начинал"

        lines.append(
            f"{medal} <b>{user['name']}</b> — {user['pushups']} отж.\n"
            f"    {sub}"
        )

    return "\n\n".join(lines), total_pages


def build_profile_text(user: dict, rank: int, today_pushups: int) -> str:
    return (
        f"👤 <b>Профиль</b>\n\n"
        f"Имя: <b>{user['name']}</b>\n"
        f"Отжиманий всего: <b>{user['pushups']}</b>\n"
        f"Место в топе: <b>#{rank}</b>\n"
        f"Дата старта: <b>{format_date(user.get('joined_at'))}</b>\n"
        f"Отжиманий сегодня: <b>{today_pushups}</b>"
    )


def build_stats_text(user: dict, from_date: date, to_date: date, history: dict) -> str:
    days = (to_date - from_date).days + 1
    lines = [
        f"📊 <b>Статистика</b> — {user['name']}\n"
        f"📅 {from_date.strftime('%d.%m.%Y')} — {to_date.strftime('%d.%m.%Y')}\n"
    ]

    total_period = 0
    for i in range(days):
        day = from_date + timedelta(days=i)
        amount = history.get(day)
        label = day.strftime("%d.%m")
        if amount is not None:
            sign = "+" if amount >= 0 else ""
            lines.append(f"{label}  {sign}{amount} отж.")
            total_period += amount
        else:
            lines.append(f"{label}  —")

    lines.append(f"\n<b>Итого за период: {total_period} отж.</b>")
    return "\n".join(lines)


def build_changelog_text() -> str:
    lines = ["📋 <b>Список изменений</b>\n"]
    for entry_date, version, description in CHANGELOG:
        lines.append(f"<b>{version}</b> ({entry_date})\n{description}")
    return "\n\n".join(lines)


# ─── Handlers ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    if db_is_banned(tg_user.id):
        await update.message.reply_text("🚫 Ты заблокирован.")
        return
    db_get_or_create_user(tg_user.id, tg_user.first_name)

    await update.message.reply_text(
        f"Привет, <b>{tg_user.first_name}</b>! 👋\n{MAIN_MENU_TEXT}",
        reply_markup=main_menu_keyboard(),
        parse_mode="HTML",
    )


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет доступа.")
        return

    users = db_get_all_users()
    for user in users:
        for field in ("last_updated", "joined_at"):
            if user.get(field):
                user[field] = user[field].isoformat()

    json_bytes = json.dumps(users, ensure_ascii=False, indent=2).encode("utf-8")
    file = BytesIO(json_bytes)
    file.name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    await update.message.reply_document(document=file, caption="📦 Бэкап базы данных")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Использование: /ban USER_ID")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("USER_ID должен быть числом.")
        return
    db_ban_user(target_id)
    await update.message.reply_text(f"🚫 Пользователь {target_id} заблокирован.")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Использование: /unban USER_ID")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("USER_ID должен быть числом.")
        return
    db_unban_user(target_id)
    await update.message.reply_text(f"✅ Пользователь {target_id} разблокирован.")


async def callback_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(MAIN_MENU_TEXT, reply_markup=main_menu_keyboard())


async def callback_misc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⚙️ Прочее:", reply_markup=misc_keyboard())


async def callback_changelog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        build_changelog_text(),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("« Назад", callback_data="misc")]
        ]),
        parse_mode="HTML",
    )


async def _show_top(query, page: int) -> None:
    users = db_get_all_users_with_stats()
    text, total_pages = build_top_text(users, page)
    await query.edit_message_text(
        text,
        reply_markup=top_keyboard(page, total_pages),
        parse_mode="HTML",
    )


async def callback_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _show_top(query, page=0)


async def callback_top_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    await _show_top(query, page=page)


async def callback_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    tg_user = update.effective_user
    user = db_get_or_create_user(tg_user.id, tg_user.first_name)
    rank = db_get_user_rank(tg_user.id)
    today_pushups = db_get_today_pushups(tg_user.id)

    await query.edit_message_text(
        build_profile_text(user, rank, today_pushups),
        reply_markup=profile_keyboard(),
        parse_mode="HTML",
    )


async def callback_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    days = int(query.data.split("_")[1])
    tg_user = update.effective_user
    user = db_get_or_create_user(tg_user.id, tg_user.first_name)

    to_date = datetime.now(KYIV_TZ).date()
    from_date = to_date - timedelta(days=days - 1)
    history = db_get_history(tg_user.id, from_date, to_date)

    await query.edit_message_text(
        build_stats_text(user, from_date, to_date, history),
        reply_markup=stats_keyboard(),
        parse_mode="HTML",
    )


async def callback_stats_custom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "📅 Введи промежуток в формате:\n<code>01.05.2026 - 11.05.2026</code>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Отмена", callback_data="stats_7")]
        ]),
        parse_mode="HTML",
    )
    return WAITING_CUSTOM_RANGE


async def receive_custom_range(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    try:
        parts = [p.strip() for p in text.split("-", 1)]
        from_date = datetime.strptime(parts[0], "%d.%m.%Y").date()
        to_date = datetime.strptime(parts[1], "%d.%m.%Y").date()
        if from_date > to_date:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text(
            "Неверный формат. Попробуй так:\n<code>01.05.2026 - 11.05.2026</code>",
            parse_mode="HTML",
        )
        return WAITING_CUSTOM_RANGE

    max_days = 365
    if (to_date - from_date).days > max_days:
        await update.message.reply_text(f"Максимальный период — {max_days} дней.")
        return WAITING_CUSTOM_RANGE

    tg_user = update.effective_user
    user = db_get_or_create_user(tg_user.id, tg_user.first_name)
    history = db_get_history(tg_user.id, from_date, to_date)

    await update.message.reply_text(
        build_stats_text(user, from_date, to_date, history),
        reply_markup=stats_keyboard(),
        parse_mode="HTML",
    )
    return ConversationHandler.END


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

    if amount > MAX_PUSHUPS_PER_ACTION:
        await update.message.reply_text(
            f"❌ Максимум {MAX_PUSHUPS_PER_ACTION} отжиманий за один раз."
        )
        return WAITING_PUSHUPS_AMOUNT

    action = context.user_data.get("pushups_action")
    tg_user = update.effective_user

    if db_is_banned(tg_user.id):
        await update.message.reply_text("🚫 Ты заблокирован.")
        return ConversationHandler.END

    user = db_get_or_create_user(tg_user.id, tg_user.first_name)

    if action == "pushups_add":
        new_total = user["pushups"] + amount
        delta = amount
        verb = f"➕ Добавлено <b>{amount}</b> отжиманий"
    else:
        actual = min(amount, user["pushups"])
        new_total = user["pushups"] - actual
        delta = -actual
        verb = f"➖ Убрано <b>{actual}</b> отжиманий"

    db_update_pushups(tg_user.id, new_total, delta)

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

    await context.bot.send_message(
        chat_id=IDEA_CHAT_ID,
        text=f"💡 <b>Идея от {user['name']}</b>\n\n{update.message.text.strip()}",
        parse_mode="HTML",
    )
    await update.message.reply_text("✅ Идея отправлена в чат!", reply_markup=main_menu_keyboard())
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(MAIN_MENU_TEXT, reply_markup=main_menu_keyboard())
    return ConversationHandler.END


# ─── App Setup ───────────────────────────────────────────────────────────────

def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    pushups_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_pushups_action, pattern="^pushups_(add|sub)$")],
        states={WAITING_PUSHUPS_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pushups_amount)]},
        fallbacks=[CallbackQueryHandler(cancel_conversation, pattern="^main_menu$")],
    )

    name_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_change_name, pattern="^change_name$")],
        states={WAITING_NEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_name)]},
        fallbacks=[CallbackQueryHandler(cancel_conversation, pattern="^main_menu$")],
    )

    idea_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_suggest_idea, pattern="^suggest_idea$")],
        states={WAITING_IDEA_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_idea_text)]},
        fallbacks=[CallbackQueryHandler(cancel_conversation, pattern="^main_menu$")],
    )

    stats_custom_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_stats_custom, pattern="^stats_custom$")],
        states={WAITING_CUSTOM_RANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_range)]},
        fallbacks=[CallbackQueryHandler(callback_stats, pattern="^stats_7$")],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(pushups_conv)
    app.add_handler(name_conv)
    app.add_handler(idea_conv)
    app.add_handler(stats_custom_conv)
    app.add_handler(CallbackQueryHandler(callback_main_menu, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(callback_misc, pattern="^misc$"))
    app.add_handler(CallbackQueryHandler(callback_changelog, pattern="^changelog$"))
    app.add_handler(CallbackQueryHandler(callback_top, pattern="^top$"))
    app.add_handler(CallbackQueryHandler(callback_top_page, pattern=r'^top_page_\d+$'))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern="^noop$"))
    app.add_handler(CallbackQueryHandler(callback_profile, pattern="^profile$"))
    app.add_handler(CallbackQueryHandler(callback_stats, pattern="^stats_(7|30)$"))
    app.add_handler(CallbackQueryHandler(callback_edit_pushups, pattern="^edit_pushups$"))

    return app


def main() -> None:
    init_db()
    logger.info("Starting bot...")
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
