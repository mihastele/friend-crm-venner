"""Venner storage layer: SQLite database, password hashing, reminders engine."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  must_change_password INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'custom',
  policy_type TEXT NOT NULL DEFAULT 'none',
  frequency TEXT NOT NULL DEFAULT 'weekly',
  message TEXT NOT NULL DEFAULT '',
  birthday_alert INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS friends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  first_name TEXT NOT NULL DEFAULT '',
  last_name TEXT NOT NULL DEFAULT '',
  birthday TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual',
  category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  notes TEXT NOT NULL DEFAULT '',
  last_contact TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  friend_a_id INTEGER NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
  friend_b_id INTEGER NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
  reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  friend_id INTEGER REFERENCES friends(id) ON DELETE CASCADE,
  type TEXT NOT NULL,
  message TEXT NOT NULL,
  due_at TEXT NOT NULL,
  is_read INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_friends_owner ON friends(owner_id);
CREATE INDEX IF NOT EXISTS idx_cat_owner ON categories(owner_id);
CREATE INDEX IF NOT EXISTS idx_notif_owner ON notifications(owner_id);
CREATE INDEX IF NOT EXISTS idx_merge_owner ON merge_candidates(owner_id);
"""

FREQ_DAYS = {"daily": 1, "weekly": 7, "biweekly": 14, "monthly": 30}

DEFAULT_CATEGORIES = [
    ("Close friends", "close", "contact", "weekly",
     "Reach out to {name} - your close friends need you this week.", 1),
    ("Acquaintances", "acquaintance", "contact", "monthly",
     "Check in with {name} sometime soon.", 1),
    ("Celebrate", "celebrate", "none", "weekly", "", 1),
    ("Cutoff list", "avoid", "avoid", "weekly",
     "Reminder: keep your distance from {name}.", 0),
    ("Blocklist", "avoid", "avoid", "weekly",
     "Do not engage with {name}. Protect your peace.", 0),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def display_name(f: dict) -> str:
    full = f"{f.get('first_name', '')} {f.get('last_name', '')}".strip()
    return full or f.get("phone") or f.get("email") or "Unnamed"


def fill_template(tpl: str, ctx: dict) -> str:
    out = tpl or ""
    for key, val in ctx.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 210_000)
    return digest.hex(), salt


def parse_birthday(raw: str) -> tuple[int, int] | None:
    """Accept YYYY-MM-DD, MM-DD, YYYYMMDD, or ISO datetime. Return (month, day)."""
    s = (raw or "").strip().replace("/", "-")
    if not s:
        return None
    if "T" in s:
        s = s.split("T", 1)[0]
    p = [x for x in s.split("-") if x]
    try:
        if len(p) == 3:
            return int(p[1]), int(p[2])
        if len(p) == 2 and len(p[0]) <= 2:
            return int(p[0]), int(p[1])
        digits = "".join(ch for ch in s if ch.isdigit())
        if len(digits) == 8:
            return int(digits[4:6]), int(digits[6:8])
        if len(digits) == 4:
            return int(digits[:2]), int(digits[2:])
    except ValueError:
        return None
    return None


def next_birthday(month: int, day: int, today: date) -> date | None:
    def pick(year: int) -> date | None:
        try:
            return date(year, month, day)
        except ValueError:
            if month == 2 and day == 29:
                return date(year, 2, 28)
            return None
    this_year = pick(today.year)
    if this_year is None:
        return None
    if this_year < today:
        return pick(today.year + 1)
    return this_year


