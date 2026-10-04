"""The encrypted vault: one SQLite file on the broker-only volume.

On disk in plaintext: secret ids, kinds, sharing modes, timestamps, audit
actions and push subscriptions. Secret names, usage notes, fields and audit
details are encrypted (crypto.py). The data key lives only in this process,
from unlock until lock, auto-lock or restart.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from secrets import token_hex
from typing import Any

from . import crypto
from .config import AUTO_LOCK_SECONDS, MAX_FIELD_VALUE, MIN_PASSPHRASE_LENGTH
from .kinds import KINDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_meta (k TEXT PRIMARY KEY, v BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS secrets (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  share_mode TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  blob BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id TEXT PRIMARY KEY,
  at INTEGER NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  item_id TEXT, -- the secret's id; column name kept from the first schema
  blob BLOB
);
CREATE INDEX IF NOT EXISTS audit_at ON audit(at);
CREATE TABLE IF NOT EXISTS push_subscriptions (
  endpoint TEXT PRIMARY KEY,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  email TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
"""

ASK, AUTO = "ask", "auto"
KEY_AAD = b"vault-data-key"


class VaultError(Exception):
    """Safe to show the user."""


class Locked(VaultError):
    def __init__(self) -> None:
        super().__init__("the vault is locked")


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    value: str
    sensitive: bool = False
    multiline: bool = False


@dataclass(frozen=True)
class Secret:
    id: str
    kind: str
    share_mode: str
    name: str
    usage: str
    fields: tuple[Field, ...]
    created_at: int
    updated_at: int

    def field(self, key: str) -> Field | None:
        return next((f for f in self.fields if f.key == key), None)

    @property
    def public_fields(self) -> tuple[Field, ...]:
        return tuple(f for f in self.fields if not f.sensitive)

    @property
    def private_fields(self) -> tuple[Field, ...]:
        return tuple(f for f in self.fields if f.sensitive)

    @property
    def kind_label(self) -> str:
        return KINDS[self.kind].label if self.kind in KINDS else self.kind


@dataclass(frozen=True)
class AuditEntry:
    at: int
    actor: str
    action: str
    secret_id: str | None
    detail: str


@dataclass
class _Unlocked:
    key: bytes
    last_used: float = field(default=0.0)


def _now() -> float:
    return time.time()


def check_passphrase(passphrase: str) -> str:
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise VaultError(f"the passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters")
    return passphrase


def build_fields(kind: str, values: dict[str, str]) -> tuple[Field, ...]:
    """The kind's fields in order; empty optional fields are dropped."""
    if kind not in KINDS:
        raise VaultError("unknown type of secret")
    out: list[Field] = []
    for spec in KINDS[kind].fields:
        value = values.get(spec.key, "").strip()
        if not value:
            if spec.required:
                raise VaultError(f"{spec.label} is required")
            continue
        if len(value) > MAX_FIELD_VALUE:
            raise VaultError(f"{spec.label} is longer than {MAX_FIELD_VALUE} characters")
        out.append(Field(spec.key, spec.label, value, spec.sensitive, spec.multiline))
    return tuple(out)


