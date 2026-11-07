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
    ReplyKeyboardRemove,
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
DB_PATH = Path(__file__).with_name("leo.db")
MUTUAL_LIKE_MESSAGE = "У ВАС ВЗАИМНАЯ СИМПАТИЯ!"
PREMIUM_TEXT = "Premium стоит 1490 tg в неделю. Для подключения напишите @leo_admin."

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

    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)

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
                    name TEXT NOT NULL,
                    age INTEGER NOT NULL CHECK (age BETWEEN 18 AND 99),
                    gender TEXT NOT NULL,
                    city TEXT NOT NULL,
                    photo_file_id TEXT NOT NULL,
                    about TEXT NOT NULL,
                    looking_for TEXT NOT NULL,
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
                        telegram_id, name, age, gender, city,
                        photo_file_id, about, looking_for
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (telegram_id) DO UPDATE SET
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
                        profile["name"],
                        profile["age"],
                        profile["gender"],
                        profile["city"],
                        profile["photo_file_id"],
                        profile["about"],
                        profile["looking_for"],
                    ),
                )

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
                  AND NOT EXISTS (
                    SELECT 1
                    FROM likes
                    WHERE from_user_id = ?
                      AND to_user_id = candidate.telegram_id
                  )
                ORDER BY RANDOM()
                """,
                (viewer_id, viewer_id),
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


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Смотреть анкеты"],
            ["Моя анкета", "Premium"],
        ],
        resize_keyboard=True,
        is_persistent=True,
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
            [InlineKeyboardButton("Premium", callback_data="premium")],
        ]
    )


def profile_caption(profile: dict[str, Any]) -> str:
    name = html.escape(str(profile["name"]))
    city = html.escape(str(profile["city"]))
    about = html.escape(str(profile["about"]))
    return f"<b>{name}, {profile['age']}</b>\n{city}\n\n{about}"


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return ConversationHandler.END

    if get_database(context).get_user(user.id):
        await message.reply_text(
            "Вы уже зарегистрированы. Выберите действие в меню.",
            reply_markup=main_menu(),
        )
        return ConversationHandler.END

    context.user_data["registration"] = {}
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
        return ConversationHandler.END

    if query.message:
        await query.message.reply_text(
            "Как вас зовут?",
            reply_markup=ReplyKeyboardRemove(),
        )
    return NAME


async def edit_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    context.user_data["registration"] = {}
    if query.message:
        await query.message.reply_text(
            "Обновим вашу анкету. Как вас зовут?",
            reply_markup=ReplyKeyboardRemove(),
        )
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

    registration["looking_for"] = LOOKING_FOR_OPTIONS[key]
    get_database(context).save_profile(user.id, registration)
    context.user_data.pop("registration", None)
    if query.message:
        await query.message.reply_text(
            "Анкета сохранена. Выберите действие в меню.",
            reply_markup=main_menu(),
        )
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
    if not get_database(context).get_user(user.id):
        await context.bot.send_message(
            chat_id=chat.id,
            text="Сначала создайте анкету командой /start.",
            reply_markup=main_menu(),
        )
        return
    await send_next_profile(chat.id, user.id, context)


async def show_my_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    profile = get_database(context).get_user(user.id)
    if not profile:
        await context.bot.send_message(
            chat_id=chat.id,
            text="Сначала создайте анкету командой /start.",
            reply_markup=main_menu(),
        )
        return

    await context.bot.send_photo(
        chat_id=chat.id,
        photo=profile["photo_file_id"],
        caption=profile_caption(profile),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Изменить анкету", callback_data="profile:edit")]]
        ),
    )


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
        return

    chat = update.effective_chat
    if chat:
        await context.bot.send_message(
            chat_id=chat.id,
            text=PREMIUM_TEXT,
            reply_markup=main_menu(),
        )


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
    viewer = database.get_user(user.id)
    candidate = database.get_user(target_id)
    if (
        action not in {"like", "dislike"}
        or not viewer
        or not candidate
        or target_id == user.id
    ):
        await query.answer("Анкета недоступна.", show_alert=True)
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
        for recipient_id in (user.id, target_id):
            try:
                await context.bot.send_message(
                    chat_id=recipient_id,
                    text=MUTUAL_LIKE_MESSAGE,
                )
            except TelegramError:
                logger.info("Could not deliver a match notification.")

    chat = update.effective_chat
    if chat:
        await send_next_profile(chat.id, user.id, context)


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
        ],
        states={
            CONSENT: [
                CallbackQueryHandler(accept_registration, pattern=r"^consent:")
            ],
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_age)],
            GENDER: [CallbackQueryHandler(receive_gender, pattern=r"^gender:")],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_city)],
            PHOTO: [
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(filters.ALL & ~filters.COMMAND, remind_photo),
            ],
            ABOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_about)],
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
        MessageHandler(filters.Regex(r"^Смотреть анкеты$"), browse_profiles)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^Моя анкета$"), show_my_profile)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^Premium$"), show_premium)
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
