from __future__ import annotations

import time
import secrets
import hashlib
from typing import List, Optional, Tuple

import pymysql

from .config import get_settings, Settings


class UserStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._init_db()
        self._seed_admin()

    def _connect(self):
        return pymysql.connect(
            host=self.settings.mysql_host,
            port=self.settings.mysql_port,
            user=self.settings.mysql_user,
            password=self.settings.mysql_password,
            database=self.settings.mysql_db,
            charset="utf8mb4",
            autocommit=False,
        )

    def _init_db(self):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username VARCHAR(255) PRIMARY KEY,
                password_hash VARCHAR(255) NOT NULL,
                must_change_password TINYINT DEFAULT 1,
                role VARCHAR(32) DEFAULT 'student',
                created_at DOUBLE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                token VARCHAR(64) PRIMARY KEY,
                username VARCHAR(255) NOT NULL,
                role VARCHAR(32) DEFAULT 'student',
                created_at DOUBLE,
                INDEX idx_username (username),
                CONSTRAINT fk_token_user FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        conn.commit()
        conn.close()

    def _seed_admin(self):
        """Seed admin user into DB if admin_password is provided and user does not exist."""
        if not self.settings.admin_password:
            return
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE username=%s", (self.settings.admin_username,))
        exists = cur.fetchone()
        if exists:
            conn.close()
            return
        now = time.time()
        cur.execute(
            "INSERT INTO users (username, password_hash, must_change_password, role, created_at) VALUES (%s,%s,%s,%s,%s)",
            (
                self.settings.admin_username,
                self._hash_password(self.settings.admin_password),
                1,
                "admin",
                now,
            ),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _hash_password(password: str) -> str:
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    def create_user(self, username: str, password: str, must_change: bool = True, role: str = "student") -> bool:
        now = time.time()
        conn = None
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO users (username, password_hash, must_change_password, role, created_at) VALUES (%s,%s,%s,%s,%s)",
                (username, self._hash_password(password), 1 if must_change else 0, role, now),
            )
            conn.commit()
            return True
        except pymysql.err.IntegrityError:
            return False
        finally:
            if conn:
                conn.close()

    def batch_create(self, usernames: List[str], password_prefix: str = "hziee", role: str = "student") -> Tuple[List[str], List[str]]:
        created: List[str] = []
        skipped: List[str] = []
        for uid in usernames:
            pwd = f"{password_prefix}{uid}"
            ok = self.create_user(uid, pwd, must_change=True, role=role)
            if ok:
                created.append(uid)
            else:
                skipped.append(uid)
        return created, skipped

    def _issue_token(self, username: str, role: str) -> str:
        token = secrets.token_hex(16)
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tokens (token, username, role, created_at) VALUES (%s,%s,%s,%s)",
            (token, username, role, time.time()),
        )
        conn.commit()
        conn.close()
        return token

    def authenticate(self, username: str, password: str) -> Tuple[bool, Optional[str], bool, Optional[str]]:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT password_hash, must_change_password, role FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return False, None, False, None
        stored_hash, must_change, role = row
        if stored_hash != self._hash_password(password):
            return False, None, False, None
        token = self._issue_token(username, role or "student")
        return True, token, bool(must_change), role or "student"

    def set_password(self, username: str, new_password: str):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET password_hash=%s, must_change_password=0 WHERE username=%s",
            (self._hash_password(new_password), username),
        )
        conn.commit()
        conn.close()

    def validate_token(self, token: str) -> Optional[Tuple[str, str]]:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT username, role FROM tokens WHERE token=%s", (token,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        username = row[0]
        role = row[1] or "student"
        return username, role

    def list_users(self) -> List[str]:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT username FROM users ORDER BY username ASC")
        rows = cur.fetchall()
        conn.close()
        return [r[0] for r in rows]