class Vault:
    def __init__(
        self,
        path: str,
        clock: Callable[[], float] = _now,
        kdf: crypto.KdfParams | None = None,
        auto_lock_seconds: float = AUTO_LOCK_SECONDS,
    ):
        self._clock = clock
        self._kdf = kdf or crypto.KdfParams()
        self._auto_lock = auto_lock_seconds
        self._state: _Unlocked | None = None
        self._lock = threading.RLock()
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            old = os.umask(0o077)
            try:
                self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            finally:
                os.umask(old)
            os.chmod(path, 0o600)
        else:
            self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            # Deleted rows are overwritten, not just unlinked from the b-tree.
            self._db.execute("PRAGMA secure_delete = ON")

    def now(self) -> int:
        return int(self._clock())

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    def _meta(self, k: str) -> bytes | None:
        rows = self._q("SELECT v FROM vault_meta WHERE k = ?", (k,))
        return bytes(rows[0]["v"]) if rows else None

    # ── lock state ───────────────────────────────────────────────────────────
    def is_setup(self) -> bool:
        return self._meta("wrapped_key") is not None

    def is_unlocked(self) -> bool:
        with self._lock:
            if self._state and self._clock() - self._state.last_used > self._auto_lock:
                self._state = None
            return self._state is not None

    def touch(self) -> None:
        with self._lock:
            if self.is_unlocked() and self._state:
                self._state.last_used = self._clock()

    def _key(self) -> bytes:
        # Deliberately no touch(): only the person using the UI keeps the vault
        # open, so an agent asking again and again cannot stop the auto-lock.
        with self._lock:
            if not self.is_unlocked() or self._state is None:
                raise Locked()
            return self._state.key

    def setup(self, passphrase: str) -> None:
        check_passphrase(passphrase)
        with self._lock:
            if self.is_setup():
                raise VaultError("the vault already exists")
            key = crypto.new_key()
            self._store_wrapped(key, passphrase)
            self._state = _Unlocked(key, self._clock())

    def _store_wrapped(self, key: bytes, passphrase: str) -> None:
        salt = crypto.new_salt()
        kek = crypto.derive_key(passphrase, salt, self._kdf)
        wrapped = crypto.seal(kek, key, KEY_AAD)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for k, v in (
                    ("salt", salt),
                    ("kdf", json.dumps(self._kdf.to_dict()).encode()),
                    ("wrapped_key", wrapped),
                ):
                    self._db.execute("INSERT OR REPLACE INTO vault_meta (k, v) VALUES (?, ?)", (k, v))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _unwrap(self, passphrase: str) -> bytes:
        salt, kdf, wrapped = self._meta("salt"), self._meta("kdf"), self._meta("wrapped_key")
        if salt is None or kdf is None or wrapped is None:
            raise VaultError("the vault has not been created yet")
        params = crypto.KdfParams(**json.loads(kdf))
        try:
            return crypto.open_sealed(crypto.derive_key(passphrase, salt, params), wrapped, KEY_AAD)
        except crypto.CryptoError as e:
            raise VaultError("wrong passphrase") from e

    def unlock(self, passphrase: str) -> None:
        key = self._unwrap(passphrase)
        with self._lock:
            self._state = _Unlocked(key, self._clock())

    def lock(self) -> None:
        with self._lock:
            self._state = None

    def change_passphrase(self, old: str, new: str) -> None:
        check_passphrase(new)
        key = self._unwrap(old)
        self._store_wrapped(key, new)
        with self._lock:
            self._state = _Unlocked(key, self._clock())

    def reset(self) -> None:
        """Forgotten passphrase: everything encrypted is gone for good."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for table in ("secrets", "audit", "vault_meta"):
                    self._db.execute(f"DELETE FROM {table}")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("VACUUM")
            self._state = None

    # ── secrets ──────────────────────────────────────────────────────────────
    def _decode(self, row: sqlite3.Row, key: bytes) -> Secret:
        data = json.loads(crypto.open_sealed(key, bytes(row["blob"]), f"secret:{row['id']}".encode()))
        return Secret(
            id=row["id"],
            kind=row["kind"],
            share_mode=row["share_mode"],
            name=data["name"],
            usage=data["usage"],
            fields=tuple(Field(**f) for f in data["fields"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _encode(self, secret_id: str, name: str, usage: str, fields: tuple[Field, ...], key: bytes) -> bytes:
        data = {
            "name": name,
            "usage": usage,
            "fields": [f.__dict__ for f in fields],
        }
        return crypto.seal(key, json.dumps(data).encode(), f"secret:{secret_id}".encode())

    @staticmethod
    def _check(kind: str, name: str, share_mode: str) -> None:
        if kind not in KINDS:
            raise VaultError("unknown type of secret")
        if not name.strip() or len(name) > 80:
            raise VaultError("the name must be 1 to 80 characters")
        if share_mode not in (ASK, AUTO):
            raise VaultError("invalid sharing choice")
        if share_mode == AUTO and not KINDS[kind].auto_allowed:
            raise VaultError(f"{KINDS[kind].label} secrets always ask before an agent uses them")

    def secrets(self) -> list[Secret]:
        key = self._key()
        found = [self._decode(r, key) for r in self._q("SELECT * FROM secrets")]
        return sorted(found, key=lambda x: (x.kind_label, x.name.lower()))

    def secret(self, secret_id: str) -> Secret | None:
        key = self._key()
        rows = self._q("SELECT * FROM secrets WHERE id = ?", (secret_id,))
        return self._decode(rows[0], key) if rows else None

    def add_secret(self, kind: str, name: str, usage: str, fields: tuple[Field, ...], share_mode: str = ASK) -> str:
        self._check(kind, name, share_mode)
        key = self._key()
        secret_id = token_hex(8)
        now = self.now()
        self._x(
            "INSERT INTO secrets (id, kind, share_mode, created_at, updated_at, blob) VALUES (?, ?, ?, ?, ?, ?)",
            (secret_id, kind, share_mode, now, now, self._encode(secret_id, name.strip(), usage.strip(), fields, key)),
        )
        return secret_id

    def update_secret(self, secret_id: str, name: str, usage: str, fields: tuple[Field, ...], share_mode: str) -> None:
        current = self.secret(secret_id)
        if current is None:
            raise VaultError("no such secret")
        self._check(current.kind, name, share_mode)
        key = self._key()
        self._x(
            "UPDATE secrets SET share_mode = ?, updated_at = ?, blob = ? WHERE id = ?",
            (share_mode, self.now(), self._encode(secret_id, name.strip(), usage.strip(), fields, key), secret_id),
        )

    def delete_secret(self, secret_id: str) -> None:
        self._key()
        if not self._x("DELETE FROM secrets WHERE id = ?", (secret_id,)):
            raise VaultError("no such secret")

    # ── audit ────────────────────────────────────────────────────────────────
    def audit(self, actor: str, action: str, secret_id: str | None = None, detail: str = "") -> None:
        """Action and actor are plaintext; the detail is encrypted, so it is
        dropped when the vault is locked (only lock events happen then)."""
        entry_id = token_hex(8)
        blob = None
        if detail and self.is_unlocked():
            blob = crypto.seal(self._key(), detail.encode(), f"audit:{entry_id}".encode())
        self._x(
            "INSERT INTO audit (id, at, actor, action, item_id, blob) VALUES (?, ?, ?, ?, ?, ?)",
            (entry_id, self.now(), actor, action, secret_id, blob),
        )

    def audit_log(self, limit: int = 200) -> list[AuditEntry]:
        key = self._key()
        out = []
        for r in self._q("SELECT * FROM audit ORDER BY at DESC, rowid DESC LIMIT ?", (limit,)):
            detail = ""
            if r["blob"] is not None:
                detail = crypto.open_sealed(key, bytes(r["blob"]), f"audit:{r['id']}".encode()).decode()
            out.append(AuditEntry(r["at"], r["actor"], r["action"], r["item_id"], detail))
        return out

    # ── push subscriptions (not secret: device endpoints) ───────────────────
    def add_subscription(self, endpoint: str, p256dh: str, auth: str, email: str) -> None:
        self._x(
            "INSERT OR REPLACE INTO push_subscriptions (endpoint, p256dh, auth, email, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (endpoint, p256dh, auth, email, self.now()),
        )

    def remove_subscription(self, endpoint: str) -> None:
        self._x("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

    def subscriptions(self) -> list[Any]:
        return self._q("SELECT * FROM push_subscriptions ORDER BY created_at")
