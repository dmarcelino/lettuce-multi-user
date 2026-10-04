"""The ASGI app: MCP at /mcp (internal only; the tunnel forwards /secrets/ alone)
and the vault UI at /secrets/ (every route needs a verified Access JWT, and all
but setup/unlock/notifications an unlocked vault)."""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any

import anyio
from jinja2 import Environment, PackageLoader, select_autoescape
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request as HttpRequest
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from . import icons
from .auth import Authenticator, AuthError
from .config import UNLOCK_ATTEMPTS_PER_MINUTE
from .kinds import KINDS, preview
from .mcp_tools import build_server
from .service import Broker
from .vault import ASK, AUTO, Field, Item, Locked, Vault, VaultError, build_fields

log = logging.getLogger("secret_broker.ui")

SECURITY_HEADERS = {
    "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; form-action 'self'; "
    "base-uri 'none'; object-src 'none'",
    "x-content-type-options": "nosniff",
    # same-origin, not no-referrer: with no-referrer browsers send `Origin: null`
    # on form posts, which check_origin must refuse.
    "referrer-policy": "same-origin",
    "cache-control": "no-store",
    "x-frame-options": "DENY",
}
CUSTOM_ROWS = 2

Handler = Callable[[HttpRequest, str], Awaitable[Response]]


def _fmt_time(ts: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


class RateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic):
        self._per_minute = per_minute
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= self._per_minute:
            return False
        hits.append(now)
        return True


def _safe_next(path: Any) -> str:
    return path if isinstance(path, str) and path.startswith("/secrets/") and "//" not in path else "/secrets/"


