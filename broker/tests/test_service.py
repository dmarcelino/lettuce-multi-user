import json
import logging

import anyio
import pytest

from secret_broker.config import HANDLE_TTL_SECONDS, MAX_HANDLES, MAX_PENDING, REQUEST_TTL_SECONDS
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


# ── env delivery: a one-time handle instead of the values ─────────────────────


async def test_env_delivery_returns_a_handle_never_values(settings, vault, caplog):
    caplog.set_level(logging.DEBUG)
    card = add_card(vault)
    broker = make_broker(settings, vault)

    async def flow():
        sub = await broker.request_secret(card, "Pay for the train", ["number", "notes"], delivery="env")
        assert broker.request(sub["id"]).delivery == "env"
        broker.decide(sub["id"], True, ["number"], EMAIL)
        return await broker.get_result(sub["id"]), await broker.get_result(sub["id"])

    first, second = await with_broker(broker, flow)
    assert first["status"] == "done" and first["secret"] == "Visa" and "values" not in first
    assert first["env"] == {"VAULT_NUMBER": "Card number"} and first["withheld"] == ["Notes"]
    assert first["expires_in"] == HANDLE_TTL_SECONDS and first["handle"] in first["run"]
    assert "vault-run" in first["run"]
    assert CARD not in json.dumps(first) and SECRET not in json.dumps(first)
    assert "handle" not in second and "values" not in second

    redeemed = broker.redeem(first["handle"])
    assert redeemed == {"secret": "Visa", "env": {"VAULT_NUMBER": CARD}}
    assert broker.redeem(first["handle"]) is None  # single use
    assert any(e.action == "given to a command" for e in vault.audit_log())
    assert CARD not in caplog.text and first["handle"] not in caplog.text


async def test_handle_expires(settings, vault, clock):
    secret = add_address(vault, share_mode="auto")
    broker = make_broker(settings, vault)
    res = await with_broker(broker, lambda: broker.request_secret(secret, "delivery form", delivery="env"))
    clock.t += HANDLE_TTL_SECONDS + 1
    assert broker.redeem(res["handle"]) is None


async def test_env_delivery_without_asking_returns_a_handle_at_once(settings, vault):
    secret = add_address(vault, share_mode="auto")
    broker = make_broker(settings, vault)
    res = await with_broker(broker, lambda: broker.request_secret(secret, "delivery form", delivery="env"))
    assert res["status"] == "done" and res["env"] == {"VAULT_TEXT": "Text"}
    assert STREET not in json.dumps(res)
    assert broker.redeem(res["handle"]) == {"secret": "Home address", "env": {"VAULT_TEXT": ADDRESS_TEXT}}


@pytest.mark.parametrize("bad", ["", "file", None, 1])
async def test_unknown_delivery_refused(settings, vault, bad):
    secret = add_address(vault)
    broker = make_broker(settings, vault)
    res = await with_broker(broker, lambda: broker.request_secret(secret, "x", delivery=bad))
    assert res["status"] == "refused" and "delivery" in res["error"]


@pytest.mark.parametrize("bad", ["nope", "", None, 42])
async def test_redeem_unknown_handle(settings, vault, bad):
    assert make_broker(settings, vault).redeem(bad) is None


async def test_redeem_is_audited_with_the_purpose(settings, vault):
    secret = add_address(vault, share_mode="auto")
    broker = make_broker(settings, vault)
    res = await with_broker(broker, lambda: broker.request_secret(secret, "delivery form", delivery="env"))
    broker.redeem(res["handle"])
    entry = next(e for e in vault.audit_log() if e.action == "given to a command")
    assert entry.detail == "Home address: Text. Purpose: delivery form"


async def test_lock_forgets_handles_and_uncollected_values(settings, vault):
    auto = add_address(vault, share_mode="auto")
    card = add_card(vault)
    broker = make_broker(settings, vault)

    async def flow():
        handle = (await broker.request_secret(auto, "delivery form", delivery="env"))["handle"]
        sub = await broker.request_secret(card, "Pay for the train", ["number"])
        broker.decide(sub["id"], True, ["number"], EMAIL)
        vault.lock()
        locked = broker.redeem(handle), await broker.get_result(sub["id"])
        vault.unlock(PASSPHRASE)
        return locked, broker.redeem(handle), await broker.get_result(sub["id"])

    (redeemed, result), after_unlock, result_after_unlock = await with_broker(broker, flow)
    assert redeemed is None and after_unlock is None
    for res in (result, result_after_unlock):
        assert res["status"] == "expired" and "locked" in res["note"] and "values" not in res
    assert CARD not in json.dumps([result, result_after_unlock])


async def test_unused_handles_are_capped(settings, vault):
    secret = add_address(vault, share_mode="auto")
    broker = make_broker(settings, vault)

    async def flow():
        for _ in range(MAX_HANDLES):
            assert (await broker.request_secret(secret, "x", delivery="env"))["status"] == "done"
        refused = await broker.request_secret(secret, "x", delivery="env")
        text = await broker.request_secret(secret, "x")  # text delivery is not capped
        return refused, text

    refused, text = await with_broker(broker, flow)
    assert refused["status"] == "refused" and "handles" in refused["error"]
    assert text["status"] == "done"
