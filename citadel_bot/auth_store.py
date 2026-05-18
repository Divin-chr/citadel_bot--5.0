"""
Tenant authentication and encrypted credential storage.

This module intentionally keeps identity data separate from trading ledgers.
It uses SQLite for a portable control-plane store, PBKDF2-HMAC for passwords,
hashed session tokens, account lockout, and Fernet encryption for MetaApi
tokens. Set CITADEL_SECRET_KEY in production; local development gets an
ignored key file under data/.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from citadel_bot.config.config import BotConfig

PBKDF2_ITERATIONS = 390_000
SESSION_TTL_SECONDS = 60 * 60 * 12
LOCKOUT_SECONDS = 15 * 60
MAX_FAILED_LOGINS = 5


@dataclass
class AuthUser:
    user_id: int
    email: str
    display_name: str
    role: str


class AuthError(Exception):
    pass


class AuthenticationStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path or os.getenv("CITADEL_AUTH_DB_PATH", "data/auth.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._secret = None
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'trader',
                    failed_login_count INTEGER NOT NULL DEFAULT 0,
                    locked_until REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_credentials (
                    user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                    metaapi_account_id TEXT NOT NULL,
                    metaapi_token_ciphertext TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_configs (
                    user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                    config_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL
                );
                """
            )

    def _load_secret(self) -> bytes:
        if self._secret is not None:
            return self._secret
        raw_key = os.getenv("CITADEL_SECRET_KEY", "").strip()
        if not raw_key:
            key_path = Path(os.getenv("CITADEL_SECRET_KEY_FILE", "data/.citadel_secret"))
            if key_path.exists():
                raw_key = key_path.read_text(encoding="utf-8").strip()
            else:
                raw_key = self.generate_secret_key()
                key_path.parent.mkdir(parents=True, exist_ok=True)
                key_path.write_text(raw_key, encoding="utf-8")
        try:
            self._secret = base64.urlsafe_b64decode(raw_key.encode("ascii"))
        except Exception as exc:
            raise AuthError("CITADEL_SECRET_KEY must be a valid urlsafe base64 key.") from exc
        if len(self._secret) < 32:
            raise AuthError("CITADEL_SECRET_KEY must decode to at least 32 bytes.")
        return self._secret

    def _encrypt(self, value: str) -> str:
        key = self._load_secret()
        nonce = secrets.token_bytes(16)
        plaintext = value.encode("utf-8")
        ciphertext = self._xor_stream(plaintext, key, nonce)
        tag = hmac.new(self._mac_key(key), nonce + ciphertext, hashlib.sha256).digest()
        payload = nonce + tag + ciphertext
        return "v1:" + base64.urlsafe_b64encode(payload).decode("ascii")

    def _decrypt(self, value: str) -> str:
        if not value.startswith("v1:"):
            try:
                from cryptography.fernet import Fernet

                raw_key = os.getenv("CITADEL_SECRET_KEY", "").strip()
                return Fernet(raw_key.encode("ascii")).decrypt(value.encode("ascii")).decode("utf-8")
            except Exception as exc:
                raise AuthError("Stored credential could not be decrypted.") from exc
        key = self._load_secret()
        try:
            payload = base64.urlsafe_b64decode(value[3:].encode("ascii"))
            nonce, tag, ciphertext = payload[:16], payload[16:48], payload[48:]
            expected = hmac.new(self._mac_key(key), nonce + ciphertext, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, expected):
                raise AuthError("Stored credential failed integrity verification.")
            return self._xor_stream(ciphertext, key, nonce).decode("utf-8")
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError("Stored credential could not be decrypted.") from exc

    @staticmethod
    def _mac_key(key: bytes) -> bytes:
        return hmac.new(key, b"citadel-auth-store-mac", hashlib.sha256).digest()

    @staticmethod
    def _enc_key(key: bytes) -> bytes:
        return hmac.new(key, b"citadel-auth-store-enc", hashlib.sha256).digest()

    @classmethod
    def _xor_stream(cls, data: bytes, key: bytes, nonce: bytes) -> bytes:
        enc_key = cls._enc_key(key)
        output = bytearray()
        counter = 0
        while len(output) < len(data):
            block = hmac.new(enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
            output.extend(block)
            counter += 1
        return bytes(a ^ b for a, b in zip(data, output))

    @staticmethod
    def generate_secret_key() -> str:
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")

    @staticmethod
    def _normalize_email(email: str) -> str:
        return email.strip().lower()

    @staticmethod
    def _validate_password(password: str):
        if len(password) < 12:
            raise AuthError("Password must be at least 12 characters.")
        checks = [
            bool(re.search(r"[a-z]", password)),
            bool(re.search(r"[A-Z]", password)),
            bool(re.search(r"\d", password)),
            bool(re.search(r"[^A-Za-z0-9]", password)),
        ]
        if sum(checks) < 3:
            raise AuthError("Password must include at least 3 of: lowercase, uppercase, number, symbol.")

    @staticmethod
    def _hash_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return "pbkdf2_sha256${}${}${}".format(
            PBKDF2_ITERATIONS,
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )

    @staticmethod
    def _verify_password(password: str, stored: str) -> bool:
        try:
            scheme, iterations, salt_b64, digest_b64 = stored.split("$", 3)
            if scheme != "pbkdf2_sha256":
                return False
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(digest_b64)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
            return hmac.compare_digest(actual, expected)
        except Exception:
            return False

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_user(self, email: str, password: str, display_name: str = "") -> AuthUser:
        email = self._normalize_email(email)
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise AuthError("Enter a valid email address.")
        self._validate_password(password)
        now = time.time()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO users (email, display_name, password_hash, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (email, display_name.strip() or email, self._hash_password(password), now, now),
                )
                user_id = int(cur.lastrowid)
                conn.execute(
                    "INSERT INTO user_configs (user_id, config_json, updated_at) VALUES (?, ?, ?)",
                    (user_id, json.dumps(BotConfig().to_dict()), now),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthError("An account with that email already exists.") from exc
        return AuthUser(user_id=user_id, email=email, display_name=display_name.strip() or email, role="trader")

    def authenticate(self, email: str, password: str) -> tuple[AuthUser, str]:
        email = self._normalize_email(email)
        now = time.time()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if not row:
                raise AuthError("Invalid email or password.")
            if row["locked_until"] and float(row["locked_until"]) > now:
                raise AuthError("Account is temporarily locked. Try again later.")
            if not self._verify_password(password, row["password_hash"]):
                failed = int(row["failed_login_count"] or 0) + 1
                locked_until = now + LOCKOUT_SECONDS if failed >= MAX_FAILED_LOGINS else None
                conn.execute(
                    "UPDATE users SET failed_login_count = ?, locked_until = ?, updated_at = ? WHERE user_id = ?",
                    (failed, locked_until, now, row["user_id"]),
                )
                raise AuthError("Invalid email or password.")
            conn.execute(
                "UPDATE users SET failed_login_count = 0, locked_until = NULL, updated_at = ? WHERE user_id = ?",
                (now, row["user_id"]),
            )
            token = secrets.token_urlsafe(32)
            conn.execute(
                """
                INSERT INTO sessions (user_id, token_hash, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (row["user_id"], self._hash_token(token), now, now + SESSION_TTL_SECONDS),
            )
        return self._row_to_user(row), token

    def get_user_by_session(self, token: str) -> Optional[AuthUser]:
        if not token:
            return None
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT u.* FROM sessions s
                JOIN users u ON u.user_id = s.user_id
                WHERE s.token_hash = ?
                  AND s.revoked_at IS NULL
                  AND s.expires_at > ?
                """,
                (self._hash_token(token), now),
            ).fetchone()
        return self._row_to_user(row) if row else None

    def revoke_session(self, token: str):
        if not token:
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ?",
                (time.time(), self._hash_token(token)),
            )

    def save_credentials(self, user_id: int, metaapi_token: str, metaapi_account_id: str):
        if not metaapi_token.strip() or not metaapi_account_id.strip():
            raise AuthError("MetaApi token and account ID are required.")
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_credentials (user_id, metaapi_account_id, metaapi_token_ciphertext, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    metaapi_account_id = excluded.metaapi_account_id,
                    metaapi_token_ciphertext = excluded.metaapi_token_ciphertext,
                    updated_at = excluded.updated_at
                """,
                (user_id, metaapi_account_id.strip(), self._encrypt(metaapi_token.strip()), now),
            )

    def get_credentials(self, user_id: int) -> Optional[Dict[str, str]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT metaapi_account_id, metaapi_token_ciphertext FROM user_credentials WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "metaapi_account_id": row["metaapi_account_id"],
            "metaapi_token": self._decrypt(row["metaapi_token_ciphertext"]),
        }

    def save_config(self, user_id: int, config: BotConfig):
        data = config.to_dict(include_secrets=False)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_configs (user_id, config_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (user_id, json.dumps(data), now),
            )

    def get_config(self, user_id: int) -> BotConfig:
        with self._connect() as conn:
            row = conn.execute("SELECT config_json FROM user_configs WHERE user_id = ?", (user_id,)).fetchone()
        config = BotConfig.from_dict(json.loads(row["config_json"]) if row else {}, apply_environment=True)
        # Tenant trading credentials must come from the encrypted tenant store,
        # never from process-wide environment variables.
        config.metaapi_token = ""
        config.metaapi_account_id = ""
        creds = self.get_credentials(user_id)
        if creds:
            config.metaapi_token = creds["metaapi_token"]
            config.metaapi_account_id = creds["metaapi_account_id"]
        config.user_id = user_id
        config.data_dir = str(Path("data") / f"user_{user_id}")
        config.log_dir = str(Path("logs") / f"user_{user_id}")
        return config

    def has_credentials(self, user_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM user_credentials WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None

    @staticmethod
    def _row_to_user(row: Optional[sqlite3.Row]) -> AuthUser:
        if row is None:
            raise AuthError("User not found.")
        return AuthUser(
            user_id=int(row["user_id"]),
            email=row["email"],
            display_name=row["display_name"],
            role=row["role"],
        )


_store: Optional[AuthenticationStore] = None


def get_auth_store() -> AuthenticationStore:
    global _store
    if _store is None:
        _store = AuthenticationStore()
    return _store
