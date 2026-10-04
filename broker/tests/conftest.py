from __future__ import annotations

import time
from typing import Any

import anyio
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from secret_broker.auth import Authenticator
from secret_broker.config import Settings
from secret_broker.crypto import KdfParams
from secret_broker.push import Pusher
from secret_broker.service import Broker
from secret_broker.vault import Vault, build_fields

# Deterministic fakes; tests that can see broker output assert they never leak.
SECRET = "TEST_SECRET_DO_NOT_USE_938742"
STREET = "Rua Falsa 123"
CARD = "4111111111114242"
PASSPHRASE = "correct horse battery staple"
EMAIL = "alfred@gmail.com"
ORIGIN = "https://alfred.example.com"
FAST_KDF = KdfParams(n=2**10, r=8, p=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        team_domain="team1",
        audience="aud1",
        issuer="https://team1.cloudflareaccess.com",
        allowed_users=frozenset({EMAIL}),
        public_origin=ORIGIN,
        vapid_public_key="BPublicKeyForTests",
        vapid_private_key="private-key-for-tests",
        vapid_contact="ops@example.com",
        db_path=":memory:",
    )


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def vault_path(tmp_path) -> str:
    return str(tmp_path / "vault.db")


@pytest.fixture
def vault(vault_path, clock) -> Vault:
    v = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    v.setup(PASSPHRASE)
    return v


def add_address(vault: Vault, share_mode: str = "ask") -> str:
    fields = build_fields(
        "address", {"full_name": "Alfred Test", "street": STREET, "city": "Lisboa", "country": "Portugal"}, []
    )
    return vault.add_item("address", "Home address", "deliveries", fields, share_mode)


def add_card(vault: Vault) -> str:
    fields = build_fields(
        "card",
        {"cardholder": "Alfred Test", "number": CARD, "expiry": "12/30", "security_code": "123"},
        [("Bank PIN hint", SECRET, True)],
    )
    return vault.add_item("card", "Visa", "", fields)


class FakeSender:
    def __init__(self, fail_status: int | None = None):
        self.calls: list[dict[str, Any]] = []
        self.fail_status = fail_status

    def __call__(self, **kw: Any) -> None:
        self.calls.append(kw)
        if self.fail_status:
            from pywebpush import WebPushException

            class Resp:
                status_code = self.fail_status

            raise WebPushException("push failed", response=Resp())


def make_broker(settings: Settings, vault: Vault, sender: FakeSender | None = None) -> Broker:
    return Broker(vault, Pusher(settings, vault, sender or FakeSender()), settings.public_origin)


async def with_broker(broker: Broker, fn):
    async with anyio.create_task_group() as tg:
        broker.bind_task_group(tg)
        try:
            return await fn()
        finally:
            await anyio.sleep(0.01)  # let queued pushes run
            tg.cancel_scope.cancel()


# ── Access JWTs ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_jwt(key: Any, **over: Any) -> str:
    now = int(time.time())
    claims = {
        "aud": ["aud1"],
        "iss": "https://team1.cloudflareaccess.com",
        "email": EMAIL,
        "iat": now,
        "exp": now + 3600,
        "sub": "user-1",
    }
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256")


@pytest.fixture
def authenticator(settings: Settings, signing_key: Any) -> Authenticator:
    public = signing_key.public_key()
    return Authenticator(settings, key_resolver=lambda token: public)
