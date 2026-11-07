import tempfile
import unittest
from pathlib import Path

from main import Database, accepts_gender


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

    def test_profiles_must_be_compatible_in_both_directions(self) -> None:
        viewer = profile("Анна", "Женщина", "Мужчин")
        self.database.save_profile(101, viewer)
        self.database.save_profile(202, profile("Илья", "Мужчина", "Мужчин"))

        self.assertIsNone(self.database.next_profile(101, viewer))

    def test_gender_preference(self) -> None:
        self.assertTrue(accepts_gender("Всех", "Другое"))
        self.assertTrue(accepts_gender("Женщин", "Женщина"))
        self.assertFalse(accepts_gender("Мужчин", "Женщина"))


if __name__ == "__main__":
    unittest.main()
