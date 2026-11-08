"""Telegram dating bot with SQLite-backed profiles and likes."""

from __future__ import annotations

import html
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("leo_bot")

CONSENT, NAME, AGE, GENDER, CITY, PHOTO, ABOUT, LOOKING_FOR = range(8)
DB_PATH = Path(__file__).with_name("bot.db")
MUTUAL_LIKE_MESSAGE = (
    "У ВАС ВЗАИМНАЯ СИМПАТИЯ! ❤️\n"
    "Вы понравились друг другу!"
)
USERNAME_REQUIRED_TEXT = (
    "Для регистрации и обмена контактами при взаимной симпатии добавьте "
    "username в Telegram: Настройки → Имя пользователя. Затем нажмите /start "
    "ещё раз."
)
PREMIUM_TEXT = "Premium стоит 2000 тенге в неделю. Для подключения напишите @leoqazaq."
HOME_MENU_TEXT = (
    "Подождем пока кто-то увидит твою анкету\n\n"
    "1. Смотреть анкеты.\n"
    "2. Моя анкета.\n"
    "3. Я больше не хочу никого искать.\n"
    "4. Изменить анкету.\n"
    "***\n"
    "5. Активируй Premium — будь в топе ⭐️."
)
MENU_BUTTON_PATTERN = r"^(1|2|3|4|5)$"
MENU_BUTTON_FILTER = filters.Regex(MENU_BUTTON_PATTERN)
TEXT_INPUT_FILTER = filters.TEXT & ~filters.COMMAND & ~MENU_BUTTON_FILTER

GENDERS = {
    "man": "Мужчина",
    "woman": "Женщина",
    "other": "Другое",
}
LOOKING_FOR_OPTIONS = {
    "men": "Мужчин",
    "women": "Женщин",
    "all": "Всех",
}
REPORT_REASONS = {
    "spam": "Спам или мошенничество",
    "harassment": "Оскорбления или домогательства",
    "inappropriate": "Неприемлемый контент",
    "fake_profile": "Поддельная анкета",
    "other": "Другая причина",
}


def accepts_gender(preference: str, gender: str) -> bool:
    """Return whether a gender fits a saved looking-for preference."""
    if preference == "Всех":
        return True
    return (preference, gender) in {
        ("Мужчин", "Мужчина"),
        ("Женщин", "Женщина"),
    }


