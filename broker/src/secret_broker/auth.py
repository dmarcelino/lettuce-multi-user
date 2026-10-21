"""Who is asking: the Cloudflare Access JWT, verified like upstream's BFF does
(bff/src/auth/cf-access.ts), and CSRF tokens for every form.

Agents reach the broker over the Docker network without passing Access, so
they can send any header they like. Only a JWT signed by the team's Access
keys, for this stack's application, carrying an allowed email, counts.
The unsigned Cf-Access-Authenticated-User-Email header is never read.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from typing import Any

import anyio
import jwt

from .config import Settings

JWT_HEADER = "cf-access-jwt-assertion"


class AuthError(Exception):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


KeyResolver = Callable[[str], Any]


def jwks_resolver(team_domain: str) -> KeyResolver:
    client = jwt.PyJWKClient(
        f"https://{team_domain}.cloudflareaccess.com/cdn-cgi/access/certs", cache_keys=True, lifespan=3600
    )

    def resolve(token: str) -> Any:
        return client.get_signing_key_from_jwt(token).key

    return resolve


class Authenticator:
    def __init__(self, settings: Settings, key_resolver: KeyResolver | None = None):
        self._settings = settings
        self._resolve = key_resolver or jwks_resolver(settings.team_domain)
        # Forms carry HMAC(email); the key lives only in this process, so a
        # restart invalidates open forms (the page says to reload).
        self._csrf_key = secrets.token_bytes(32)

    async def email(self, headers: Any) -> str:
        token = headers.get(JWT_HEADER)
        if not token:
            raise AuthError("sign in through the stack's address; direct access is not allowed")
        try:
            key = await anyio.to_thread.run_sync(self._resolve, token)
        except jwt.PyJWKClientError as e:
            # Unreachable or unknown signing keys: fail closed.
            raise AuthError("could not verify the sign-in right now", status=503) from e
        except jwt.PyJWTError as e:
            raise AuthError("invalid sign-in token") from e
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._settings.audience,
                issuer=self._settings.issuer,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except jwt.PyJWTError as e:
            raise AuthError("invalid sign-in token") from e
        email = claims.get("email")
        if not isinstance(email, str) or not email.strip():
            raise AuthError("the sign-in token has no email")
        email = email.strip().lower()
        if email not in self._settings.allowed_users:
            raise AuthError("this account may not use this stack")
        return email

    def csrf_token(self, email: str) -> str:
        return hmac.new(self._csrf_key, email.encode(), hashlib.sha256).hexdigest()

    def check_csrf(self, email: str, token: str | None) -> None:
        if not token or not hmac.compare_digest(self.csrf_token(email), token):
            raise AuthError("this form has expired; reload the page and try again")

    def check_origin(self, headers: Any) -> None:
        """Browsers send Origin on every POST; it must be this stack's own."""
        if headers.get("origin") != self._settings.public_origin:
            raise AuthError("cross-site request refused")
