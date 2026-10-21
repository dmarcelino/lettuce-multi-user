import json
import logging
import re
import time

import jwt
import pytest
from starlette.testclient import TestClient

from secret_broker.app import create_app
from secret_broker.auth import Authenticator
from secret_broker.vault import Vault

from .conftest import (
    ADDRESS_TEXT,
    CARD,
    EMAIL,
    FAST_KDF,
    ORIGIN,
    PASSPHRASE,
    SECRET,
    STREET,
    FakeSender,
    make_broker,
    make_jwt,
)

MCP = "http://secret-broker:8000/mcp"


@pytest.fixture
def env(settings, authenticator, signing_key, vault_path, clock):
    vault = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    sender = FakeSender()
    broker = make_broker(settings, vault, sender)
    app = create_app(broker, authenticator, settings.vapid_public_key)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        client.headers["cf-access-jwt-assertion"] = make_jwt(signing_key)
        yield client, vault, sender


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" content="([0-9a-f]+)"', html).group(1)


def post(client, path, data, csrf=None, origin=ORIGIN):
    form = dict(data)
    form["csrf"] = csrf if csrf is not None else csrf_of(client.get("/secrets/notifications").text)
    return client.post(path, data=form, headers={"origin": origin} if origin else {})


def setup_vault(client):
    r = post(client, "/secrets/setup", {"passphrase": PASSPHRASE, "confirm": PASSPHRASE})
    assert r.status_code == 303, r.text


ADDRESS = {"kind": "address", "name": "Home address", "f_text": ADDRESS_TEXT, "share_mode": "ask"}
CARD_FORM = {
    "kind": "card",
    "name": "Visa",
    "f_cardholder": "Alfred Test",
    "f_number": CARD,
    "f_expiry": "12/30",
    "f_security_code": "123",
    "f_notes": SECRET,
}


def add(client, form) -> str:
    r = post(client, "/secrets/vault", form)
    assert r.status_code == 303, r.text
    return r.headers["location"].rsplit("/", 1)[1]


def mcp(client, method, params=None, host_url=MCP):
    return client.post(
        host_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-06-18",
            "cf-access-jwt-assertion": "",
        },
    )


def call_tool(client, name, args):
    r = mcp(client, "tools/call", {"name": name, "arguments": args})
    assert r.status_code == 200, r.text
    return r.json()["result"]["structuredContent"], r.text


# ── authentication ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "token",
    [
        None,
        "not-a-jwt",
        "other-key",
        {"aud": ["other-app"]},
        {"iss": "https://evil.cloudflareaccess.com"},
        {"email": "mallory@example.com"},
        {"email": None},
        {"exp": int(time.time()) - 10},
    ],
)
def test_ui_requires_valid_access_jwt(env, signing_key, other_key, token):
    client = env[0]
    if token is None:
        value = ""
    elif token == "other-key":
        value = make_jwt(other_key)
    elif isinstance(token, dict):
        value = make_jwt(signing_key, **token)
    else:
        value = token
    for path in ["/secrets/", "/secrets/vault", "/secrets/audit", "/secrets/sw.js", "/secrets/notifications"]:
        assert client.get(path, headers={"cf-access-jwt-assertion": value}).status_code == 403, path


def test_unsigned_email_header_is_ignored(env):
    r = env[0].get("/secrets/", headers={"cf-access-jwt-assertion": "", "cf-access-authenticated-user-email": EMAIL})
    assert r.status_code == 403


def test_jwks_unavailable_fails_closed(settings, signing_key, vault_path, clock):
    def broken(token):
        raise jwt.PyJWKClientError("unreachable")

    vault = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    app = create_app(make_broker(settings, vault), Authenticator(settings, key_resolver=broken), "k")
    with TestClient(app, base_url=ORIGIN) as client:
        r = client.get("/secrets/", headers={"cf-access-jwt-assertion": make_jwt(signing_key)})
    assert r.status_code == 503