class Database:
    """Small SQLite data layer for profiles and one-time profile decisions."""

    def __init__(
        self,
        path: Path | str = DB_PATH,
        legacy_path: Path | str | None = None,
    ) -> None:
        self.path = Path(path)
        self.legacy_path = (
            Path(legacy_path)
            if legacy_path is not None
            else self.path.with_name("leo.db")
        )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    name TEXT NOT NULL,
                    age INTEGER NOT NULL CHECK (age BETWEEN 18 AND 99),
                    gender TEXT NOT NULL,
                    city TEXT NOT NULL,
                    photo_file_id TEXT NOT NULL,
                    about TEXT NOT NULL,
                    looking_for TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1
                        CHECK (is_active IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS likes (
                    from_user_id INTEGER NOT NULL,
                    to_user_id INTEGER NOT NULL,
                    is_like INTEGER NOT NULL CHECK (is_like IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (from_user_id, to_user_id),
                    CHECK (from_user_id != to_user_id),
                    FOREIGN KEY (from_user_id) REFERENCES users (telegram_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (to_user_id) REFERENCES users (telegram_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS matches (
                    first_user_id INTEGER NOT NULL,
                    second_user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (first_user_id, second_user_id),
                    CHECK (first_user_id < second_user_id),
                    FOREIGN KEY (first_user_id) REFERENCES users (telegram_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (second_user_id) REFERENCES users (telegram_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS blocks (
                    blocker_id INTEGER NOT NULL,
                    blocked_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (blocker_id, blocked_id),
                    CHECK (blocker_id != blocked_id),
                    FOREIGN KEY (blocker_id) REFERENCES users (telegram_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (blocked_id) REFERENCES users (telegram_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS reports (
                    reporter_id INTEGER NOT NULL,
                    reported_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (reporter_id, reported_id),
                    CHECK (reporter_id != reported_id),
                    FOREIGN KEY (reporter_id) REFERENCES users (telegram_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (reported_id) REFERENCES users (telegram_id)
                        ON DELETE CASCADE
                );
                """
            )
            user_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(users)")
            }
            if "is_active" not in user_columns:
                connection.execute(
                    """
                    ALTER TABLE users
                    ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1
                        CHECK (is_active IN (0, 1))
                    """
                )
            if "username" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN username TEXT"
                )
            self._migrate_legacy_database(connection)
            self._backfill_mutual_likes(connection)

    def _migrate_legacy_database(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        if (
            not self.legacy_path.exists()
            or self.legacy_path.resolve() == self.path.resolve()
        ):
            return

        connection.execute(
            "ATTACH DATABASE ? AS legacy",
            (str(self.legacy_path),),
        )
        try:
            legacy_tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM legacy.sqlite_master WHERE type = 'table'"
                )
            }
            if "users" not in legacy_tables:
                return

            legacy_user_columns = {
                row["name"]
                for row in connection.execute("PRAGMA legacy.table_info(users)")
            }
            required_user_columns = {
                "telegram_id",
                "name",
                "age",
                "gender",
                "city",
                "photo_file_id",
                "about",
                "looking_for",
            }
            if not required_user_columns.issubset(legacy_user_columns):
                raise RuntimeError(
                    "Cannot migrate leo.db: required profile columns are missing."
                )

            active_expression = (
                "is_active" if "is_active" in legacy_user_columns else "1"
            )
            username_expression = (
                "username" if "username" in legacy_user_columns else "NULL"
            )
            created_expression = (
                "created_at"
                if "created_at" in legacy_user_columns
                else "CURRENT_TIMESTAMP"
            )
            updated_expression = (
                "updated_at"
                if "updated_at" in legacy_user_columns
                else "CURRENT_TIMESTAMP"
            )

            with connection:
                connection.execute(
                    f"""
                    INSERT OR IGNORE INTO main.users (
                        telegram_id, username, name, age, gender, city,
                        photo_file_id, about, looking_for, is_active,
                        created_at, updated_at
                    )
                    SELECT telegram_id, {username_expression}, name, age,
                           gender, city, photo_file_id, about, looking_for,
                           {active_expression}, {created_expression},
                           {updated_expression}
                    FROM legacy.users
                    """
                )

                if "likes" in legacy_tables:
                    legacy_like_columns = {
                        row["name"]
                        for row in connection.execute("PRAGMA legacy.table_info(likes)")
                    }
                    required_like_columns = {
                        "from_user_id",
                        "to_user_id",
                        "is_like",
                    }
                    if not required_like_columns.issubset(legacy_like_columns):
                        raise RuntimeError(
                            "Cannot migrate leo.db: required like columns are missing."
                        )
                    like_created_expression = (
                        "created_at"
                        if "created_at" in legacy_like_columns
                        else "CURRENT_TIMESTAMP"
                    )
                    connection.execute(
                        f"""
                        INSERT OR IGNORE INTO main.likes (
                            from_user_id, to_user_id, is_like, created_at
                        )
                        SELECT from_user_id, to_user_id, is_like,
                               {like_created_expression}
                        FROM legacy.likes
                        """
                    )
        finally:
            connection.execute("DETACH DATABASE legacy")

    @staticmethod
    def _backfill_mutual_likes(connection: sqlite3.Connection) -> None:
        with connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO matches (first_user_id, second_user_id)
                SELECT outgoing.from_user_id, outgoing.to_user_id
                FROM likes AS outgoing
                JOIN likes AS incoming
                  ON incoming.from_user_id = outgoing.to_user_id
                 AND incoming.to_user_id = outgoing.from_user_id
                WHERE outgoing.is_like = 1
                  AND incoming.is_like = 1
                  AND outgoing.from_user_id < outgoing.to_user_id
                """
            )

    def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_profile(self, telegram_id: int, profile: dict[str, Any]) -> None:
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO users (
                        telegram_id, username, name, age, gender, city,
                        photo_file_id, about, looking_for
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (telegram_id) DO UPDATE SET
                        username = excluded.username,
                        name = excluded.name,
                        age = excluded.age,
                        gender = excluded.gender,
                        city = excluded.city,
                        photo_file_id = excluded.photo_file_id,
                        about = excluded.about,
                        looking_for = excluded.looking_for,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        telegram_id,
                        profile.get("username"),
                        profile["name"],
                        profile["age"],
                        profile["gender"],
                        profile["city"],
                        profile["photo_file_id"],
                        profile["about"],
                        profile["looking_for"],
                    ),
                )

    def update_username(
        self,
        telegram_id: int,
        username: str | None,
    ) -> bool:
        normalized = username.strip().removeprefix("@") if username else None
        if normalized == "":
            normalized = None
        with closing(self.connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE users
                    SET username = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE telegram_id = ?
                    """,
                    (normalized, telegram_id),
                )
        return cursor.rowcount == 1

    def set_search_active(self, telegram_id: int, is_active: bool) -> bool:
        with closing(self.connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE users
                    SET is_active = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE telegram_id = ?
                    """,
                    (int(is_active), telegram_id),
                )
        return cursor.rowcount == 1

    def record_action(
        self,
        from_user_id: int,
        to_user_id: int,
        is_like: bool,
    ) -> bool:
        """Record a first like/dislike; return False for a stale duplicate tap."""
        with closing(self.connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO likes (from_user_id, to_user_id, is_like)
                    VALUES (?, ?, ?)
                    ON CONFLICT (from_user_id, to_user_id) DO NOTHING
                    """,
                    (from_user_id, to_user_id, int(is_like)),
                )
        return cursor.rowcount == 1

    def has_mutual_like(self, first_user_id: int, second_user_id: int) -> bool:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM likes AS outgoing
                    JOIN likes AS incoming
                      ON incoming.from_user_id = outgoing.to_user_id
                     AND incoming.to_user_id = outgoing.from_user_id
                    WHERE outgoing.from_user_id = ?
                      AND outgoing.to_user_id = ?
                      AND outgoing.is_like = 1
                      AND incoming.is_like = 1
                )
                """,
                (first_user_id, second_user_id),
            ).fetchone()
        return bool(row[0])

    def record_match(self, first_user_id: int, second_user_id: int) -> bool:
        first_id, second_id = sorted((first_user_id, second_user_id))
        with closing(self.connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO matches (first_user_id, second_user_id)
                    VALUES (?, ?)
                    ON CONFLICT (first_user_id, second_user_id) DO NOTHING
                    """,
                    (first_id, second_id),
                )
        return cursor.rowcount == 1

    def has_match(self, first_user_id: int, second_user_id: int) -> bool:
        first_id, second_id = sorted((first_user_id, second_user_id))
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM matches
                    WHERE first_user_id = ? AND second_user_id = ?
                )
                """,
                (first_id, second_id),
            ).fetchone()
        return bool(row[0])

    def block_user(self, blocker_id: int, blocked_id: int) -> bool:
        """Block a profile; return False when it was already blocked."""
        with closing(self.connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO blocks (blocker_id, blocked_id)
                    VALUES (?, ?)
                    ON CONFLICT (blocker_id, blocked_id) DO NOTHING
                    """,
                    (blocker_id, blocked_id),
                )
        return cursor.rowcount == 1

    def record_report(
        self,
        reporter_id: int,
        reported_id: int,
        reason: str,
    ) -> bool:
        """Save a report for review without contacting the reported profile."""
        with closing(self.connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO reports (reporter_id, reported_id, reason)
                    VALUES (?, ?, ?)
                    ON CONFLICT (reporter_id, reported_id) DO NOTHING
                    """,
                    (reporter_id, reported_id, reason),
                )
        return cursor.rowcount == 1

    def reports_for_review(self) -> list[dict[str, Any]]:
        """Return stored reports without exposing them to reported users."""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM reports ORDER BY created_at, reporter_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def is_blocked_between(self, first_user_id: int, second_user_id: int) -> bool:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM blocks
                    WHERE (blocker_id = ? AND blocked_id = ?)
                       OR (blocker_id = ? AND blocked_id = ?)
                )
                """,
                (first_user_id, second_user_id, second_user_id, first_user_id),
            ).fetchone()
        return bool(row[0])

    def next_profile(
        self,
        viewer_id: int,
        viewer_profile: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Find a random unseen profile compatible in both directions."""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT candidate.*
                FROM users AS candidate
                WHERE candidate.telegram_id != ?
                  AND candidate.is_active = 1
                  AND candidate.username IS NOT NULL
                  AND TRIM(candidate.username) != ''
                  AND NOT EXISTS (
                    SELECT 1
                    FROM likes
                    WHERE from_user_id = ?
                      AND to_user_id = candidate.telegram_id
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM blocks
                    WHERE (blocker_id = ? AND blocked_id = candidate.telegram_id)
                       OR (blocker_id = candidate.telegram_id AND blocked_id = ?)
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM reports
                    WHERE reporter_id = ? AND reported_id = candidate.telegram_id
                  )
                ORDER BY RANDOM()
                """,
                (viewer_id, viewer_id, viewer_id, viewer_id, viewer_id),
            ).fetchall()

        for row in rows:
            candidate = dict(row)
            if (
                accepts_gender(viewer_profile["looking_for"], candidate["gender"])
                and accepts_gender(candidate["looking_for"], viewer_profile["gender"])
            ):
                return candidate
        return None


def get_database(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["database"]


def sync_telegram_username(database: Database, user: Any) -> bool:
    username = getattr(user, "username", None)
    database.update_username(user.id, username)
    return bool(username and username.strip())


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["1", "2", "3", "4", "5"],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
    )


def consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Начать", callback_data="consent:yes"),
                InlineKeyboardButton("Отмена", callback_data="consent:no"),
            ]
        ]
    )


def gender_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=f"gender:{key}")
                for key, label in GENDERS.items()
            ]
        ]
    )


def looking_for_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=f"looking:{key}")
                for key, label in LOOKING_FOR_OPTIONS.items()
            ]
        ]
    )


def profile_keyboard(profile_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("❤️ Like", callback_data=f"like:{profile_id}"),
                InlineKeyboardButton(
                    "👎 Dislike",
                    callback_data=f"dislike:{profile_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Заблокировать",
                    callback_data=f"block:{profile_id}",
                ),
                InlineKeyboardButton(
                    "Пожаловаться",
                    callback_data=f"report:{profile_id}",
                ),
            ],
        ]
    )


def report_reason_keyboard(profile_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    reason,
                    callback_data=f"report_reason:{profile_id}:{key}",
                )
            ]
            for key, reason in REPORT_REASONS.items()
        ]
        + [
            [
                InlineKeyboardButton(
                    "Отмена",
                    callback_data=f"report_cancel:{profile_id}",
                )
            ]
        ]
    )


def profile_caption(profile: dict[str, Any]) -> str:
    name = html.escape(str(profile["name"]))
    city = html.escape(str(profile["city"]))
    about = html.escape(str(profile["about"]))
    return f"<b>{name}, {profile['age']}</b>\n{city}\n\n{about}"


def own_profile_text(profile: dict[str, Any]) -> str:
    return (
        f"{profile['name']}, {profile['age']}, {profile['city']} — "
        f"{profile['about']}"
    )


def build_mutual_like_message(profile: dict[str, Any]) -> str:
    username = str(profile.get("username") or "").strip().removeprefix("@")
    if not username:
        raise ValueError("Mutual-like contacts require a Telegram username.")
    return (
        f"{MUTUAL_LIKE_MESSAGE}\n\n"
        f"Анкета: {profile['name']}, {profile['age']}, {profile['city']}\n"
        f"Юзернейм: @{username}\n"
        f"Ссылка: https://t.me/{username}\n"
        "Напишите друг другу!"
    )


async def send_home_menu(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await context.bot.send_message(
        chat_id=chat_id,
        text=HOME_MENU_TEXT,
        reply_markup=main_menu(),
    )


async def send_username_required(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await context.bot.send_message(
        chat_id=chat_id,
        text=USERNAME_REQUIRED_TEXT,
        reply_markup=main_menu(),
    )
    await send_home_menu(chat_id, context)


async def send_own_profile(
    chat_id: int,
    profile: dict[str, Any],
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=profile["photo_file_id"],
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=own_profile_text(profile),
        reply_markup=main_menu(),
    )
    await send_home_menu(chat_id, context)


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return ConversationHandler.END

    database = get_database(context)
    if not sync_telegram_username(database, user):
        await send_username_required(message.chat_id, context)
        return ConversationHandler.END
    profile = database.get_user(user.id)
    if profile:
        await send_own_profile(message.chat_id, profile, context)
        return ConversationHandler.END

    context.user_data["registration"] = {}
    await send_home_menu(message.chat_id, context)
    await message.reply_text(
        "Перед регистрацией: анкета и фотография будут показаны другим "
        "подходящим пользователям. Бот предназначен только для взрослых "
        "18 лет и старше. Нажимая «Начать», вы соглашаетесь на показ анкеты.",
        reply_markup=consent_keyboard(),
    )
    return CONSENT


async def accept_registration(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    if query.data == "consent:no":
        context.user_data.pop("registration", None)
        if query.message:
            await query.message.reply_text(
                "Регистрация отменена.",
                reply_markup=main_menu(),
            )
            await send_home_menu(query.message.chat_id, context)
        return ConversationHandler.END

    if query.message:
        await query.message.reply_text(
            "Как вас зовут?",
            reply_markup=main_menu(),
        )
        await send_home_menu(query.message.chat_id, context)
    return NAME


async def edit_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    message = update.effective_message
    if query:
        await query.answer()
        message = query.message
    if not message:
        return ConversationHandler.END
    user = update.effective_user
    if not user:
        return ConversationHandler.END
    database = get_database(context)
    if not sync_telegram_username(database, user):
        await send_username_required(message.chat_id, context)
        return ConversationHandler.END
    context.user_data["registration"] = {}
    await message.reply_text(
        "Обновим вашу анкету. Как вас зовут?",
        reply_markup=main_menu(),
    )
    await send_home_menu(message.chat_id, context)
    return NAME


async def receive_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    text = update.effective_message.text.strip() if update.effective_message else ""
    if not 2 <= len(text) <= 50:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Имя должно содержать от 2 до 50 символов. Попробуйте ещё раз."
            )
        return NAME
    context.user_data["registration"]["name"] = text
    await update.effective_message.reply_text("Сколько вам лет? (от 18 до 99)")
    return AGE


async def receive_age(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    text = update.effective_message.text.strip() if update.effective_message else ""
    try:
        age = int(text)
    except ValueError:
        age = 0
    if not 18 <= age <= 99:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Укажите возраст от 18 до 99 лет."
            )
        return AGE
    context.user_data["registration"]["age"] = age
    await update.effective_message.reply_text(
        "Выберите ваш пол:",
        reply_markup=gender_keyboard(),
    )
    return GENDER


async def receive_gender(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    if not query:
        return GENDER
    await query.answer()
    key = query.data.partition(":")[2]
    if key not in GENDERS:
        if query.message:
            await query.message.reply_text("Выберите вариант кнопкой ниже.")
            await query.message.reply_text(
                "Ваш пол:",
                reply_markup=gender_keyboard(),
            )
        return GENDER
    context.user_data["registration"]["gender"] = GENDERS[key]
    if query.message:
        await query.message.reply_text("В каком городе вы живёте?")
    return CITY


async def receive_city(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    text = update.effective_message.text.strip() if update.effective_message else ""
    if not 2 <= len(text) <= 60:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Название города должно содержать от 2 до 60 символов."
            )
        return CITY
    context.user_data["registration"]["city"] = text
    await update.effective_message.reply_text(
        "Пришлите фотографию анкеты как фото, не как файл."
    )
    return PHOTO


async def receive_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    message = update.effective_message
    if not message or not message.photo:
        return PHOTO
    context.user_data["registration"]["photo_file_id"] = message.photo[-1].file_id
    await message.reply_text("Напишите пару слов о себе (до 500 символов).")
    return ABOUT


async def remind_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if update.effective_message:
        await update.effective_message.reply_text(
            "Для анкеты нужна фотография. Отправьте её как фото, не как файл."
        )
    return PHOTO


async def receive_about(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    text = update.effective_message.text.strip() if update.effective_message else ""
    if not text or len(text) > 500:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Описание должно содержать от 1 до 500 символов."
            )
        return ABOUT
    context.user_data["registration"]["about"] = text
    await update.effective_message.reply_text(
        "Кого вы ищете?",
        reply_markup=looking_for_keyboard(),
    )
    return LOOKING_FOR


async def receive_looking_for(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    if not query:
        return LOOKING_FOR
    await query.answer()
    key = query.data.partition(":")[2]
    if key not in LOOKING_FOR_OPTIONS:
        if query.message:
            await query.message.reply_text(
                "Выберите, кого вы ищете:",
                reply_markup=looking_for_keyboard(),
            )
        return LOOKING_FOR

    user = update.effective_user
    registration = context.user_data.get("registration", {})
    required_fields = {
        "name",
        "age",
        "gender",
        "city",
        "photo_file_id",
        "about",
    }
    if not user or not required_fields.issubset(registration):
        if query.message:
            await query.message.reply_text(
                "Не удалось сохранить анкету. Начните заново командой /start."
            )
        context.user_data.pop("registration", None)
        return ConversationHandler.END

    database = get_database(context)
    if not sync_telegram_username(database, user):
        context.user_data.pop("registration", None)
        if query.message:
            await send_username_required(query.message.chat_id, context)
        return ConversationHandler.END

    registration["username"] = user.username.strip().removeprefix("@")
    registration["looking_for"] = LOOKING_FOR_OPTIONS[key]
    database.save_profile(user.id, registration)
    context.user_data.pop("registration", None)
    if query.message:
        await query.message.reply_text(
            "Анкета сохранена. Выберите действие в меню.",
            reply_markup=main_menu(),
        )
        await send_home_menu(query.message.chat_id, context)
    return ConversationHandler.END


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    context.user_data.pop("registration", None)
    if update.effective_message:
        await update.effective_message.reply_text(
            "Регистрация отменена.",
            reply_markup=main_menu(),
        )
        await send_home_menu(update.effective_message.chat_id, context)
    return ConversationHandler.END


async def send_next_profile(
    chat_id: int,
    viewer_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    database = get_database(context)
    viewer = database.get_user(viewer_id)
    candidate = database.next_profile(viewer_id, viewer) if viewer else None
    if not candidate:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Пока нет новых подходящих анкет. Загляните позже.",
            reply_markup=main_menu(),
        )
        return

    await context.bot.send_photo(
        chat_id=chat_id,
        photo=candidate["photo_file_id"],
        caption=profile_caption(candidate),
        parse_mode=ParseMode.HTML,
        reply_markup=profile_keyboard(candidate["telegram_id"]),
    )


async def browse_profiles(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    database = get_database(context)
    if not sync_telegram_username(database, user):
        await send_username_required(chat.id, context)
        return
    viewer = database.get_user(user.id)
    if not viewer:
        await context.bot.send_message(
            chat_id=chat.id,
            text="Сначала создайте анкету командой /start.",
            reply_markup=main_menu(),
        )
        await send_home_menu(chat.id, context)
        return
    if not viewer["is_active"]:
        database.set_search_active(user.id, True)
        await context.bot.send_message(
            chat_id=chat.id,
            text="Поиск включён. Ваша анкета снова показывается другим пользователям.",
            reply_markup=main_menu(),
        )
    await send_next_profile(chat.id, user.id, context)
    await send_home_menu(chat.id, context)


async def show_my_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    database = get_database(context)
    sync_telegram_username(database, user)
    profile = database.get_user(user.id)
    if not profile:
        await context.bot.send_message(
            chat_id=chat.id,
            text="Сначала создайте анкету командой /start.",
            reply_markup=main_menu(),
        )
        await send_home_menu(chat.id, context)
        return
    await send_own_profile(chat.id, profile, context)


async def stop_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    database = get_database(context)
    profile = database.get_user(user.id)
    if not profile:
        await context.bot.send_message(
            chat_id=chat.id,
            text="Сначала создайте анкету командой /start.",
            reply_markup=main_menu(),
        )
        await send_home_menu(chat.id, context)
        return

    if profile["is_active"]:
        database.set_search_active(user.id, False)
        text = (
            "Поиск выключен. Ваша анкета скрыта от других пользователей. "
            "Чтобы вернуться, нажмите 1."
        )
    else:
        text = (
            "Поиск уже выключен. Чтобы вернуться и снова показывать анкету, "
            "нажмите 1."
        )
    await context.bot.send_message(
        chat_id=chat.id,
        text=text,
        reply_markup=main_menu(),
    )
    await send_home_menu(chat.id, context)


async def show_premium(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query:
        await query.answer()
        if query.message:
            await query.message.reply_text(
                PREMIUM_TEXT,
                reply_markup=main_menu(),
            )
            await send_home_menu(query.message.chat_id, context)
        return

    chat = update.effective_chat
    if chat:
        await context.bot.send_message(
            chat_id=chat.id,
            text=PREMIUM_TEXT,
            reply_markup=main_menu(),
        )
        await send_home_menu(chat.id, context)


async def block_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not query.data:
        return

    try:
        target_id = int(query.data.partition(":")[2])
    except ValueError:
        await query.answer("Анкета недоступна.", show_alert=True)
        return

    database = get_database(context)
    if (
        not database.get_user(user.id)
        or not database.get_user(target_id)
        or target_id == user.id
    ):
        await query.answer("Анкета недоступна.", show_alert=True)
        return

    added = database.block_user(user.id, target_id)
    await query.answer(
        "Анкета заблокирована." if added else "Эта анкета уже заблокирована."
    )
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    chat = update.effective_chat
    if chat:
        await send_next_profile(chat.id, user.id, context)
        await send_home_menu(chat.id, context)


async def begin_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not query.data:
        return

    try:
        target_id = int(query.data.partition(":")[2])
    except ValueError:
        await query.answer("Анкета недоступна.", show_alert=True)
        return

    database = get_database(context)
    if (
        not database.get_user(user.id)
        or not database.get_user(target_id)
        or target_id == user.id
    ):
        await query.answer("Анкета недоступна.", show_alert=True)
        return

    await query.answer()
    await query.edit_message_reply_markup(
        reply_markup=report_reason_keyboard(target_id)
    )
    chat = update.effective_chat
    if chat:
        await send_home_menu(chat.id, context)


async def cancel_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not query:
        return
    try:
        target_id = int(query.data.partition(":")[2]) if query.data else 0
    except ValueError:
        target_id = 0
    await query.answer("Отправка жалобы отменена.")
    try:
        await query.edit_message_reply_markup(
            reply_markup=profile_keyboard(target_id) if target_id else None
        )
    except TelegramError:
        pass
    chat = update.effective_chat
    if chat:
        await send_home_menu(chat.id, context)


async def submit_report(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not query.data:
        return

    _, _, payload = query.data.partition(":")
    target_text, separator, reason_key = payload.partition(":")
    try:
        target_id = int(target_text)
    except ValueError:
        await query.answer("Анкета недоступна.", show_alert=True)
        return

    database = get_database(context)
    reason = REPORT_REASONS.get(reason_key) if separator else None
    if (
        not reason
        or not database.get_user(user.id)
        or not database.get_user(target_id)
        or target_id == user.id
    ):
        await query.answer("Анкета недоступна.", show_alert=True)
        return

    added = database.record_report(user.id, target_id, reason)
    await query.answer(
        "Жалоба отправлена на проверку."
        if added
        else "Вы уже отправляли жалобу на эту анкету."
    )
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    chat = update.effective_chat
    if chat:
        await send_next_profile(chat.id, user.id, context)
        await send_home_menu(chat.id, context)


async def profile_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not query.data:
        return

    action, _, target_text = query.data.partition(":")
    try:
        target_id = int(target_text)
    except ValueError:
        await query.answer("Анкета недоступна.", show_alert=True)
        return

    database = get_database(context)
    if not sync_telegram_username(database, user):
        await query.answer(
            "Добавьте username в настройках Telegram, чтобы использовать лайки.",
            show_alert=True,
        )
        chat = update.effective_chat
        if chat:
            await send_username_required(chat.id, context)
        return

    viewer = database.get_user(user.id)
    candidate = database.get_user(target_id)
    if (
        action not in {"like", "dislike"}
        or not viewer
        or not candidate
        or target_id == user.id
        or database.is_blocked_between(user.id, target_id)
    ):
        await query.answer("Анкета недоступна.", show_alert=True)
        return

    if action == "like" and not candidate.get("username"):
        await query.answer(
            "У этой анкеты пока нет username, поэтому контакт не получится передать.",
            show_alert=True,
        )
        return

    added = database.record_action(
        from_user_id=user.id,
        to_user_id=target_id,
        is_like=action == "like",
    )
    if not added:
        await query.answer("Вы уже оценили эту анкету.")
        return

    await query.answer("Лайк отправлен." if action == "like" else "Анкета пропущена.")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    if action == "like" and database.has_mutual_like(user.id, target_id):
        if database.record_match(user.id, target_id):
            recipients = (
                (user.id, candidate),
                (target_id, viewer),
            )
            for recipient_id, other_profile in recipients:
                try:
                    await context.bot.send_message(
                        chat_id=recipient_id,
                        text=build_mutual_like_message(other_profile),
                    )
                except TelegramError:
                    logger.info("Could not deliver a match notification.")
                try:
                    await send_home_menu(recipient_id, context)
                except TelegramError:
                    logger.info("Could not deliver the menu after a match.")

        return

    chat = update.effective_chat
    if chat:
        await send_next_profile(chat.id, user.id, context)
        await send_home_menu(chat.id, context)


async def on_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    error_name = type(context.error).__name__ if context.error else "UnknownError"
    logger.error("Bot update failed (%s).", error_name)


def build_application(token: str) -> Application:
    application = Application.builder().token(token).build()
    application.bot_data["database"] = Database()
    application.bot_data["database"].initialize()

    registration = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(edit_profile, pattern=r"^profile:edit$"),
            MessageHandler(filters.Regex(r"^4$"), edit_profile),
        ],
        states={
            CONSENT: [
                CallbackQueryHandler(accept_registration, pattern=r"^consent:")
            ],
            NAME: [MessageHandler(TEXT_INPUT_FILTER, receive_name)],
            AGE: [MessageHandler(TEXT_INPUT_FILTER, receive_age)],
            GENDER: [CallbackQueryHandler(receive_gender, pattern=r"^gender:")],
            CITY: [MessageHandler(TEXT_INPUT_FILTER, receive_city)],
            PHOTO: [
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(
                    filters.ALL & ~filters.COMMAND & ~MENU_BUTTON_FILTER,
                    remind_photo,
                ),
            ],
            ABOUT: [MessageHandler(TEXT_INPUT_FILTER, receive_about)],
            LOOKING_FOR: [
                CallbackQueryHandler(receive_looking_for, pattern=r"^looking:")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_chat=True,
        per_user=True,
    )
    application.add_handler(registration)
    application.add_handler(CommandHandler("premium", show_premium))
    application.add_handler(
        MessageHandler(filters.Regex(r"^1$"), browse_profiles)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^2$"), show_my_profile)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^3$"), stop_search)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^5$"), show_premium)
    )
    application.add_handler(
        CallbackQueryHandler(block_profile, pattern=r"^block:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(begin_report, pattern=r"^report:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(submit_report, pattern=r"^report_reason:\d+:[a-z_]+$")
    )
    application.add_handler(
        CallbackQueryHandler(cancel_report, pattern=r"^report_cancel:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(profile_action, pattern=r"^(like|dislike):\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(show_premium, pattern=r"^premium$")
    )
    application.add_error_handler(on_error)
    return application


def main() -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit(
            "TELEGRAM_TOKEN is not set. Add it to the project's Secrets."
        )

    logger.info("Starting the Telegram dating bot with long polling.")
    build_application(token).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
