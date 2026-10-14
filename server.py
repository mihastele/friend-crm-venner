#!/usr/bin/env python3
"""Venner server: stdlib HTTP API + static PWA hosting. No dependencies."""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sys
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import parsers
from store import Store, display_name

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
TOKEN_TTL = 24 * 3600
MAX_BODY = 8 * 1024 * 1024


def load_secret() -> str:
    path = os.environ.get("VENNER_SECRET_FILE", os.path.join(ROOT, ".venner_secret"))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    secret = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(secret)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


SECRET = load_secret()


def make_token(uid: int) -> str:
    body = f"{uid}:{int(time.time()) + TOKEN_TTL}"
    sig = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}:{sig}".encode()).decode()


def parse_token(token: str) -> int | None:
    try:
        uid_s, exp_s, sig = base64.urlsafe_b64decode(token.encode()).decode().split(":")
        if int(exp_s) < time.time():
            return None
        good = hmac.new(SECRET.encode(), f"{uid_s}:{exp_s}".encode(),
                        hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        return int(uid_s)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "Venner/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {self.command} {self.path} -> {fmt % args}\n")

    # -- helpers ---------------------------------------------------------
    def _json(self, code: int, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str):
        self._json(code, {"error": msg})

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    @property
    def store(self) -> Store:
        return self.server.store

    def _auth(self, admin: bool = False):
        header = self.headers.get("Authorization") or ""
        token = header[7:] if header.startswith("Bearer ") else ""
        uid = parse_token(token) if token else None
        user = self.store.get_user(uid) if uid else None
        if not user:
            self._err(401, "login required")
            return None
        if admin and not user["is_admin"]:
            self._err(403, "admin only")
            return None
        return user

    # -- dispatch ----------------------------------------------------------
    def _dispatch(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        method = self.command
        if not parts or parts[0] != "api":
            if method == "GET":
                return self._serve_static(url.path)
            return self._err(404, "not found")
        try:
            return self._route_api(method, parts[1:])
        except ValueError as exc:
            return self._err(400, str(exc) or "bad request")
        except Exception as exc:  # never leak internals
            sys.stderr.write(f"API error: {exc!r}\n")
            return self._err(500, "server error")

    do_GET = do_POST = do_PUT = do_DELETE = _dispatch

    def _route_api(self, method: str, parts: list[str]):
        if parts == ["login"] and method == "POST":
            return self._api_login()
        if parts == ["me"] and method == "GET":
            user = self._auth()
            return user and self._json(200, {"user": self.store.public_user(user)})
        if parts == ["change-password"] and method == "POST":
            return self._api_change_password()
        if parts[:1] == ["admin"]:
            return self._route_admin(method, parts[1:])
        if parts[:1] == ["friends"]:
            return self._route_friends(method, parts[1:])
        if parts[:1] == ["categories"]:
            return self._route_categories(method, parts[1:])
        if parts[:1] == ["import"]:
            return self._route_import(method, parts[1:])
        if parts[:1] == ["merge-candidates"]:
            return self._route_merge(method, parts[1:])
        if parts[:1] == ["notifications"]:
            return self._route_notifications(method, parts[1:])
        return self._err(404, "unknown endpoint")

    # -- auth --------------------------------------------------------------
    def _api_login(self):
        data = self._body()
        user = self.store.get_user_by_name(data.get("username") or "")
        if not user or not self.store.verify(user, data.get("password") or ""):
            return self._err(401, "wrong username or password")
        try:
            self.store.generate_notifications(user["id"])
        except Exception:
            pass
        return self._json(200, {"token": make_token(user["id"]),
                                "user": self.store.public_user(user)})

    def _api_change_password(self):
        user = self._auth()
        if not user:
            return
        new = (self._body().get("newPassword") or "")
        if len(new) < 8:
            return self._err(400, "new password must be at least 8 characters")
        self.store.set_password(user["id"], new, must_change=False)
        return self._json(200, {"ok": True,
                                "user": self.store.public_user(self.store.get_user(user["id"]))})

    # -- admin ---------------------------------------------------------------
    def _route_admin(self, method: str, parts: list[str]):
        user = self._auth(admin=True)
        if not user:
            return
        if parts == ["users"] and method == "GET":
            return self._json(200, {"users": self.store.list_users()})
        if parts == ["users"] and method == "POST":
            data = self._body()
            username = (data.get("username") or "").strip()
            temp = data.get("tempPassword") or ""
            if len(username) < 2:
                return self._err(400, "username must be at least 2 characters")
            if len(temp) < 4:
                return self._err(400, "temporary password must be at least 4 characters")
            created = self.store.create_user(username, temp,
                                             is_admin=bool(data.get("isAdmin")),
                                             must_change=True)
            return self._json(201, {"user": self.store.public_user(created)})
        if len(parts) == 2 and parts[0] == "users" and method == "DELETE":
            try:
                target = int(parts[1])
            except ValueError:
                return self._err(404, "no such user")
            if target == user["id"]:
                return self._err(400, "you cannot remove yourself")
            victim = self.store.get_user(target)
            if not victim:
                return self._err(404, "no such user")
            if victim["is_admin"]:
                admins = [u for u in self.store.list_users() if u["is_admin"]]
                if len(admins) <= 1:
                    return self._err(400, "cannot remove the last admin")
            self.store.delete_user(target)
            return self._json(200, {"ok": True})
        if len(parts) == 3 and parts[0] == "users" and parts[2] == "reset-password" \
                and method == "POST":
            try:
                target = int(parts[1])
            except ValueError:
                return self._err(404, "no such user")
            if not self.store.get_user(target):
                return self._err(404, "no such user")
            temp = self._body().get("tempPassword") or ""
            if len(temp) < 4:
                return self._err(400, "temporary password must be at least 4 characters")
            self.store.set_password(target, temp, must_change=True)
            return self._json(200, {"ok": True})
        return self._err(404, "unknown endpoint")

    # -- friends -------------------------------------------------------------
    def _route_friends(self, method: str, parts: list[str]):
        user = self._auth()
        if not user:
            return
        if parts == [] and method == "GET":
            return self._json(200, {"friends": self.store.list_friends(user["id"])})
        if parts == [] and method == "POST":
            return self._json(201, {"friend": self.store.create_friend(user["id"],
                                                                       self._body())})
        if len(parts) == 1 and method in ("PUT", "DELETE"):
            try:
                fid = int(parts[0])
            except ValueError:
                return self._err(404, "no such friend")
            if method == "DELETE":
                if not self.store.delete_friend(user["id"], fid):
                    return self._err(404, "no such friend")
                return self._json(200, {"ok": True})
            updated = self.store.update_friend(user["id"], fid, self._body())
            if not updated:
                return self._err(404, "no such friend")
            return self._json(200, {"friend": updated})
        if len(parts) == 2 and parts[1] == "contact" and method == "POST":
            try:
                fid = int(parts[0])
            except ValueError:
                return self._err(404, "no such friend")
            updated = self.store.update_friend(user["id"], fid,
                                               {"last_contact": date.today().isoformat()})
            if not updated:
                return self._err(404, "no such friend")
            return self._json(200, {"friend": updated})
        return self._err(404, "unknown endpoint")

    # -- categories ----------------------------------------------------------
    def _route_categories(self, method: str, parts: list[str]):
        user = self._auth()
        if not user:
            return
        if parts == [] and method == "GET":
            return self._json(200, {"categories": self.store.list_categories(user["id"])})
        if parts == [] and method == "POST":
            return self._json(201, {"category": self.store.create_category(user["id"],
                                                                           self._body())})
        if len(parts) == 1 and method in ("PUT", "DELETE"):
            try:
                cid = int(parts[0])
            except ValueError:
                return self._err(404, "no such category")
            if method == "DELETE":
                if not self.store.delete_category(user["id"], cid):
                    return self._err(404, "no such category")
                return self._json(200, {"ok": True})
            updated = self.store.update_category(user["id"], cid, self._body())
            if not updated:
                return self._err(404, "no such category")
            return self._json(200, {"category": updated})
        return self._err(404, "unknown endpoint")

    # -- imports ---------------------------------------------------------------
    def _run_import(self, user: dict, entries: list[dict], source: str):
        made, candidates = [], []
        for entry in entries:
            first = (entry.get("first_name") or "").strip()
            last = (entry.get("last_name") or "").strip()
            match = self.store.find_by_name(user["id"], first, last) if first else None
            friend = self.store.create_friend(user["id"], {
                "first_name": first, "last_name": last,
                "birthday": entry.get("birthday") or "",
                "phone": entry.get("phone") or "",
                "email": entry.get("email") or "", "source": source})
            made.append(friend)
            if match:
                cand = self.store.add_candidate(
                    user["id"], match["id"], friend["id"],
                    f"'{display_name(friend)}' is in both {match.get('source', 'earlier')}"
                    f" and {source} - same person?")
                if cand:
                    cand["friend_a"], cand["friend_b"] = match, friend
                    candidates.append(cand)
        return {"imported": len(made), "candidates": candidates, "friends": made}

    def _route_import(self, method: str, parts: list[str]):
        user = self._auth()
        if not user:
            return
        if method != "POST" or len(parts) != 1:
            return self._err(404, "unknown endpoint")
        data = self._body()
        content, filename = data.get("content") or "", data.get("filename") or ""
        if not content.strip():
            return self._err(400, "empty file")
        kind = parts[0]
        if kind == "birthdays":
            entries = parsers.parse_ics_birthdays(content)
            if not entries:
                return self._err(400, "no birthday events found in this calendar file")
            return self._json(200, self._run_import(user, entries, "birthday-calendar"))
        if kind == "contacts":
            entries = parsers.parse_contacts(content, filename)
            if not entries:
                return self._err(400, "no contacts found in this file")
            return self._json(200, self._run_import(user, entries, "contacts"))
        if kind == "phones":
            entries = parsers.parse_phones(content)
            if not entries:
                return self._err(400, "no phone numbers found in this file")
            return self._json(200, self._run_import(user, entries, "phones"))
        return self._err(404, "unknown endpoint")

    # -- merge review ------------------------------------------------------------
    def _route_merge(self, method: str, parts: list[str]):
        user = self._auth()
        if not user:
            return
        if parts == [] and method == "GET":
            return self._json(200, {"candidates": self.store.list_candidates(user["id"])})
        if len(parts) == 2 and parts[1] == "resolve" and method == "POST":
            try:
                cid = int(parts[0])
            except ValueError:
                return self._err(404, "no such suggestion")
            result = self.store.resolve_candidate(user["id"], cid,
                                                  bool(self._body().get("confirm")))
            if not result:
                return self._err(404, "no such suggestion")
            return self._json(200, result)
        return self._err(404, "unknown endpoint")

    # -- notifications -------------------------------------------------------------
    def _route_notifications(self, method: str, parts: list[str]):
        user = self._auth()
        if not user:
            return
        if parts == [] and method == "GET":
            try:
                self.store.generate_notifications(user["id"])
            except Exception:
                pass
            return self._json(200, {"notifications": self.store.list_notifications(user["id"])})
        if parts == ["generate"] and method == "POST":
            made = self.store.generate_notifications(user["id"])
            return self._json(200, {"created": made})
        if len(parts) == 2 and parts[1] == "read" and method == "POST":
            try:
                nid = int(parts[0])
            except ValueError:
                return self._err(404, "no such reminder")
            if not self.store.mark_read(user["id"], nid):
                return self._err(404, "no such reminder")
            return self._json(200, {"ok": True})
        return self._err(404, "unknown endpoint")

    # -- static ----------------------------------------------------------------------
    def _serve_static(self, raw_path: str):
        path = raw_path.split("?", 1)[0].split("#", 1)[0]
        if path == "/":
            path = "/index.html"
        target = os.path.normpath(os.path.join(PUBLIC, path.lstrip("/")))
        if not target.startswith(PUBLIC + os.sep) and target != PUBLIC:
            return self._err(404, "not found")
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            target = os.path.join(PUBLIC, "index.html")
            if not os.path.isfile(target):
                return self._err(404, "not found")
        ctype, _ = mimetypes.guess_type(target)
        if target.endswith(".js"):
            ctype = "application/javascript"
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._err(404, "not found")
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        if os.path.basename(target) == "service-worker.js":
            self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _env_port() -> int:
    try:
        return int(os.environ.get("PORT", "8765"))
    except ValueError:
        return 8765


def main():
    parser = argparse.ArgumentParser(description="Venner - friend reminders PWA")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=_env_port())
    parser.add_argument("--db", default=os.environ.get("VENNER_DB",
                                                       os.path.join(ROOT, "venner.db")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    admin_user = (os.environ.get("ADMIN_USER", "admin") or "admin").strip() or "admin"
    admin_password = os.environ.get("ADMIN_PASSWORD") or None
    server.store = Store(args.db, admin_user=admin_user, admin_password=admin_password)
    url = f"http://{args.host}:{args.port}"
    print(f"Venner running at {url}  (db: {args.db})")
    seed = server.store.seed_info
    if seed:
        print(f"Fresh database: created admin '{seed['username']}'"
              " (must change password on first login)")
        if seed["generated"]:
            print(f"Generated admin password (shown once): {seed['password']}")
        else:
            print("Admin password taken from ADMIN_PASSWORD.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()