def test_security_headers_and_static(env):
    client = env[0]
    r = client.get("/secrets/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    # Regression: with "no-referrer" browsers send `Origin: null` on form posts,
    # and check_origin refused every form ("cross-site request refused").
    assert r.headers["referrer-policy"] == "same-origin"
    assert client.get("/secrets/sw.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/secrets/manifest.webmanifest").json()["name"] == "Lettuce Vault"
    assert client.get("/secrets/icon-192.png").content.startswith(b"\x89PNG")


# ── setup, lock, unlock ──────────────────────────────────────────────────────


def test_first_visit_asks_to_create_the_vault(env):
    client, vault = env[0], env[1]
    assert "Create your vault" in client.get("/secrets/vault").text
    r = post(client, "/secrets/setup", {"passphrase": PASSPHRASE, "confirm": "something else entirely"})
    assert r.status_code == 400 and not vault.is_setup()
    r = post(client, "/secrets/setup", {"passphrase": "short", "confirm": "short"})
    assert r.status_code == 400 and not vault.is_setup()
    setup_vault(client)
    assert vault.is_unlocked()
    assert "Add a secret" in client.get("/secrets/vault").text


def test_locked_pages_ask_to_unlock_and_return(env):
    client, vault = env[0], env[1]
    setup_vault(client)
    secret = add(client, ADDRESS)
    post(client, "/secrets/lock", {})
    page = client.get(f"/secrets/vault/{secret}")
    assert "Your vault is locked" in page.text and STREET not in page.text
    assert f'value="/secrets/vault/{secret}"' in page.text
    r = post(client, "/secrets/unlock", {"passphrase": "wrong passphrase!!", "next": f"/secrets/vault/{secret}"})
    assert r.status_code == 403 and not vault.is_unlocked()
    r = post(client, "/secrets/unlock", {"passphrase": PASSPHRASE, "next": f"/secrets/vault/{secret}"})
    assert r.status_code == 303 and r.headers["location"] == f"/secrets/vault/{secret}"
    post(client, "/secrets/lock", {})
    r = post(client, "/secrets/unlock", {"passphrase": PASSPHRASE, "next": "https://evil.example/"})
    assert r.headers["location"] == "/secrets/"


def test_unlock_is_rate_limited(env):
    client, vault = env[0], env[1]
    setup_vault(client)
    post(client, "/secrets/lock", {})
    codes = [post(client, "/secrets/unlock", {"passphrase": "nope nope nope"}).status_code for _ in range(5)]
    assert codes == [403] * 5
    r = post(client, "/secrets/unlock", {"passphrase": PASSPHRASE})
    assert r.status_code == 429 and not vault.is_unlocked()


def test_reset_needs_typed_confirmation(env):
    client, vault = env[0], env[1]
    setup_vault(client)
    add(client, ADDRESS)
    assert post(client, "/secrets/settings/reset", {"confirm": "delete"}).status_code == 400
    assert post(client, "/secrets/settings/reset", {"confirm": "DELETE"}).status_code == 303
    assert not vault.is_setup()


# ── CSRF and origin ──────────────────────────────────────────────────────────


def test_post_needs_csrf_and_same_origin(env):
    client, vault = env[0], env[1]
    form = {"passphrase": PASSPHRASE, "confirm": PASSPHRASE}
    assert post(client, "/secrets/setup", form, csrf="0" * 64).status_code == 403
    assert post(client, "/secrets/setup", form, origin="https://evil.example").status_code == 403
    assert post(client, "/secrets/setup", form, origin=None).status_code == 403
    assert not vault.is_setup()


# ── secrets ────────────────────────────────────────────────────────────────────


def test_private_fields_hidden_until_show(env):
    client = env[0]
    setup_vault(client)
    secret = add(client, CARD_FORM)
    page = client.get(f"/secrets/vault/{secret}").text
    assert "Visa" in page and "ending 4242" in page and "12/30" in page
    assert CARD not in page and SECRET not in page
    shown = post(client, f"/secrets/vault/{secret}/show", {})
    assert CARD in shown.text and SECRET in shown.text
    assert any(e.action == "viewed private fields" for e in env[1].audit_log())
    edit = client.get(f"/secrets/vault/{secret}/edit").text
    assert CARD not in edit and SECRET not in edit


def test_editing_keeps_private_values_left_blank(env):
    client, vault = env[0], env[1]
    setup_vault(client)
    secret = add(client, CARD_FORM)
    form = {
        "kind": "card",
        "name": "Visa (work)",
        "f_cardholder": "Alfred Test",
        "f_number": "",
        "clear_number": "on",  # required: cannot be removed, the blank keeps it
        "f_expiry": "01/31",
        "f_security_code": "",
        "clear_security_code": "on",
        "f_notes": "",
        "share_mode": "ask",
    }
    assert post(client, f"/secrets/vault/{secret}", form).status_code == 303
    saved = vault.secret(secret)
    assert saved.name == "Visa (work)" and saved.field("number").value == CARD
    assert saved.field("expiry").value == "01/31" and saved.field("security_code") is None
    assert saved.field("notes").value == SECRET


def test_card_cannot_be_shared_without_asking(env):
    client, vault = env[0], env[1]
    setup_vault(client)
    assert post(client, "/secrets/vault", {**CARD_FORM, "share_mode": "auto"}).status_code == 400
    assert vault.secrets() == []


def test_delete_item(env):
    client, vault = env[0], env[1]
    setup_vault(client)
    secret = add(client, ADDRESS)
    assert post(client, f"/secrets/vault/{secret}/delete", {}).status_code == 303
    assert vault.secrets() == []


def test_values_never_in_history_page(env):
    client = env[0]
    setup_vault(client)
    secret = add(client, CARD_FORM)
    post(client, f"/secrets/vault/{secret}/show", {})
    page = client.get("/secrets/audit").text
    assert "viewed private fields" in page and CARD not in page and SECRET not in page


# ── MCP ──────────────────────────────────────────────────────────────────────


def test_mcp_tools(env):
    r = mcp(env[0], "tools/list")
    assert r.status_code == 200, r.text
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == {"list_secrets", "request_secret", "get_result"}
    assert tools["list_secrets"]["annotations"]["readOnlyHint"] is True
    assert tools["get_result"]["annotations"]["readOnlyHint"] is True
    assert tools["request_secret"]["annotations"].get("readOnlyHint") is not True
    assert tools["request_secret"]["inputSchema"]["required"] == ["secret_id", "purpose"]


def test_mcp_refuses_public_host(env):
    assert mcp(env[0], "tools/list", host_url=f"{ORIGIN}/mcp").status_code in (403, 421)


def test_end_to_end_through_mcp_and_ui(env, caplog):
    caplog.set_level(logging.DEBUG)
    client, vault, sender = env
    setup_vault(client)
    card = add(client, CARD_FORM)
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)

    listed, raw = call_tool(client, "list_secrets", {})
    assert listed["secrets"][0]["name"] == "Visa"
    assert CARD not in raw and SECRET not in raw

    args = {"secret_id": card, "fields": ["number", "security_code"], "purpose": "Pay for the train"}
    sub, _ = call_tool(client, "request_secret", args)
    assert sub["status"] == "pending"
    rid = sub["id"]

    page = client.get(f"/secrets/r/{rid}").text
    assert "Pay for the train" in page and "Card number" in page and "ending 4242" in page and CARD not in page
    assert "Expiry (MM/YY): 12/30" in page
    assert 'class="badge">1<' in client.get("/secrets/vault").text
    assert post(client, f"/secrets/r/{rid}/decide", {"action": "share", "fields": ["number"]}).status_code == 303

    res, _ = call_tool(client, "get_result", {"request_id": rid, "wait_seconds": 5})
    assert res["status"] == "done" and res["values"] == {"number": CARD} and res["withheld"] == ["Security code"]
    again, _ = call_tool(client, "get_result", {"request_id": rid})
    assert "values" not in again
    assert CARD not in client.get(f"/secrets/r/{rid}").text
    assert CARD not in json.dumps([c["data"] for c in sender.calls])
    assert CARD not in caplog.text and SECRET not in caplog.text


