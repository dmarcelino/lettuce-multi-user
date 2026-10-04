import json
import logging

import anyio
import pytest

from secret_broker.config import MAX_PENDING, REQUEST_TTL_SECONDS
from secret_broker.vault import VaultError

from .conftest import (
    ADDRESS_TEXT,
    CARD,
    EMAIL,
    PASSPHRASE,
    SECRET,
    STREET,
    FakeSender,
    add_address,
    add_card,
    make_broker,
    with_broker,
)

pytestmark = pytest.mark.anyio


async def test_list_secrets_shows_public_details_never_private_values(settings, vault):
    add_address(vault)
    add_card(vault)
    listed = make_broker(settings, vault).list_secrets()
    text = json.dumps(listed)
    for needle in (STREET, CARD, SECRET, "Lisboa", '123"'):
        assert needle not in text
    card = next(s for s in listed["secrets"] if s["name"] == "Visa")
    assert card["type"] == "Payment card"
    assert card["details"] == {"Name on card": "Alfred Test", "Expiry (MM/YY)": "12/30"}
    assert [f["key"] for f in card["private_fields"]] == ["number", "security_code", "notes"]
    assert card["needs_approval"] is True
    address = next(s for s in listed["secrets"] if s["name"] == "Home address")
    assert address["details"] == {} and address["use_for"] == "deliveries"


async def test_locked_vault_refuses_and_asks_to_unlock(settings, vault):
    add_address(vault)
    vault.lock()
    sender = FakeSender()
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    broker = make_broker(settings, vault, sender)

    async def flow():
        return broker.list_secrets(), await broker.request_secret("x", "delivery")

    a, b = await with_broker(broker, flow)
    assert a["locked"] and b["locked"]
    # One unlock notice, not one per call.
    assert len(sender.calls) == 1 and "locked" in json.loads(sender.calls[0]["data"])["title"].lower()


async def test_approval_flow_shares_only_ticked_fields_once(settings, vault, caplog):
    caplog.set_level(logging.DEBUG)
    card = add_card(vault)
    sender = FakeSender()
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    broker = make_broker(settings, vault, sender)

    async def flow():
        sub = await broker.request_secret(card, "Pay for the train")  # no fields: all private ones
        assert sub["status"] == "pending" and sub["approve_url"].endswith(f"/secrets/r/{sub['id']}")
        req = broker.request(sub["id"])
        assert req.fields == ("number", "security_code", "notes")
        assert req.details == {"Name on card": "Alfred Test", "Expiry (MM/YY)": "12/30"}
        assert (await broker.get_result(sub["id"]))["status"] == "pending"
        broker.decide(sub["id"], True, ["number", "security_code"], EMAIL)
        return await broker.get_result(sub["id"]), await broker.get_result(sub["id"])

    first, second = await with_broker(broker, flow)
    assert first["status"] == "done" and first["values"] == {"number": CARD, "security_code": "123"}
    assert first["withheld"] == ["Notes"]
    assert "values" not in second
    pushed = json.dumps([c["data"] for c in sender.calls])
    assert "Visa" in pushed and CARD not in pushed and "4242" not in pushed
    assert CARD not in caplog.text and SECRET not in caplog.text
    assert any(e.action == "shared on approval" for e in vault.audit_log())


async def test_public_fields_are_not_requested(settings, vault):
    card = add_card(vault)
    broker = make_broker(settings, vault)
    res = await with_broker(broker, lambda: broker.request_secret(card, "x", ["expiry", "number"]))
    assert res["status"] == "refused" and "already in list_secrets" in res["error"]


