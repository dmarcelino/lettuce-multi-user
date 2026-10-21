"""What agents can do with the vault, shared by the MCP tools and the web UI.

An agent lists secrets (names, types and public fields; never private values),
asks for the private fields of one secret with a purpose, and gets them once
the user has approved, or straight away for secrets the user set to "share
without asking". Requests and results live only in memory: a restart locks the
vault anyway.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from secrets import token_urlsafe
from typing import Any

import anyio

from .config import MAX_PENDING, MAX_PURPOSE_LENGTH, MAX_WAIT_SECONDS, REQUEST_TTL_SECONDS, RESULT_TTL_SECONDS
from .kinds import preview
from .logs import SECRET_FILTER
from .push import Pusher
from .vault import AUTO, Locked, Secret, Vault, VaultError

log = logging.getLogger("secret_broker")

PENDING, DONE, DENIED, EXPIRED = "pending", "done", "denied", "expired"
# Pushes asking to unlock are sent at most this often.
UNLOCK_NOTICE_SECONDS = 300


@dataclass
class Request:
    id: str
    secret_id: str
    secret_name: str
    secret_kind: str
    details: dict[str, str]  # the secret's public fields, as context for the user
    fields: tuple[str, ...]  # private field keys asked for, in the secret's order
    labels: dict[str, str]
    purpose: str
    preview: str
    created_at: int
    expires_at: int
    status: str = PENDING
    decided_by: str | None = None
    decided_at: int | None = None
    shared: tuple[str, ...] = ()
    values: dict[str, str] | None = field(default=None, repr=False)


class Broker:
    def __init__(self, vault: Vault, pusher: Pusher, public_origin: str):
        self.vault = vault
        self.pusher = pusher
        self._origin = public_origin
        self._requests: dict[str, Request] = {}
        self._tasks: anyio.abc.TaskGroup | None = None
        self._last_unlock_notice = -UNLOCK_NOTICE_SECONDS

    def bind_task_group(self, tg: anyio.abc.TaskGroup | None) -> None:
        self._tasks = tg

    def _spawn_push(self, payload: dict[str, str]) -> None:
        if self._tasks is None:
            raise RuntimeError("broker task group is not running")
        self._tasks.start_soon(self._push, payload)

    async def _push(self, payload: dict[str, str]) -> None:
        try:
            await self.pusher.send(payload)
        except Exception as e:
            log.warning("push failed error=%s", type(e).__name__)

    def url(self, path: str) -> str:
        return f"{self._origin}/secrets/{path}"

    def _locked_answer(self) -> dict[str, Any]:
        now = self.vault.now()
        if now - self._last_unlock_notice >= UNLOCK_NOTICE_SECONDS:
            self._last_unlock_notice = now
            self._spawn_push(
                {
                    "title": "Vault locked",
                    "body": "An agent wants to use your vault. Unlock it to continue.",
                    "url": "/secrets/",
                    "tag": "vault-locked",
                }
            )
        return {
            "locked": True,
            "message": "The user's vault is locked. They were notified; ask them to unlock it "
            f"at {self.url('')} and then try again.",
        }

    # ── housekeeping ─────────────────────────────────────────────────────────
    def expire(self) -> None:
        now = self.vault.now()
        for rid, req in list(self._requests.items()):
            if req.status == PENDING and req.expires_at <= now:
                req.status = EXPIRED
            if req.status != PENDING and (req.decided_at or req.created_at) + RESULT_TTL_SECONDS <= now:
                del self._requests[rid]

    def pending(self) -> list[Request]:
        self.expire()
        return sorted((r for r in self._requests.values() if r.status == PENDING), key=lambda r: r.created_at)

    def recent(self) -> list[Request]:
        self.expire()
        done = (r for r in self._requests.values() if r.status != PENDING)
        return sorted(done, key=lambda r: r.decided_at or r.created_at, reverse=True)

    def request(self, rid: str) -> Request | None:
        self.expire()
        return self._requests.get(rid)

    # ── agent side ───────────────────────────────────────────────────────────
    def list_secrets(self) -> dict[str, Any]:
        try:
            found = self.vault.secrets()
        except Locked:
            return self._locked_answer()
        return {
            "secrets": [
                {
                    "id": s.id,
                    "name": s.name,
                    "type": s.kind_label,
                    "use_for": s.usage,
                    "details": {f.label: f.value for f in s.public_fields},
                    "private_fields": [{"key": f.key, "label": f.label} for f in s.private_fields],
                    "needs_approval": s.share_mode != AUTO,
                }
                for s in found
            ]
        }

    @staticmethod
    def _values(secret: Secret, keys: tuple[str, ...]) -> dict[str, str]:
        return {k: f.value for k in keys if (f := secret.field(k)) is not None}

    async def request_secret(self, secret_id: str, purpose: str, fields: list[str] | None = None) -> dict[str, Any]:
        if not isinstance(secret_id, str) or not isinstance(purpose, str):
            return {"status": "refused", "error": "secret_id and purpose must be strings"}
        if fields is not None and not isinstance(fields, list):
            return {"status": "refused", "error": "fields must be a list of private field keys"}
        purpose = purpose.strip()
        if not purpose or len(purpose) > MAX_PURPOSE_LENGTH:
            return {"status": "refused", "error": f"say what the values are for (1-{MAX_PURPOSE_LENGTH} characters)"}
        try:
            secret = self.vault.secret(secret_id)
        except Locked:
            return self._locked_answer()
        if secret is None:
            return {"status": "refused", "error": f"no secret '{secret_id}' (see list_secrets)"}
        private = {f.key for f in secret.private_fields}
        wanted = private if not fields else {f for f in fields if isinstance(f, str)}
        public = wanted & {f.key for f in secret.public_fields}
        if public:
            return {"status": "refused", "error": f"{', '.join(sorted(public))}: already in list_secrets"}
        if not wanted or wanted - private:
            known = ", ".join(f.key for f in secret.private_fields)
            return {"status": "refused", "error": f"ask for one or more of this secret's private fields: {known}"}
        keys = tuple(f.key for f in secret.private_fields if f.key in wanted)
        labels = ", ".join(secret.field(k).label for k in keys)  # type: ignore[union-attr]

        if secret.share_mode == AUTO:
            values = self._values(secret, keys)
            with SECRET_FILTER.guarding(values.values()):
                self.vault.audit(
                    "agent", "shared without asking", secret.id, f"{secret.name}: {labels}. Purpose: {purpose}"
                )
                log.info("shared without asking secret=%s fields=%s", secret.id, ",".join(keys))
            self._spawn_push(
                {
                    "title": "Shared with an agent",
                    "body": f"{secret.name}: {labels}",
                    "url": "/secrets/audit",
                    "tag": f"vault-shared-{secret.id}",
                }
            )
            return {"status": DONE, "secret": secret.name, "values": values}

        self.expire()
        if sum(1 for r in self._requests.values() if r.status == PENDING) >= MAX_PENDING:
            return {"status": "refused", "error": "too many requests are waiting; ask the user to answer them first"}
        now = self.vault.now()
        req = Request(
            id=token_urlsafe(16),
            secret_id=secret.id,
            secret_name=secret.name,
            secret_kind=secret.kind_label,
            details={f.label: f.value for f in secret.public_fields},
            fields=keys,
            labels={k: secret.field(k).label for k in keys},  # type: ignore[union-attr]
            purpose=purpose,
            preview=preview(secret.kind, list(secret.fields)),
            created_at=now,
            expires_at=now + REQUEST_TTL_SECONDS,
        )
        self._requests[req.id] = req
        log.info("pending id=%s secret=%s fields=%s", req.id, secret.id, ",".join(keys))
        self._spawn_push(
            {
                "title": "Approve sharing",
                "body": f"{secret.name}: {labels}",
                "url": f"/secrets/r/{req.id}",
                "tag": f"vault-request-{req.id}",
            }
        )
        return {
            "id": req.id,
            "status": PENDING,
            "approve_url": self.url(f"r/{req.id}"),
            "message": "Waiting for the user's approval; they were notified. "
            f"Call get_result with this id (wait_seconds up to {MAX_WAIT_SECONDS} waits for the answer).",
        }

    async def get_result(self, rid: str, wait_seconds: float = 0) -> dict[str, Any]:
        wait = max(0.0, min(float(wait_seconds or 0), MAX_WAIT_SECONDS))
        with anyio.move_on_after(wait):
            while (req := self.request(rid) if isinstance(rid, str) else None) and req.status == PENDING:
                await anyio.sleep(0.5)
        req = self.request(rid) if isinstance(rid, str) else None
        if req is None:
            return {"status": "unknown", "error": f"no request '{rid}' (requests are forgotten after a restart)"}
        if req.status == PENDING:
            return {"id": rid, "status": PENDING, "approve_url": self.url(f"r/{rid}")}
        if req.status in (DENIED, EXPIRED):
            return {"id": rid, "status": req.status}
        if req.values is None:
            return {"id": rid, "status": DONE, "note": "the values were already returned"}
        values, req.values = req.values, None
        result: dict[str, Any] = {"id": rid, "status": DONE, "secret": req.secret_name, "values": values}
        withheld = [req.labels[k] for k in req.fields if k not in req.shared]
        if withheld:
            result["withheld"] = withheld
        return result

    # ── user side ────────────────────────────────────────────────────────────
    def decide(self, rid: str, approve: bool, fields: list[str], email: str) -> Request:
        req = self.request(rid)
        if req is None or req.status != PENDING:
            raise VaultError("this request is no longer waiting (answered, expired, or the vault restarted)")
        now = self.vault.now()
        if not approve:
            req.status, req.decided_by, req.decided_at = DENIED, email, now
            self.vault.audit(email, "denied", req.secret_id, f"{req.secret_name}. Purpose: {req.purpose}")
            return req
        shared = tuple(k for k in req.fields if k in set(fields))
        if not shared:
            raise VaultError("tick at least one field to share, or deny")
        secret = self.vault.secret(req.secret_id)
        if secret is None:
            raise VaultError("that secret was deleted")
        values = self._values(secret, shared)
        if len(values) != len(shared):
            raise VaultError("that secret changed; ask the agent to request it again")
        labels = ", ".join(req.labels[k] for k in shared)
        with SECRET_FILTER.guarding(values.values()):
            self.vault.audit(
                email, "shared on approval", req.secret_id, f"{req.secret_name}: {labels}. Purpose: {req.purpose}"
            )
            log.info("approved id=%s secret=%s fields=%s by=%s", rid, req.secret_id, ",".join(shared), email)
        req.status, req.decided_by, req.decided_at, req.shared, req.values = DONE, email, now, shared, values
        return req