def test_mcp_cannot_approve(env):
    """An agent can reach every internal route, but has no Access JWT."""
    client = env[0]
    setup_vault(client)
    secret = add(client, ADDRESS)
    sub, _ = call_tool(client, "request_secret", {"secret_id": secret, "purpose": "x"})
    r = client.post(
        f"http://secret-broker:8000/secrets/r/{sub['id']}/decide",
        data={"action": "share", "fields": "text", "csrf": "x"},
        headers={"cf-access-jwt-assertion": "", "origin": ORIGIN},
    )
    assert r.status_code == 403
    res, _ = call_tool(client, "get_result", {"request_id": sub["id"]})
    assert res["status"] == "pending"


def test_request_page_escapes_agent_input(env):
    client = env[0]
    setup_vault(client)
    secret = add(client, ADDRESS)
    args = {"secret_id": secret, "purpose": "<script>alert(1)</script>"}
    sub, _ = call_tool(client, "request_secret", args)
    page = client.get(f"/secrets/r/{sub['id']}").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_push_subscription_endpoints_work_while_locked(env):
    client, vault, _ = env
    csrf = csrf_of(client.get("/secrets/notifications").text)
    headers = {"x-csrf-token": csrf, "origin": ORIGIN}
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "key", "auth": "auth"}}
    assert client.post("/secrets/push/subscribe", json=sub, headers=headers).status_code == 200
    assert [s["email"] for s in vault.subscriptions()] == [EMAIL]
    assert client.post("/secrets/push/subscribe", json=sub, headers={"origin": ORIGIN}).status_code == 403
    bad = {"endpoint": "http://internal/x", "keys": {"p256dh": "k", "auth": "a"}}
    assert client.post("/secrets/push/subscribe", json=bad, headers=headers).status_code == 400
    assert client.post("/secrets/push/test", json={}, headers=headers).json() == {"delivered": 1}
    unsub = {"endpoint": sub["endpoint"]}
    assert client.post("/secrets/push/unsubscribe", json=unsub, headers=headers).status_code == 200
    assert vault.subscriptions() == []


def test_locked_vault_answers_agents_and_notifies(env):
    """Regression: the list tool ran in a worker thread and could not schedule the unlock push."""
    client, vault, sender = env
    setup_vault(client)
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    post(client, "/secrets/lock", {})
    listed, _ = call_tool(client, "list_secrets", {})
    assert listed["locked"] is True
    for _ in range(100):  # the push is sent in the background
        if sender.calls:
            break
        time.sleep(0.02)
    assert len(sender.calls) == 1