async def test_deny_and_expiry(settings, vault, clock):
    secret = add_address(vault)
    broker = make_broker(settings, vault)

    async def flow():
        a = await broker.request_secret(secret, "x")
        broker.decide(a["id"], False, [], EMAIL)
        b = await broker.request_secret(secret, "y")
        clock.t += REQUEST_TTL_SECONDS + 1
        with pytest.raises(VaultError):
            broker.decide(b["id"], True, ["text"], EMAIL)
        return await broker.get_result(a["id"]), await broker.get_result(b["id"])

    a, b = await with_broker(broker, flow)
    assert a["status"] == "denied" and b["status"] == "expired"


async def test_cannot_approve_twice_or_add_fields(settings, vault):
    card = add_card(vault)
    broker = make_broker(settings, vault)

    async def flow():
        sub = await broker.request_secret(card, "x", ["number"])
        with pytest.raises(VaultError):
            broker.decide(sub["id"], True, ["notes"], EMAIL)  # not requested: nothing left to share
        broker.decide(sub["id"], True, ["number", "notes"], EMAIL)
        with pytest.raises(VaultError):
            broker.decide(sub["id"], True, ["number"], EMAIL)
        return await broker.get_result(sub["id"])

    assert (await with_broker(broker, flow))["values"] == {"number": CARD}


async def test_free_text_address_shared_without_asking(settings, vault):
    secret = add_address(vault, share_mode="auto")
    sender = FakeSender()
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    broker = make_broker(settings, vault, sender)
    res = await with_broker(broker, lambda: broker.request_secret(secret, "delivery form"))
    assert res == {"status": "done", "secret": "Home address", "values": {"text": ADDRESS_TEXT}}
    assert vault.audit_log()[0].action == "shared without asking"
    assert STREET not in json.dumps([c["data"] for c in sender.calls])


@pytest.mark.parametrize(
    "args,error",
    [
        (("nope", "x"), "no secret"),
        (("SECRET", "x", ["iban"]), "private fields"),
        (("SECRET", ""), "say what"),
        (("SECRET", "x" * 600), "say what"),
    ],
)
async def test_bad_requests_refused(settings, vault, args, error):
    secret = add_address(vault)
    broker = make_broker(settings, vault)
    args = tuple(secret if a == "SECRET" else a for a in args)
    res = await with_broker(broker, lambda: broker.request_secret(*args))
    assert res["status"] == "refused" and error in res["error"]


async def test_pending_cap(settings, vault):
    secret = add_address(vault)
    broker = make_broker(settings, vault)

    async def flow():
        for _ in range(MAX_PENDING):
            assert (await broker.request_secret(secret, "x"))["status"] == "pending"
        return await broker.request_secret(secret, "x")

    assert (await with_broker(broker, flow))["status"] == "refused"


async def test_long_poll_returns_on_decision(settings, vault):
    secret = add_address(vault)
    broker = make_broker(settings, vault)

    async def flow():
        sub = await broker.request_secret(secret, "x")

        async def answer():
            await anyio.sleep(0.2)
            broker.decide(sub["id"], True, ["text"], EMAIL)

        async with anyio.create_task_group() as tg:
            tg.start_soon(answer)
            with anyio.fail_after(5):
                return await broker.get_result(sub["id"], wait_seconds=10)

    assert (await with_broker(broker, flow))["status"] == "done"


async def test_deleted_secret_cannot_be_shared(settings, vault):
    secret = add_address(vault)
    broker = make_broker(settings, vault)

    async def flow():
        sub = await broker.request_secret(secret, "x")
        vault.delete_secret(secret)
        with pytest.raises(VaultError):
            broker.decide(sub["id"], True, ["text"], EMAIL)

    await with_broker(broker, flow)


async def test_restart_forgets_requests(settings, vault, vault_path, clock):
    from secret_broker.vault import Vault

    from .conftest import FAST_KDF

    secret = add_address(vault)
    broker = make_broker(settings, vault)
    sub = await with_broker(broker, lambda: broker.request_secret(secret, "x"))
    fresh = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    fresh.unlock(PASSPHRASE)
    assert (await make_broker(settings, fresh).get_result(sub["id"]))["status"] == "unknown"
