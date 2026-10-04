"""What agents can do with the vault, shared by the MCP tools and the web UI.

An agent lists items (names and field labels, never values), asks for some
fields of one item with a purpose, and gets the values once the user has
approved, or straight away for items the user set to "share without asking".
Requests and results live only in memory: a restart locks the vault anyway.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

import anyio

from .config import MAX_PENDING, MAX_PURPOSE_LENGTH, MAX_WAIT_SECONDS, REQUEST_TTL_SECONDS, RESULT_TTL_SECONDS
from .kinds import preview
from .logs import SECRET_FILTER
from .push import Pusher
from .vault import AUTO, Item, Locked, Vault, VaultError

log = logging.getLogger("secret_broker")

PENDING, DONE, DENIED, EXPIRED = "pending", "done", "denied", "expired"
# Pushes asking to unlock are sent at most this often.
UNLOCK_NOTICE_SECONDS = 300


@dataclass
class Request:
    id: str
    item_id: str
    item_name: str
    item_kind: str
    fields: tuple[str, ...]  # keys, in the item's order
    labels: dict[str, str]
    sensitive: frozenset[str]
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
    def list_items(self) -> dict[str, Any]:
        try:
            items = self.vault.items()
        except Locked:
            return self._locked_answer()
        return {
            "items": [
                {
                    "id": i.id,
                    "name": i.name,
                    "type": i.kind_label,
                    "use_for": i.usage,
                    "fields": [{"key": f.key, "label": f.label} for f in i.fields],
                    "needs_approval": i.share_mode != AUTO,
                }
                for i in items
            ]
        }

    @staticmethod
    def _values(item: Item, keys: tuple[str, ...]) -> dict[str, str]:
        return {k: f.value for k in keys if (f := item.field(k)) is not None}

    async def request_item(self, item_id: str, fields: list[str], purpose: str) -> dict[str, Any]:
        if not isinstance(item_id, str) or not isinstance(purpose, str) or not isinstance(fields, list):
            return {"status": "refused", "error": "item_id and purpose must be strings, fields a list"}
        purpose = purpose.strip()
        if not purpose or len(purpose) > MAX_PURPOSE_LENGTH:
            return {"status": "refused", "error": f"say what the values are for (1-{MAX_PURPOSE_LENGTH} characters)"}
        try:
            item = self.vault.item(item_id)
        except Locked:
            return self._locked_answer()
        if item is None:
            return {"status": "refused", "error": f"no item '{item_id}' (see list_items)"}
        wanted = {f for f in fields if isinstance(f, str)}
        unknown = wanted - {f.key for f in item.fields}
        if unknown or not wanted:
            known = ", ".join(f.key for f in item.fields)
            return {"status": "refused", "error": f"ask for one or more of this item's fields: {known}"}
        keys = tuple(f.key for f in item.fields if f.key in wanted)
        labels = ", ".join(item.field(k).label for k in keys)  # type: ignore[union-attr]

        if item.share_mode == AUTO:
            values = self._values(item, keys)
            with SECRET_FILTER.guarding(values.values()):
                self.vault.audit(
                    "agent", "shared without asking", item.id, f"{item.name}: {labels}. Purpose: {purpose}"
                )
                log.info("shared without asking item=%s fields=%s", item.id, ",".join(keys))
            self._spawn_push(
                {
                    "title": "Shared with an agent",
                    "body": f"{item.name}: {labels}",
                    "url": "/secrets/audit",
                    "tag": f"vault-shared-{item.id}",
                }
            )
            return {"status": DONE, "item": item.name, "values": values}

        self.expire()
        if sum(1 for r in self._requests.values() if r.status == PENDING) >= MAX_PENDING:
            return {"status": "refused", "error": "too many requests are waiting; ask the user to answer them first"}
        now = self.vault.now()
        req = Request(
            id=secrets.token_urlsafe(16),
            item_id=item.id,
            item_name=item.name,
            item_kind=item.kind_label,
            fields=keys,
            labels={k: item.field(k).label for k in keys},  # type: ignore[union-attr]
            sensitive=frozenset(k for k in keys if item.field(k).sensitive),  # type: ignore[union-attr]
            purpose=purpose,
            preview=preview(item.kind, list(item.fields)),
            created_at=now,
            expires_at=now + REQUEST_TTL_SECONDS,
        )
        self._requests[req.id] = req
        log.info("pending id=%s item=%s fields=%s", req.id, item.id, ",".join(keys))
        self._spawn_push(
            {
                "title": "Approve sharing",
                "body": f"{item.name}: {labels}",
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
        result: dict[str, Any] = {"id": rid, "status": DONE, "item": req.item_name, "values": values}
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
            self.vault.audit(email, "denied", req.item_id, f"{req.item_name}. Purpose: {req.purpose}")
            return req
        shared = tuple(k for k in req.fields if k in set(fields))
        if not shared:
            raise VaultError("tick at least one field to share, or deny")
        item = self.vault.item(req.item_id)
        if item is None:
            raise VaultError("that item was deleted")
        values = self._values(item, shared)
        if len(values) != len(shared):
            raise VaultError("that item changed; ask the agent to request it again")
        labels = ", ".join(req.labels[k] for k in shared)
        with SECRET_FILTER.guarding(values.values()):
            self.vault.audit(
                email, "shared on approval", req.item_id, f"{req.item_name}: {labels}. Purpose: {req.purpose}"
            )
            log.info("approved id=%s item=%s fields=%s by=%s", rid, req.item_id, ",".join(shared), email)
        req.status, req.decided_by, req.decided_at, req.shared, req.values = DONE, email, now, shared, values
        return req
