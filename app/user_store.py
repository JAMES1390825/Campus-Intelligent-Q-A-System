from __future__ import annotations

import time
import secrets
import hashlib
import logging
from typing import Dict, List, Optional, Tuple

import pymysql

from .config import get_settings, Settings


class InMemoryUserStore:
    """内存用户存储：用于本地开发无 MySQL 时暂存账号、Token。"""

    def __init__(self):
        self.users: Dict[str, Dict[str, str]] = {}  # username -> {password_hash, must_change, role, created_at}
        self.tokens: Dict[str, Tuple[str, str, bool]] = {}  # token -> (username, role, must_change)

    @staticmethod
    def _hash_password(password: str) -> str:
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    def seed_admin(self, username: str, password: str):
        if username in self.users:
            return
        self.users[username] = {
            "password_hash": self._hash_password(password),
            "must_change_password": "1",
            "role": "admin",
            "created_at": str(time.time()),
        }

    def create_user(self, username: str, password: str, must_change: bool, role: str) -> bool:
        if username in self.users:
            return False
        self.users[username] = {
            "password_hash": self._hash_password(password),
            "must_change_password": "1" if must_change else "0",
            "role": role,
            "created_at": str(time.time()),
        }
        return True

    def issue_token(self, username: str, role: str, must_change: bool = False) -> str:
        token = secrets.token_hex(16)
        self.tokens[token] = (username, role, must_change)
        return token

    def authenticate(self, username: str, password: str) -> Tuple[bool, Optional[str], bool, Optional[str]]:
        user = self.users.get(username)
        if not user:
            return False, None, False, None
        if user["password_hash"] != self._hash_password(password):
            return False, None, False, None
        must_change = user.get("must_change_password", "0") == "1"
        token = self.issue_token(username, user.get("role", "student"), must_change)
        return True, token, must_change, user.get("role", "student")

    def set_password(self, username: str, new_password: str):
        if username not in self.users:
            return
        self.users[username]["password_hash"] = self._hash_password(new_password)
        self.users[username]["must_change_password"] = "0"

    def validate_token(self, token: str) -> Optional[Tuple[str, str, bool]]:
        return self.tokens.get(token)

    def list_users(self) -> List[str]:
        return sorted(self.users.keys())


class UserStore:
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._backend = "mysql"
        self.memory_store = InMemoryUserStore()
        try:
            self._init_db()
            self._seed_admin()
        except Exception as exc:
            logging.warning("MySQL 不可用，回退到内存用户存储: %s", exc)
            self._backend = "memory"
            self.memory_store.seed_admin(self.settings.admin_username, self.settings.admin_password or "changeme")

    def _connect(self):
        return pymysql.connect(
            host=self.settings.mysql_host,
            port=self.settings.mysql_port,
            user=self.settings.mysql_user,
            password=self.settings.mysql_password or "",
            database=self.settings.mysql_db,
            charset="utf8mb4",
            autocommit=False,
            connect_timeout=3,
            read_timeout=3,
            write_timeout=3,
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
        self._backend = "mysql"

    def _seed_admin(self):
        """Seed admin user into DB if admin_password is provided and user does not exist."""
        if not self.settings.admin_password:
            return
        if self._backend == "memory":
            self.memory_store.seed_admin(self.settings.admin_username, self.settings.admin_password)
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
        if self._backend == "memory":
            return self.memory_store.create_user(username, password, must_change, role)
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
        if self._backend == "memory":
            return self.memory_store.issue_token(username, role)
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
        if self._backend == "memory":
            return self.memory_store.authenticate(username, password)
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
        if self._backend == "memory":
            self.memory_store.set_password(username, new_password)
            return
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET password_hash=%s, must_change_password=0 WHERE username=%s",
            (self._hash_password(new_password), username),
        )
        conn.commit()
        conn.close()

    def validate_token(self, token: str) -> Optional[Tuple[str, str, bool]]:
        if self._backend == "memory":
            rec = self.memory_store.validate_token(token)
            if not rec:
                return None
            username, role, must_change = rec
            return username, role, must_change
        conn = self._connect()
        cur = conn.cursor()
        # 通过 token 查用户，并带出 must_change 状态
        cur.execute(
            """
            SELECT t.username, t.role, u.must_change_password
            FROM tokens t
            JOIN users u ON u.username = t.username
            WHERE t.token=%s
            """,
            (token,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        username, role, must_change = row
        role = role or "student"
        return username, role, bool(must_change)

    def list_users(self) -> List[str]:
        if self._backend == "memory":
            return self.memory_store.list_users()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT username FROM users ORDER BY username ASC")
        rows = cur.fetchall()
        conn.close()
        return [r[0] for r in rows]
