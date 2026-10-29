"""Authentication for LAN use: salted PBKDF2 passwords, signed stateless session cookies,
roles (admin / operator) and login rate limiting. Users live in config/auth.json (0600)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

ITERATIONS = 240_000
SESSION_TTL = 12 * 3600
COOKIE = "studio_session"
ROLES = ("admin", "operator")
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{2,40}$")

# Paths only an administrator may use (settings, storage, process control, user management).
ADMIN_RULES = [
    ("PUT", re.compile(r"^/api/settings$")),
    ("PUT", re.compile(r"^/api/books/\d+/narrative/settings$")),
    ("PUT", re.compile(r"^/api/secrets/")),
    ("DELETE", re.compile(r"^/api/secrets/")),
    ("GET", re.compile(r"^/api/storage$")),
    ("GET", re.compile(r"^/api/storage/browse$")),
    ("POST", re.compile(r"^/api/storage/")),
    ("POST", re.compile(r"^/api/books/\d+/(move|archive)$")),
    ("POST", re.compile(r"^/api/worker/restart$")),
    ("POST", re.compile(r"^/api/update/(check|download|install|cancel|restart)$")),
    ("PUT", re.compile(r"^/api/update/preferences$")),
    ("POST", re.compile(r"^/api/voices/\d+/archive$")),
    ("GET", re.compile(r"^/api/auth/users$")),
    ("POST", re.compile(r"^/api/auth/users$")),
    ("GET", re.compile(r"^/api/doctor$")),
    ("GET", re.compile(r"^/api/disk/")),
    ("POST", re.compile(r"^/api/disk/")),
]


def is_admin_only(method: str, path: str) -> bool:
    return any(m == method and rx.match(path) for m, rx in ADMIN_RULES)


class AuthStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cache: tuple[float, dict] | None = None

    def _load(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return {"secret": "", "users": {}}
        if self._cache and self._cache[0] == mtime:
            return self._cache[1]
        data = json.loads(self.path.read_text("utf-8") or "{}")
        data.setdefault("users", {})
        data.setdefault("secret", "")
        self._cache = (mtime, data)
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2))
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._cache = None

    @staticmethod
    def _hash(password: str, salt: bytes, iters: int = ITERATIONS) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters).hex()

    def enabled(self) -> bool:
        return bool(self._load()["users"])

    def list_users(self) -> list[dict]:
        return [{"name": n, "role": u["role"], "created_at": u.get("created_at")} for n, u in sorted(self._load()["users"].items())]

    @staticmethod
    def _public(name: str, u: dict) -> dict:
        user = {"name": name, "role": u["role"]}
        if u.get("must_change"):
            user["must_change"] = True      # only present while a temporary password is in force
        return user

    def set_user(self, name: str, password: str, role: str = "operator", temporary: bool = False) -> None:
        """``temporary`` stores a known starter password (the installer's ``admin``): it may be short, and the account is
        limited to changing its password until it has been changed."""
        if not USER_RE.match(name or ""):
            raise ValueError("User names are 2-40 characters: letters, digits, . _ -")
        if role not in ROLES:
            raise ValueError("Role must be admin or operator")
        if len(password or "") < (1 if temporary else 10):
            raise ValueError("Password must be at least 10 characters")
        with self._lock:
            data = dict(self._load())
            data["users"] = dict(data["users"])
            if not data["secret"]:
                data["secret"] = secrets.token_hex(32)
            salt = secrets.token_bytes(16)
            existing = data["users"].get(name, {})
            if existing.get("role") == "admin" and role != "admin" and sum(1 for u in data["users"].values() if u["role"] == "admin") == 1:
                raise ValueError("Cannot demote the only administrator")
            data["users"][name] = {"salt": salt.hex(), "hash": self._hash(password, salt), "iters": ITERATIONS, "role": role,
                                   "created_at": existing.get("created_at") or time.time(), "must_change": bool(temporary)}
            self._save(data)

    def remove_user(self, name: str) -> None:
        with self._lock:
            data = dict(self._load())
            users = dict(data["users"])
            if name not in users:
                raise ValueError(f"No such user '{name}'")
            if users[name]["role"] == "admin" and sum(1 for u in users.values() if u["role"] == "admin") == 1:
                raise ValueError("Cannot remove the only administrator")
            del users[name]
            data["users"] = users
            self._save(data)

    def verify(self, name: str, password: str) -> dict | None:
        u = self._load()["users"].get(name or "")
        salt = bytes.fromhex(u["salt"]) if u else b"\0" * 16
        got = self._hash(password or "", salt, u["iters"] if u else ITERATIONS)  # constant work for unknown users
        if u and hmac.compare_digest(got, u["hash"]):
            return self._public(name, u)
        return None

    # ---------------------------------------------------------------- sessions
    def make_token(self, user: dict, ttl: int = SESSION_TTL) -> str:
        data = self._load()
        u = data["users"][user["name"]]
        payload = {"u": user["name"], "r": user["role"], "e": int(time.time()) + ttl, "v": u["hash"][:12]}
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        sig = hmac.new(bytes.fromhex(data["secret"]), body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def parse_token(self, token: str | None) -> dict | None:
        if not token or "." not in token:
            return None
        data = self._load()
        if not data["secret"]:
            return None
        body, sig = token.rsplit(".", 1)
        want = hmac.new(bytes.fromhex(data["secret"]), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except (ValueError, json.JSONDecodeError):
            return None
        u = data["users"].get(payload.get("u"))
        # a password change (new hash) or removed user invalidates old sessions
        if not u or payload.get("e", 0) < time.time() or payload.get("v") != u["hash"][:12]:
            return None
        return self._public(payload["u"], u)


class LoginLimiter:
    """At most 6 failed logins per client address per 5 minutes."""

    def __init__(self, limit: int = 6, window: int = 300):
        self.limit, self.window = limit, window
        self.fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> int:
        with self._lock:
            t = time.time()
            recent = [x for x in self.fails.get(key, []) if t - x < self.window]
            self.fails[key] = recent
            return int(self.window - (t - recent[0])) if len(recent) >= self.limit else 0

    def fail(self, key: str) -> None:
        with self._lock:
            self.fails.setdefault(key, []).append(time.time())

    def ok(self, key: str) -> None:
        with self._lock:
            self.fails.pop(key, None)


def generate_password() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4))
