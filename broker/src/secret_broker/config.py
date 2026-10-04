"""Settings, read once from the environment that compose/secrets.yml passes in."""

from __future__ import annotations

import os
from dataclasses import dataclass

# An agent's request waits this long for the user's decision.
REQUEST_TTL_SECONDS = 600
# Shared values wait this long for the agent to fetch them (once).
RESULT_TTL_SECONDS = 1800
# More waiting requests than this are refused, which also caps push spam.
MAX_PENDING = 5
# Longest `get_result(wait_seconds=...)`; Lettuce's MCP bridge gives a tool
# call 120 s (bff/src/mcp-bridge/client.ts).
MAX_WAIT_SECONDS = 50
# The vault locks itself after this long without use.
AUTO_LOCK_SECONDS = 12 * 3600
MIN_PASSPHRASE_LENGTH = 12
# Unlock attempts per signed-in person per minute.
UNLOCK_ATTEMPTS_PER_MINUTE = 5
MAX_PURPOSE_LENGTH = 500
MAX_FIELD_VALUE = 4000


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    team_domain: str
    audience: str
    issuer: str
    allowed_users: frozenset[str]
    public_origin: str
    vapid_public_key: str
    vapid_private_key: str
    vapid_contact: str
    db_path: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)

        def required(key: str) -> str:
            value = env.get(key, "").strip()
            if not value:
                raise ConfigError(f"{key} is required")
            return value

        team = required("CF_ACCESS_TEAM_DOMAIN")
        users = frozenset(u.strip().lower() for u in required("ALLOWED_USERS").split(",") if u.strip())
        origin = required("PUBLIC_ORIGIN").rstrip("/")
        if not origin.startswith("https://"):
            raise ConfigError("PUBLIC_ORIGIN must be https://")
        return cls(
            team_domain=team,
            audience=required("CF_ACCESS_AUD"),
            issuer=env.get("CF_ACCESS_ISSUER", "").strip() or f"https://{team}.cloudflareaccess.com",
            allowed_users=users,
            public_origin=origin,
            vapid_public_key=required("BROKER_VAPID_PUBLIC_KEY"),
            vapid_private_key=required("BROKER_VAPID_PRIVATE_KEY"),
            vapid_contact=required("PUSH_VAPID_CONTACT_EMAIL"),
            db_path=env.get("BROKER_DB_PATH", "/data/vault.db"),
        )