class Store:
    def __init__(self, path: str, admin_user: str = "admin",
                 admin_password: str | None = None):
        self.path = path
        self.lock = threading.RLock()
        self.seed_info: dict | None = None
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()
            if self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                user = (admin_user or "admin").strip() or "admin"
                generated = not (admin_password or "")
                password = admin_password if not generated else secrets.token_urlsafe(16)
                self._create_user_locked(user, password, True, True)
                self.seed_info = {"username": user, "generated": generated,
                                  "password": password if generated else None}

    # -- users ------------------------------------------------------------
    def _create_user_locked(self, username, password, is_admin, must_change):
        digest, salt = hash_password(password)
        try:
            cur = self.db.execute(
                "INSERT INTO users (username, password_hash, salt, is_admin,"
                " must_change_password, created_at) VALUES (?,?,?,?,?,?)",
                (username.strip(), digest, salt, int(bool(is_admin)),
                 int(bool(must_change)), utcnow()))
        except sqlite3.IntegrityError:
            raise ValueError("username taken")
        uid = cur.lastrowid
        for name, kind, pol, freq, msg, bday in DEFAULT_CATEGORIES:
            self.db.execute(
                "INSERT INTO categories (owner_id, name, kind, policy_type,"
                " frequency, message, birthday_alert, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (uid, name, kind, pol, freq, msg, bday, utcnow()))
        self.db.commit()
        return self.get_user(uid)

    def create_user(self, username, password, is_admin=False, must_change=True):
        with self.lock:
            return self._create_user_locked(username, password, is_admin, must_change)

    def get_user(self, uid: int) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            return dict(row) if row else None

    def get_user_by_name(self, username: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM users WHERE username=?",
                                  (username.strip(),)).fetchone()
            return dict(row) if row else None

    def public_user(self, u: dict) -> dict:
        return {"id": u["id"], "username": u["username"],
                "is_admin": bool(u["is_admin"]),
                "must_change_password": bool(u["must_change_password"]),
                "created_at": u["created_at"]}

    def list_users(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM users ORDER BY id").fetchall()
            return [self.public_user(dict(r)) for r in rows]

    def delete_user(self, uid: int):
        with self.lock:
            self.db.execute("DELETE FROM users WHERE id=?", (uid,))
            self.db.commit()

    def set_password(self, uid: int, password: str, must_change: bool):
        digest, salt = hash_password(password)
        with self.lock:
            self.db.execute("UPDATE users SET password_hash=?, salt=?,"
                            " must_change_password=? WHERE id=?",
                            (digest, salt, int(bool(must_change)), uid))
            self.db.commit()

    def verify(self, user: dict, password: str) -> bool:
        digest, _ = hash_password(password, user["salt"])
        return secrets.compare_digest(digest, user["password_hash"])

    # -- categories -------------------------------------------------------
    def list_categories(self, owner_id: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM categories WHERE owner_id=?"
                                   " ORDER BY id", (owner_id,)).fetchall()
            return [dict(r) for r in rows]

    def create_category(self, owner_id: int, data: dict) -> dict:
        name = (data.get("name") or "").strip()
        if not name:
            raise ValueError("category name required")
        pol = data.get("policy_type", "none")
        if pol not in ("contact", "avoid", "none"):
            raise ValueError("bad policy_type")
        freq = data.get("frequency", "weekly")
        if freq not in FREQ_DAYS:
            raise ValueError("bad frequency")
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO categories (owner_id, name, kind, policy_type,"
                " frequency, message, birthday_alert, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (owner_id, name, (data.get("kind") or "custom").strip() or "custom",
                 pol, freq, (data.get("message") or "").strip(),
                 int(bool(data.get("birthday_alert", True))), utcnow()))
            self.db.commit()
            return dict(self.db.execute("SELECT * FROM categories WHERE id=?",
                                        (cur.lastrowid,)).fetchone())

    def update_category(self, owner_id: int, cid: int, data: dict) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM categories WHERE id=? AND owner_id=?",
                                  (cid, owner_id)).fetchone()
            if not row:
                return None
            cur = dict(row)
            name = (data.get("name") or cur["name"]).strip()
            pol = data.get("policy_type", cur["policy_type"])
            freq = data.get("frequency", cur["frequency"])
            if pol not in ("contact", "avoid", "none") or freq not in FREQ_DAYS:
                raise ValueError("bad policy")
            self.db.execute(
                "UPDATE categories SET name=?, kind=?, policy_type=?, frequency=?,"
                " message=?, birthday_alert=? WHERE id=?",
                (name, (data.get("kind") or cur["kind"]), pol, freq,
                 (data.get("message") if data.get("message") is not None
                  else cur["message"]),
                 int(bool(data.get("birthday_alert", cur["birthday_alert"]))), cid))
            self.db.commit()
            return dict(self.db.execute("SELECT * FROM categories WHERE id=?",
                                        (cid,)).fetchone())

    def delete_category(self, owner_id: int, cid: int) -> bool:
        with self.lock:
            self.db.execute("UPDATE friends SET category_id=NULL"
                            " WHERE owner_id=? AND category_id=?", (owner_id, cid))
            cur = self.db.execute("DELETE FROM categories WHERE id=? AND owner_id=?",
                                  (cid, owner_id))
            self.db.commit()
            return cur.rowcount > 0

    # -- friends ----------------------------------------------------------
    def list_friends(self, owner_id: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM friends WHERE owner_id=?"
                                   " ORDER BY last_name, first_name", (owner_id,)).fetchall()
            return [dict(r) for r in rows]

    def get_friend(self, owner_id: int, fid: int) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM friends WHERE id=? AND owner_id=?",
                                  (fid, owner_id)).fetchone()
            return dict(row) if row else None

    def _category_owned(self, owner_id: int, cid) -> bool:
        if cid in (None, "", 0):
            return True
        row = self.db.execute("SELECT id FROM categories WHERE id=? AND owner_id=?",
                              (cid, owner_id)).fetchone()
        return row is not None

    def create_friend(self, owner_id: int, data: dict) -> dict:
        cid = data.get("category_id") or None
        with self.lock:
            if not self._category_owned(owner_id, cid):
                raise ValueError("unknown category")
            cur = self.db.execute(
                "INSERT INTO friends (owner_id, first_name, last_name, birthday, phone,"
                " email, source, category_id, notes, last_contact, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (owner_id, (data.get("first_name") or "").strip(),
                 (data.get("last_name") or "").strip(),
                 (data.get("birthday") or "").strip(),
                 (data.get("phone") or "").strip(),
                 (data.get("email") or "").strip(),
                 (data.get("source") or "manual").strip() or "manual",
                 cid, (data.get("notes") or "").strip(),
                 (data.get("last_contact") or "").strip(), utcnow()))
            self.db.commit()
            return dict(self.db.execute("SELECT * FROM friends WHERE id=?",
                                        (cur.lastrowid,)).fetchone())

    def update_friend(self, owner_id: int, fid: int, data: dict) -> dict | None:
        with self.lock:
            if not self.get_friend(owner_id, fid):
                return None
            cid = data.get("category_id", "keep")
            if cid == "keep":
                cid = self.db.execute("SELECT category_id FROM friends WHERE id=?",
                                      (fid,)).fetchone()[0]
            else:
                cid = cid or None
                if not self._category_owned(owner_id, cid):
                    raise ValueError("unknown category")
            row = self.db.execute("SELECT * FROM friends WHERE id=?", (fid,)).fetchone()
            cur = dict(row)
            get = lambda k: (data.get(k) if data.get(k) is not None else cur[k])
            self.db.execute(
                "UPDATE friends SET first_name=?, last_name=?, birthday=?, phone=?,"
                " email=?, category_id=?, notes=?, last_contact=? WHERE id=?",
                (str(get("first_name")).strip(), str(get("last_name")).strip(),
                 str(get("birthday")).strip(), str(get("phone")).strip(),
                 str(get("email")).strip(), cid, str(get("notes")).strip(),
                 str(get("last_contact")).strip(), fid))
            self.db.commit()
            return self.get_friend(owner_id, fid)

    def delete_friend(self, owner_id: int, fid: int) -> bool:
        with self.lock:
            self.db.execute("UPDATE merge_candidates SET status='rejected'"
                            " WHERE owner_id=? AND status='pending'"
                            " AND (friend_a_id=? OR friend_b_id=?)",
                            (owner_id, fid, fid))
            cur = self.db.execute("DELETE FROM friends WHERE id=? AND owner_id=?",
                                  (fid, owner_id))
            self.db.commit()
            return cur.rowcount > 0

    def find_by_name(self, owner_id: int, first: str, last: str,
                     exclude_id: int | None = None) -> dict | None:
        fn, ln = first.strip().lower(), last.strip().lower()
        if not fn:
            return None
        with self.lock:
            for r in self.db.execute("SELECT * FROM friends WHERE owner_id=?",
                                     (owner_id,)).fetchall():
                d = dict(r)
                if exclude_id and d["id"] == exclude_id:
                    continue
                if (d["first_name"].strip().lower() == fn
                        and d["last_name"].strip().lower() == ln):
                    return d
        return None

    # -- merge candidates -------------------------------------------------
    def add_candidate(self, owner_id: int, a_id: int, b_id: int, reason: str) -> dict | None:
        if a_id == b_id:
            return None
        with self.lock:
            dup = self.db.execute(
                "SELECT id FROM merge_candidates WHERE owner_id=? AND status='pending'"
                " AND ((friend_a_id=? AND friend_b_id=?)"
                " OR (friend_a_id=? AND friend_b_id=?))",
                (owner_id, a_id, b_id, b_id, a_id)).fetchone()
            if dup:
                return None
            cur = self.db.execute(
                "INSERT INTO merge_candidates (owner_id, friend_a_id, friend_b_id,"
                " reason, status, created_at) VALUES (?,?,?,?,?,?)",
                (owner_id, a_id, b_id, reason, "pending", utcnow()))
            self.db.commit()
            return dict(self.db.execute("SELECT * FROM merge_candidates WHERE id=?",
                                        (cur.lastrowid,)).fetchone())

    def list_candidates(self, owner_id: int, status: str = "pending") -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM merge_candidates WHERE owner_id=? AND status=?"
                " ORDER BY id DESC", (owner_id, status)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                a = self.db.execute("SELECT * FROM friends WHERE id=?",
                                    (d["friend_a_id"],)).fetchone()
                b = self.db.execute("SELECT * FROM friends WHERE id=?",
                                    (d["friend_b_id"],)).fetchone()
                d["friend_a"] = dict(a) if a else None
                d["friend_b"] = dict(b) if b else None
                out.append(d)
            return out

    def resolve_candidate(self, owner_id: int, cid: int, confirm: bool) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM merge_candidates"
                                  " WHERE id=? AND owner_id=? AND status='pending'",
                                  (cid, owner_id)).fetchone()
            if not row:
                return None
            d = dict(row)
            if not confirm:
                self.db.execute("UPDATE merge_candidates SET status='rejected' WHERE id=?",
                                (cid,))
                self.db.commit()
                return {"status": "rejected"}
            a = self.db.execute("SELECT * FROM friends WHERE id=?",
                                (d["friend_a_id"],)).fetchone()
            b = self.db.execute("SELECT * FROM friends WHERE id=?",
                                (d["friend_b_id"],)).fetchone()
            if not a or not b:
                self.db.execute("UPDATE merge_candidates SET status='rejected' WHERE id=?",
                                (cid,))
                self.db.commit()
                return {"status": "rejected", "note": "entry no longer exists"}
            A, B = dict(a), dict(b)
            merged = dict(A)
            for key in ("first_name", "last_name", "birthday", "phone", "email", "notes",
                        "category_id", "last_contact"):
                if not (merged.get(key) or "") and (B.get(key) or ""):
                    merged[key] = B[key]
            if A.get("last_contact") and B.get("last_contact"):
                merged["last_contact"] = max(A["last_contact"], B["last_contact"])
            self.db.execute(
                "UPDATE friends SET first_name=?, last_name=?, birthday=?, phone=?,"
                " email=?, notes=?, category_id=?, last_contact=? WHERE id=?",
                (merged["first_name"], merged["last_name"], merged["birthday"],
                 merged["phone"], merged["email"], merged["notes"],
                 merged["category_id"], merged["last_contact"], A["id"]))
            self.db.execute("DELETE FROM friends WHERE id=?", (B["id"],))
            self.db.execute("UPDATE merge_candidates SET status='merged' WHERE id=?", (cid,))
            self.db.execute("UPDATE merge_candidates SET status='rejected'"
                            " WHERE status='pending' AND id<>? AND owner_id=?"
                            " AND (friend_a_id=? OR friend_b_id=? OR friend_a_id=? OR friend_b_id=?)",
                            (cid, owner_id, A["id"], A["id"], B["id"], B["id"]))
            self.db.commit()
            return {"status": "merged",
                    "friend": dict(self.db.execute("SELECT * FROM friends WHERE id=?",
                                                   (A["id"],)).fetchone())}

    # -- notifications ----------------------------------------------------
    def list_notifications(self, owner_id: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT n.*, f.first_name, f.last_name FROM notifications n"
                " LEFT JOIN friends f ON f.id=n.friend_id"
                " WHERE n.owner_id=? ORDER BY n.is_read, n.due_at DESC, n.id DESC"
                " LIMIT 300", (owner_id,)).fetchall()
            return [dict(r) for r in rows]

    def mark_read(self, owner_id: int, nid: int) -> bool:
        with self.lock:
            cur = self.db.execute("UPDATE notifications SET is_read=1"
                                  " WHERE id=? AND owner_id=?", (nid, owner_id))
            self.db.commit()
            return cur.rowcount > 0

    def _has_unread(self, owner_id: int, fid: int, ntype: str) -> dict | None:
        row = self.db.execute("SELECT * FROM notifications WHERE owner_id=? AND friend_id=?"
                              " AND type=? AND is_read=0", (owner_id, fid, ntype)).fetchone()
        return dict(row) if row else None

    def _recent_since(self, owner_id: int, fid: int, ntype: str, days: int) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        row = self.db.execute("SELECT id FROM notifications WHERE owner_id=? AND friend_id=?"
                              " AND type=? AND created_at>=?",
                              (owner_id, fid, ntype, cutoff)).fetchone()
        return row is not None

    def generate_notifications(self, owner_id: int) -> int:
        made = 0
        today_d = date.today()
        today_s = today_d.isoformat()
        with self.lock:
            friends = self.list_friends(owner_id)
            cats = {c["id"]: c for c in self.list_categories(owner_id)}
            for f in friends:
                cat = cats.get(f.get("category_id") or 0)
                pol = cat["policy_type"] if cat else "none"
                freq = FREQ_DAYS.get(cat["frequency"] if cat else "weekly", 7)
                base_raw = (f.get("last_contact") or f.get("created_at") or "")[:10]
                try:
                    base_d = date.fromisoformat(base_raw)
                except ValueError:
                    base_d = today_d
                name = display_name(f)
                ctx = {"name": name, "birthday": f.get("birthday") or "-",
                       "phone": f.get("phone") or "-"}
                if pol == "contact" and (today_d - base_d).days >= freq:
                    if not self._has_unread(owner_id, f["id"], "contact"):
                        msg = fill_template(cat["message"] or "Contact {name}.", ctx)
                        self.db.execute(
                            "INSERT INTO notifications (owner_id, friend_id, type, message,"
                            " due_at, is_read, created_at) VALUES (?,?,?,?,?,?,?)",
                            (owner_id, f["id"], "contact", msg, today_s, 0, utcnow()))
                        made += 1
                if pol == "avoid":
                    if (not self._has_unread(owner_id, f["id"], "avoid")
                            and not self._recent_since(owner_id, f["id"], "avoid", freq)):
                        msg = fill_template(cat["message"] or "Keep away from {name}.", ctx)
                        self.db.execute(
                            "INSERT INTO notifications (owner_id, friend_id, type, message,"
                            " due_at, is_read, created_at) VALUES (?,?,?,?,?,?,?)",
                            (owner_id, f["id"], "avoid", msg, today_s, 0, utcnow()))
                        made += 1
                if f.get("birthday") and (cat is None or cat.get("birthday_alert")):
                    parsed = parse_birthday(f["birthday"])
                    if parsed:
                        nb = next_birthday(parsed[0], parsed[1], today_d)
                        if nb and 0 <= (nb - today_d).days <= 14:
                            delta = (nb - today_d).days
                            when = "today" if delta == 0 else (
                                "tomorrow" if delta == 1 else f"in {delta} days")
                            msg = (f"{name}'s birthday is {when} ({nb.isoformat()})."
                                   + (f" Call: {f['phone']}." if f.get("phone") else ""))
                            existing = self._has_unread(owner_id, f["id"], "birthday")
                            if existing:
                                self.db.execute("UPDATE notifications SET message=?, due_at=?"
                                                " WHERE id=?", (msg, nb.isoformat(), existing["id"]))
                            else:
                                self.db.execute(
                                    "INSERT INTO notifications (owner_id, friend_id, type,"
                                    " message, due_at, is_read, created_at)"
                                    " VALUES (?,?,?,?,?,?,?)",
                                    (owner_id, f["id"], "birthday", msg, nb.isoformat(), 0,
                                     utcnow()))
                                made += 1
            self.db.commit()
        return made
