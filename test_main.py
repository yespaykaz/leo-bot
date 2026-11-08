import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import ReplyKeyboardMarkup
from telegram.ext import ConversationHandler
from main import (
    HOME_MENU_TEXT,
    Database,
    accepts_gender,
    main_menu,
    start,
    submit_report,
)


def profile(
    name: str,
    gender: str,
    looking_for: str,
) -> dict[str, object]:
    return {
        "name": name,
        "age": 25,
        "gender": gender,
        "city": "Алматы",
        "photo_file_id": "telegram-photo-id",
        "about": "О себе",
        "looking_for": looking_for,
    }


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_profile_round_trip(self) -> None:
        saved = profile("Анна", "Женщина", "Мужчин")
        self.database.save_profile(101, saved)

        loaded = self.database.get_user(101)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["name"], "Анна")
        self.assertEqual(loaded["looking_for"], "Мужчин")
        self.assertEqual(loaded["is_active"], 1)

    def test_only_mutual_likes_match_and_duplicate_actions_are_ignored(self) -> None:
        self.database.save_profile(101, profile("Анна", "Женщина", "Мужчин"))
        self.database.save_profile(202, profile("Илья", "Мужчина", "Женщин"))

        self.assertTrue(self.database.record_action(101, 202, True))
        self.assertFalse(self.database.has_mutual_like(101, 202))
        self.assertTrue(self.database.record_action(202, 101, True))
        self.assertTrue(self.database.has_mutual_like(101, 202))
        self.assertFalse(self.database.record_action(101, 202, False))

    def test_disliked_profiles_are_not_shown_again(self) -> None:
        viewer = profile("Анна", "Женщина", "Мужчин")
        self.database.save_profile(101, viewer)
        self.database.save_profile(202, profile("Илья", "Мужчина", "Женщин"))

        self.assertEqual(
            self.database.next_profile(101, viewer)["telegram_id"],
            202,
        )
        self.database.record_action(101, 202, False)
        self.assertIsNone(self.database.next_profile(101, viewer))

    def test_blocked_profiles_are_not_shown_to_either_user(self) -> None:
        viewer = profile("Анна", "Женщина", "Мужчин")
        candidate = profile("Илья", "Мужчина", "Женщин")
        self.database.save_profile(101, viewer)
        self.database.save_profile(202, candidate)

        self.assertTrue(self.database.block_user(101, 202))
        self.assertFalse(self.database.block_user(101, 202))
        self.assertTrue(self.database.is_blocked_between(101, 202))
        self.assertTrue(self.database.is_blocked_between(202, 101))
        self.assertIsNone(self.database.next_profile(101, viewer))
        self.assertIsNone(self.database.next_profile(202, candidate))

    def test_reports_are_saved_once_without_hiding_the_reported_profile(self) -> None:
        viewer = profile("Анна", "Женщина", "Мужчин")
        candidate = profile("Илья", "Мужчина", "Женщин")
        self.database.save_profile(101, viewer)
        self.database.save_profile(202, candidate)

        self.assertTrue(
            self.database.record_report(101, 202, "Спам или мошенничество")
        )
        self.assertFalse(
            self.database.record_report(101, 202, "Поддельная анкета")
        )
        self.assertEqual(
            self.database.reports_for_review()[0]["reason"],
            "Спам или мошенничество",
        )
        self.assertIsNone(self.database.next_profile(101, viewer))

    def test_report_submission_does_not_message_reported_user(self) -> None:
        self.database.save_profile(101, profile("Анна", "Женщина", "Мужчин"))
        self.database.save_profile(202, profile("Илья", "Мужчина", "Женщин"))
        query = SimpleNamespace(
            data="report_reason:202:harassment",
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"database": self.database}),
            bot=SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock()),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=101),
            effective_chat=SimpleNamespace(id=101),
        )

        asyncio.run(submit_report(update, context))

        self.assertEqual(
            self.database.reports_for_review()[0]["reported_id"],
            202,
        )
        self.assertEqual(context.bot.send_message.await_count, 2)
        self.assertTrue(
            all(
                call.kwargs["chat_id"] == 101
                for call in context.bot.send_message.await_args_list
            )
        )
        self.assertEqual(
            context.bot.send_message.await_args_list[-1].kwargs["text"],
            HOME_MENU_TEXT,
        )
        context.bot.send_photo.assert_not_awaited()

    def test_profiles_must_be_compatible_in_both_directions(self) -> None:
        viewer = profile("Анна", "Женщина", "Мужчин")
        self.database.save_profile(101, viewer)
        self.database.save_profile(202, profile("Илья", "Мужчина", "Мужчин"))

        self.assertIsNone(self.database.next_profile(101, viewer))

    def test_gender_preference(self) -> None:
        self.assertTrue(accepts_gender("Всех", "Другое"))
        self.assertTrue(accepts_gender("Женщин", "Женщина"))
        self.assertFalse(accepts_gender("Мужчин", "Женщина"))

    def test_paused_profiles_are_hidden_and_can_be_restored(self) -> None:
        viewer = profile("Анна", "Женщина", "Мужчин")
        self.database.save_profile(101, viewer)
        self.database.save_profile(202, profile("Илья", "Мужчина", "Женщин"))

        self.assertTrue(self.database.set_search_active(202, False))
        self.assertIsNone(self.database.next_profile(101, viewer))

        self.assertTrue(self.database.set_search_active(202, True))
        self.assertEqual(
            self.database.next_profile(101, viewer)["telegram_id"],
            202,
        )

    def test_reply_keyboard_has_five_persistent_buttons_in_one_row(self) -> None:
        keyboard = main_menu()
        labels = [button.text for row in keyboard.keyboard for button in row]

        self.assertTrue(keyboard.is_persistent)
        self.assertFalse(keyboard.one_time_keyboard)
        self.assertEqual(len(keyboard.keyboard), 1)
        self.assertEqual(len(keyboard.keyboard[0]), 5)
        self.assertEqual(labels, ["1", "2", "3", "4", "5"])
        self.assertEqual(
            HOME_MENU_TEXT,
            "Подождем пока кто-то увидит твою анкету\n\n"
            "1. Смотреть анкеты.\n"
            "2. Моя анкета.\n"
            "3. Я больше не хочу никого искать.\n"
            "4. Изменить анкету.\n"
            "***\n"
            "5. Активируй Premium — будь в топе ⭐️.",
        )

    def test_migrates_legacy_database_without_deleting_or_duplicating_data(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "leo.db"
        new_path = Path(self.temp_dir.name) / "bot.db"
        with sqlite3.connect(legacy_path) as legacy:
            legacy.executescript(
                """
                CREATE TABLE users (
                    telegram_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    age INTEGER NOT NULL,
                    gender TEXT NOT NULL,
                    city TEXT NOT NULL,
                    photo_file_id TEXT NOT NULL,
                    about TEXT NOT NULL,
                    looking_for TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE likes (
                    from_user_id INTEGER NOT NULL,
                    to_user_id INTEGER NOT NULL,
                    is_like INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (from_user_id, to_user_id)
                );
                """
            )
            legacy.executemany(
                """
                INSERT INTO users (
                    telegram_id, name, age, gender, city,
                    photo_file_id, about, looking_for
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (301, "Анна", 25, "Женщина", "Алматы", "photo-a", "Текст A", "Мужчин"),
                    (302, "Илья", 27, "Мужчина", "Алматы", "photo-b", "Текст B", "Женщин"),
                ],
            )
            legacy.executemany(
                """
                INSERT INTO likes (from_user_id, to_user_id, is_like)
                VALUES (?, ?, ?)
                """,
                [(301, 302, 1), (302, 301, 1)],
            )

        migrated = Database(new_path, legacy_path=legacy_path)
        migrated.initialize()
        saved = migrated.get_user(301)

        self.assertEqual(saved["photo_file_id"], "photo-a")
        self.assertEqual(saved["about"], "Текст A")
        self.assertEqual(saved["is_active"], 1)
        self.assertTrue(migrated.has_mutual_like(301, 302))
        self.assertTrue(legacy_path.exists())

        migrated.initialize()
        with sqlite3.connect(new_path) as database:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                2,
            )
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM likes").fetchone()[0],
                2,
            )
        with sqlite3.connect(legacy_path) as legacy:
            self.assertEqual(
                legacy.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                2,
            )


class StartHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_sends_photo_then_profile_text_then_menu(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(
                Path(temp_dir) / "bot.db",
                legacy_path=Path(temp_dir) / "missing-legacy.db",
            )
            database.initialize()
            database.save_profile(501, profile("Сохранённый профиль", "Женщина", "Мужчин"))

            events: list[tuple[str, dict[str, object]]] = []

            async def record_photo(**kwargs: object) -> None:
                events.append(("photo", kwargs))

            async def record_message(**kwargs: object) -> None:
                events.append(("message", kwargs))

            reply_text = AsyncMock()
            send_photo = AsyncMock(side_effect=record_photo)
            send_message = AsyncMock(side_effect=record_message)
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=501),
                effective_message=SimpleNamespace(
                    chat_id=501,
                    reply_text=reply_text,
                ),
            )
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"database": database}),
                bot=SimpleNamespace(
                    send_photo=send_photo,
                    send_message=send_message,
                ),
            )

            state = await start(update, context)

            self.assertEqual(state, ConversationHandler.END)
            reply_text.assert_not_awaited()
            self.assertEqual(send_photo.await_args.kwargs["photo"], "telegram-photo-id")
            self.assertEqual([kind for kind, _ in events], ["photo", "message", "message"])
            self.assertEqual(
                events[1][1]["text"],
                "Сохранённый профиль, 25, Алматы — О себе",
            )
            self.assertIsInstance(
                events[1][1]["reply_markup"],
                ReplyKeyboardMarkup,
            )
            self.assertEqual(events[2][1]["text"], HOME_MENU_TEXT)
            self.assertIsInstance(
                events[2][1]["reply_markup"],
                ReplyKeyboardMarkup,
            )


if __name__ == "__main__":
    unittest.main()
