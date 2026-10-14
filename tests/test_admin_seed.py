"""Fresh databases seed the admin account from env-provided credentials."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from store import Store  # noqa: E402


def temp_db():
    return os.path.join(tempfile.mkdtemp(prefix="venner-seed-"), "t.db")


class AdminSeed(unittest.TestCase):
    def test_explicit_admin_credentials(self):
        store = Store(temp_db(), admin_user="boss", admin_password="s3cret-temp")
        user = store.get_user_by_name("boss")
        self.assertTrue(user and user["is_admin"] and user["must_change_password"])
        self.assertTrue(store.verify(user, "s3cret-temp"))
        self.assertIsNone(store.get_user_by_name("admin"))
        self.assertFalse(store.seed_info["generated"])
        self.assertIsNone(store.seed_info["password"])

    def test_generated_password_when_unset(self):
        store = Store(temp_db())
        info = store.seed_info
        self.assertTrue(info["generated"])
        self.assertEqual(info["username"], "admin")
        user = store.get_user_by_name("admin")
        self.assertTrue(store.verify(user, info["password"]))
        self.assertFalse(store.verify(user, "admin-1234"))

    def test_existing_db_not_reseeded(self):
        db = temp_db()
        Store(db, admin_user="one", admin_password="pw-one")
        again = Store(db, admin_user="two", admin_password="pw-two")
        self.assertIsNone(again.seed_info)
        self.assertIsNone(again.get_user_by_name("two"))
        self.assertIsNotNone(again.get_user_by_name("one"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
