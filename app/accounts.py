"""Visitor accounts for the public sandbox: email + password, opaque server-side sessions, daily chat quota.

No email verification (no mail relay is wired up); abuse is bounded by per-IP signup limits, per-user daily
chat quota and the global hourly cap instead. Passwords are scrypt-hashed; session tokens are stored only as
SHA-256 hashes, so a database leak does not yield usable sessions.
"""
import hashlib
import hmac
import re
import secrets
import time

from .store import Store

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,120}\.[^@\s]{2,}$")
SESSION_TTL = 24 * 3600


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, store: Store):
        self.s = store

    def signup(self, email: str, password: str, ip: str) -> str:
        email = (email or "").strip().lower()
        if not EMAIL_RE.match(email):
            raise ValueError("Enter a valid email address")
        if not 8 <= len(password or "") <= 72:
            raise ValueError("Password must be 8-72 characters")
        salt = secrets.token_bytes(16)
        try:
            self.s._q("INSERT INTO users VALUES (?,?,?,?,?)", (email, salt, _hash(password, salt), time.time(), ip))
        except Exception:  # sqlite3.IntegrityError
            raise ValueError("That email is already registered, log in instead")
        self.s.audit(email, "signup", None, {})
        return self._new_session(email)

    def login(self, email: str, password: str) -> str | None:
        email = (email or "").strip().lower()
        row = self.s._q("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not row or not hmac.compare_digest(_hash(password or "", row["salt"]), row["pw_hash"]):
            return None
        return self._new_session(email)

    def _new_session(self, email: str) -> str:
        tok = secrets.token_urlsafe(32)
        self.s._q("INSERT INTO sessions VALUES (?,?,?)", (_th(tok), email, time.time() + SESSION_TTL))
        return tok

    def user_for(self, token: str | None) -> str | None:
        if not token:
            return None
        row = self.s._q("SELECT email, expires_at FROM sessions WHERE token_hash=?", (_th(token),)).fetchone()
        return row["email"] if row and row["expires_at"] > time.time() else None

    def logout(self, token: str) -> None:
        self.s._q("DELETE FROM sessions WHERE token_hash=?", (_th(token),))

    def charge_chat(self, email: str, daily_limit: int) -> bool:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        self.s._q("INSERT OR IGNORE INTO usage VALUES (?,?,0)", (email, day))
        cur = self.s._q("UPDATE usage SET chats=chats+1 WHERE email=? AND day=? AND chats<?", (email, day, daily_limit))
        return cur.rowcount == 1

    def remaining(self, email: str, daily_limit: int) -> int:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        row = self.s._q("SELECT chats FROM usage WHERE email=? AND day=?", (email, day)).fetchone()
        return daily_limit - (row["chats"] if row else 0)