def create_app(broker: Broker, authenticator: Authenticator, vapid_public_key: str) -> Starlette:
    env = Environment(loader=PackageLoader("secret_broker", "templates"), autoescape=select_autoescape(default=True))
    env.filters["time"] = _fmt_time
    static = resources.files("secret_broker") / "static"
    vault: Vault = broker.vault
    unlock_limiter = RateLimiter(UNLOCK_ATTEMPTS_PER_MINUTE)

    def render(name: str, email: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        unlocked = vault.is_unlocked()
        html = env.get_template(name).render(
            email=email,
            csrf=authenticator.csrf_token(email) if email else "",
            vapid_public_key=vapid_public_key,
            unlocked=unlocked,
            pending_count=len(broker.pending()) if unlocked else 0,
            **ctx,
        )
        return HTMLResponse(html, status_code=status, headers=SECURITY_HEADERS)

    def error(message: str, status: int, email: str = "") -> HTMLResponse:
        return render("error.html", email, status=status, message=message)

    def back(path: str) -> RedirectResponse:
        return RedirectResponse(path, status_code=303, headers=SECURITY_HEADERS)

    def page(
        handler: Handler, post: bool = False, unlocked: bool = True
    ) -> Callable[[HttpRequest], Awaitable[Response]]:
        async def endpoint(request: HttpRequest) -> Response:
            try:
                email = await authenticator.email(request.headers)
            except AuthError as e:
                log.info("refused %s %s reason=auth", request.method, request.url.path)
                return error(str(e), e.status)
            if post:
                try:
                    authenticator.check_origin(request.headers)
                    if request.headers.get("content-type", "").startswith("application/json"):
                        token = request.headers.get("x-csrf-token")
                    else:
                        token = (await request.form()).get("csrf")
                    authenticator.check_csrf(email, token if isinstance(token, str) else None)
                except AuthError as e:
                    return error(str(e), e.status, email)
            if unlocked:
                if not vault.is_setup():
                    return render("setup.html", email)
                if not vault.is_unlocked():
                    target = request.url.path if request.method == "GET" else "/secrets/"
                    return render("unlock.html", email, next=target)
            vault.touch()
            try:
                return await handler(request, email)
            except Locked:
                return render("unlock.html", email, next="/secrets/")
            except VaultError as e:
                return error(str(e), 400, email)

        return endpoint

    # ── setup, unlock, lock, settings ────────────────────────────────────────
    async def setup(request: HttpRequest, email: str) -> Response:
        form = await request.form()
        passphrase, confirm = str(form.get("passphrase", "")), str(form.get("confirm", ""))
        if passphrase != confirm:
            return render("setup.html", email, status=400, problem="The two passphrases are different.")
        try:
            vault.setup(passphrase)
        except VaultError as e:
            return render("setup.html", email, status=400, problem=str(e))
        vault.audit(email, "created the vault")
        return back("/secrets/items")

    async def unlock(request: HttpRequest, email: str) -> Response:
        form = await request.form()
        nxt = _safe_next(form.get("next"))
        if not unlock_limiter.allow(email):
            return render("unlock.html", email, status=429, next=nxt, problem="Too many attempts. Wait a minute.")
        try:
            vault.unlock(str(form.get("passphrase", "")))
        except VaultError as e:
            vault.audit(email, "failed unlock")
            return render("unlock.html", email, status=403, next=nxt, problem=str(e))
        vault.audit(email, "unlocked")
        return back(nxt)

    async def lock(request: HttpRequest, email: str) -> Response:
        vault.lock()
        vault.audit(email, "locked")
        return back("/secrets/")

    async def settings_page(request: HttpRequest, email: str) -> Response:
        return render("settings.html", email)

    async def change_passphrase(request: HttpRequest, email: str) -> Response:
        form = await request.form()
        new, confirm = str(form.get("new", "")), str(form.get("confirm", ""))
        if new != confirm:
            raise VaultError("the two new passphrases are different")
        vault.change_passphrase(str(form.get("old", "")), new)
        vault.audit(email, "changed the passphrase")
        return render("settings.html", email, notice="Passphrase changed.")

    async def reset(request: HttpRequest, email: str) -> Response:
        form = await request.form()
        if form.get("confirm") != "DELETE":
            raise VaultError("type DELETE to erase the vault")
        vault.reset()
        vault.audit(email, "erased the vault")
        return back("/secrets/")

    # ── approvals ────────────────────────────────────────────────────────────
    async def approvals(request: HttpRequest, email: str) -> Response:
        return render("approvals.html", email, pending=broker.pending(), recent=broker.recent())

    async def request_detail(request: HttpRequest, email: str) -> Response:
        req = broker.request(request.path_params["rid"])
        if req is None:
            return error(
                "This request is no longer here: it was answered, expired, or the vault restarted.", 404, email
            )
        return render("request.html", email, req=req)

    async def decide(request: HttpRequest, email: str) -> Response:
        rid = request.path_params["rid"]
        form = await request.form()
        action = form.get("action")
        if action not in ("share", "deny"):
            raise VaultError("invalid answer")
        broker.decide(rid, action == "share", [str(f) for f in form.getlist("fields")], email)
        return back(f"/secrets/r/{rid}")

    # ── items ────────────────────────────────────────────────────────────────
    def fields_from(form: Any, kind: str, current: Item | None) -> tuple[Field, ...]:
        values = {spec.key: str(form.get(f"f_{spec.key}", "")) for spec in KINDS[kind].fields}
        if current is not None:
            # Hidden fields are never sent back to the browser: blank keeps them.
            for spec in KINDS[kind].fields:
                old = current.field(spec.key)
                if (
                    spec.sensitive
                    and not values[spec.key].strip()
                    and old is not None
                    and form.get(f"clear_{spec.key}") != "on"
                ):
                    values[spec.key] = old.value
        custom: list[tuple[str, str, bool]] = []
        i = 0
        while f"c_label_{i}" in form:
            label, value = str(form.get(f"c_label_{i}", "")), str(form.get(f"c_value_{i}", ""))
            sensitive = form.get(f"c_sensitive_{i}") == "on"
            old_key = str(form.get(f"c_key_{i}", ""))
            if form.get(f"c_remove_{i}") != "on":
                old = current.field(old_key) if current and old_key else None
                if sensitive and not value.strip() and old is not None:
                    value = old.value
                custom.append((label, value, sensitive))
            i += 1
        return build_fields(kind, values, custom)

    def share_mode_from(form: Any) -> str:
        return AUTO if form.get("share_mode") == AUTO else ASK

    def custom_fields(item: Item | None) -> list[Field]:
        if item is None:
            return []
        known = {spec.key for spec in KINDS[item.kind].fields}
        return [f for f in item.fields if f.key not in known]

    async def items_page(request: HttpRequest, email: str) -> Response:
        items = [(i, preview(i.kind, list(i.fields))) for i in vault.items()]
        return render("items.html", email, items=items, kinds=KINDS)

    async def item_new(request: HttpRequest, email: str) -> Response:
        kind = request.query_params.get("kind", "")
        if kind not in KINDS:
            return render("choose_kind.html", email, kinds=KINDS)
        return render("item_form.html", email, kind=KINDS[kind], item=None, custom=[], blank_rows=CUSTOM_ROWS)

    async def item_create(request: HttpRequest, email: str) -> Response:
        form = await request.form()
        kind = str(form.get("kind", ""))
        if kind not in KINDS:
            raise VaultError("unknown item type")
        item_id = vault.add_item(
            kind,
            str(form.get("name", "")),
            str(form.get("usage", "")),
            fields_from(form, kind, None),
            share_mode_from(form),
        )
        vault.audit(email, "added item", item_id, str(form.get("name", "")))
        return back(f"/secrets/items/{item_id}")

    def get_item(request: HttpRequest) -> Item:
        item = vault.item(request.path_params["iid"])
        if item is None:
            raise VaultError("no such item")
        return item

    async def item_view(request: HttpRequest, email: str) -> Response:
        item = get_item(request)
        return render("item.html", email, item=item, revealed=False, preview=preview(item.kind, list(item.fields)))

    async def item_show(request: HttpRequest, email: str) -> Response:
        item = get_item(request)
        vault.audit(email, "viewed hidden fields", item.id, item.name)
        return render("item.html", email, item=item, revealed=True, preview=preview(item.kind, list(item.fields)))

    async def item_edit(request: HttpRequest, email: str) -> Response:
        item = get_item(request)
        return render(
            "item_form.html",
            email,
            kind=KINDS[item.kind],
            item=item,
            custom=custom_fields(item),
            blank_rows=CUSTOM_ROWS,
        )

    async def item_update(request: HttpRequest, email: str) -> Response:
        item = get_item(request)
        form = await request.form()
        vault.update_item(
            item.id,
            str(form.get("name", "")),
            str(form.get("usage", "")),
            fields_from(form, item.kind, item),
            share_mode_from(form),
        )
        vault.audit(email, "changed item", item.id, str(form.get("name", "")))
        return back(f"/secrets/items/{item.id}")

    async def item_delete(request: HttpRequest, email: str) -> Response:
        item = get_item(request)
        vault.delete_item(item.id)
        vault.audit(email, "deleted item", item.id, item.name)
        return back("/secrets/items")

    # ── notifications ────────────────────────────────────────────────────────
    async def notifications(request: HttpRequest, email: str) -> Response:
        mine = [s for s in vault.subscriptions() if s["email"] == email]
        return render("notifications.html", email, devices=len(mine))

    def bad(message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=400, headers=SECURITY_HEADERS)

    async def push_subscribe(request: HttpRequest, email: str) -> Response:
        try:
            data = json.loads(await request.body())
            endpoint, keys = data["endpoint"], data["keys"]
            p256dh, auth_key = keys["p256dh"], keys["auth"]
        except (ValueError, KeyError, TypeError):
            return bad("invalid subscription")
        if not (isinstance(endpoint, str) and endpoint.startswith("https://") and len(endpoint) < 2048):
            return bad("invalid endpoint")
        if not (isinstance(p256dh, str) and isinstance(auth_key, str) and len(p256dh) < 256 and len(auth_key) < 64):
            return bad("invalid keys")
        vault.add_subscription(endpoint, p256dh, auth_key, email)
        return JSONResponse({"ok": True}, headers=SECURITY_HEADERS)

    async def push_unsubscribe(request: HttpRequest, email: str) -> Response:
        try:
            endpoint = json.loads(await request.body())["endpoint"]
        except (ValueError, KeyError, TypeError):
            return bad("invalid request")
        if isinstance(endpoint, str):
            vault.remove_subscription(endpoint)
        return JSONResponse({"ok": True}, headers=SECURITY_HEADERS)

    async def push_test(request: HttpRequest, email: str) -> Response:
        sent = await broker.pusher.send(
            {
                "title": "Test notification",
                "body": "Vault requests will arrive like this.",
                "url": "/secrets/",
                "tag": "test",
            }
        )
        return JSONResponse({"delivered": sent}, headers=SECURITY_HEADERS)

    # ── audit ────────────────────────────────────────────────────────────────
    async def audit_page(request: HttpRequest, email: str) -> Response:
        return render("audit.html", email, entries=vault.audit_log())

    # ── static: behind Access too (fetched with the Access cookie) ───────────
    def static_file(name: str, media_type: str) -> Handler:
        body = (static / name).read_bytes()

        async def serve(request: HttpRequest, email: str) -> Response:
            return Response(body, media_type=media_type, headers={**SECURITY_HEADERS, "cache-control": "no-cache"})

        return serve

    def icon(size: int) -> Handler:
        async def serve(request: HttpRequest, email: str) -> Response:
            return Response(
                icons.png(size), media_type="image/png", headers={**SECURITY_HEADERS, "cache-control": "max-age=86400"}
            )

        return serve

    async def healthz(request: HttpRequest) -> Response:
        return PlainTextResponse("ok")

    open_page = {"unlocked": False}
    ui = [
        Route("/", page(approvals)),
        Route("/setup", page(setup, post=True, **open_page), methods=["POST"]),
        Route("/unlock", page(unlock, post=True, **open_page), methods=["POST"]),
        Route("/lock", page(lock, post=True, **open_page), methods=["POST"]),
        Route("/settings", page(settings_page)),
        Route("/settings/passphrase", page(change_passphrase, post=True), methods=["POST"]),
        Route("/settings/reset", page(reset, post=True), methods=["POST"]),
        Route("/r/{rid}", page(request_detail)),
        Route("/r/{rid}/decide", page(decide, post=True), methods=["POST"]),
        Route("/items", page(items_page)),
        Route("/items", page(item_create, post=True), methods=["POST"]),
        Route("/items/new", page(item_new)),
        Route("/items/{iid}", page(item_view)),
        Route("/items/{iid}", page(item_update, post=True), methods=["POST"]),
        Route("/items/{iid}/edit", page(item_edit)),
        Route("/items/{iid}/show", page(item_show, post=True), methods=["POST"]),
        Route("/items/{iid}/delete", page(item_delete, post=True), methods=["POST"]),
        Route("/notifications", page(notifications, **open_page)),
        Route("/push/subscribe", page(push_subscribe, post=True, **open_page), methods=["POST"]),
        Route("/push/unsubscribe", page(push_unsubscribe, post=True, **open_page), methods=["POST"]),
        Route("/push/test", page(push_test, post=True, **open_page), methods=["POST"]),
        Route("/audit", page(audit_page)),
        Route("/sw.js", page(static_file("sw.js", "text/javascript"), **open_page)),
        Route("/app.js", page(static_file("app.js", "text/javascript"), **open_page)),
        Route("/app.css", page(static_file("app.css", "text/css"), **open_page)),
        Route(
            "/manifest.webmanifest", page(static_file("manifest.webmanifest", "application/manifest+json"), **open_page)
        ),
        Route("/icon-192.png", page(icon(192), **open_page)),
        Route("/icon-512.png", page(icon(512), **open_page)),
    ]

    mcp_server = build_server(broker)
    mcp_app = mcp_server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # The bridge calls http://secret-broker:8000/mcp from the stack network
        # and sends no Origin; anything else is refused.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["secret-broker:*", "secret-broker"],
            allowed_origins=[],
        ),
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp_app.router.lifespan_context(mcp_app), anyio.create_task_group() as tg:
            broker.bind_task_group(tg)

            async def housekeeping() -> None:
                while True:
                    broker.expire()
                    vault.is_unlocked()  # applies the auto-lock
                    await anyio.sleep(30)

            tg.start_soon(housekeeping)
            try:
                yield
            finally:
                broker.bind_task_group(None)
                tg.cancel_scope.cancel()

    async def secrets_root(request: HttpRequest) -> Response:
        return RedirectResponse("/secrets/", status_code=308)

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/secrets", secrets_root),
            Mount("/secrets", routes=ui),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )
