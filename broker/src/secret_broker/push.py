"""Web Push to the user's devices.

Payloads are encrypted end to end (RFC 8291) and name only the secret and its
field labels, never a value. A failed push never decides anything: a request
just waits until it expires.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import anyio
from pywebpush import WebPushException, webpush

from .config import Settings
from .vault import Vault

log = logging.getLogger("secret_broker.push")

Sender = Callable[..., Any]


class Pusher:
    def __init__(self, settings: Settings, vault: Vault, sender: Sender = webpush):
        self._settings = settings
        self._vault = vault
        self._send = sender

    def _send_one(self, sub: Any, payload: dict[str, str]) -> bool:
        """True when delivered. Gone subscriptions are deleted."""
        host = urlsplit(sub["endpoint"]).hostname
        try:
            self._send(
                subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                data=json.dumps(payload),
                vapid_private_key=self._settings.vapid_private_key,
                vapid_claims={"sub": f"mailto:{self._settings.vapid_contact}"},
                ttl=600,
                timeout=10,
            )
            return True
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                self._vault.remove_subscription(sub["endpoint"])
                log.info("push subscription gone, removed host=%s", host)
            else:
                log.warning("push failed host=%s status=%s", host, status)
        except Exception as e:  # network errors from requests
            log.warning("push failed host=%s error=%s", host, type(e).__name__)
        return False

    def send_sync(self, payload: dict[str, str]) -> int:
        delivered = 0
        for sub in self._vault.subscriptions():
            # Someone removed from ALLOWED_USERS gets nothing more.
            if sub["email"] not in self._settings.allowed_users:
                continue
            delivered += self._send_one(sub, payload)
        return delivered

    async def send(self, payload: dict[str, str]) -> int:
        return await anyio.to_thread.run_sync(self.send_sync, payload)
