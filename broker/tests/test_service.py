import json
import logging

import anyio
import pytest

from secret_broker.config import MAX_PENDING, REQUEST_TTL_SECONDS
from secret_broker.vault import VaultError

from .conftest import (
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


async def test_list_items_has_no_values(settings, vault):
    add_address(vault)
    add_card(vault)
    listed = make_broker(settings, vault).list_items()
    text = json.dumps(listed)
    assert {i["name"] for i in listed["items"]} == {"Home address", "Visa"}
    for needle in (STREET, CARD, SECRET, "Lisboa"):
        assert needle not in text
    card = next(i for i in listed["items"] if i["name"] == "Visa")
    assert {"key": "number", "label": "Card number"} in card["fields"] and card["needs_approval"] is True


async def test_locked_vault_refuses_and_asks_to_unlock(settings, vault):
    add_address(vault)
    vault.lock()
    sender = FakeSender()
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    broker = make_broker(settings, vault, sender)

    async def flow():
        a = broker.list_items()
        b = await broker.request_item("x", ["street"], "delivery")
        return a, b

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
        sub = await broker.request_item(card, ["cardholder", "number", "expiry", "security_code"], "Pay for the train")
        assert sub["status"] == "pending" and sub["approve_url"].endswith(f"/secrets/r/{sub['id']}")
        assert (await broker.get_result(sub["id"]))["status"] == "pending"
        broker.decide(sub["id"], True, ["number", "expiry"], EMAIL)
        first = await broker.get_result(sub["id"])
        second = await broker.get_result(sub["id"])
        return first, second

    first, second = await with_broker(broker, flow)
    assert first["status"] == "done" and first["values"] == {"number": CARD, "expiry": "12/30"}
    assert first["withheld"] == ["Name on card", "Security code"]
    assert "values" not in second
    pushed = json.dumps([c["data"] for c in sender.calls])
    assert "Visa" in pushed and CARD not in pushed and "4242" not in pushed
    assert CARD not in caplog.text
    assert any("shared on approval" == e.action for e in vault.audit_log())


async def test_deny_and_expiry(settings, vault, clock):
    item = add_address(vault)
    broker = make_broker(settings, vault)

    async def flow():
        a = await broker.request_item(item, ["street"], "x")
        broker.decide(a["id"], False, [], EMAIL)
        b = await broker.request_item(item, ["street"], "y")
        clock.t += REQUEST_TTL_SECONDS + 1
        with pytest.raises(VaultError):
            broker.decide(b["id"], True, ["street"], EMAIL)
        return await broker.get_result(a["id"]), await broker.get_result(b["id"])

    a, b = await with_broker(broker, flow)
    assert a["status"] == "denied" and b["status"] == "expired"


async def test_cannot_approve_twice_or_add_fields(settings, vault):
    item = add_address(vault)
    broker = make_broker(settings, vault)

    async def flow():
        sub = await broker.request_item(item, ["street"], "x")
        with pytest.raises(VaultError):
            broker.decide(sub["id"], True, ["city"], EMAIL)  # not requested: nothing left to share
        broker.decide(sub["id"], True, ["street", "city"], EMAIL)
        with pytest.raises(VaultError):
            broker.decide(sub["id"], True, ["street"], EMAIL)
        return await broker.get_result(sub["id"])

    res = await with_broker(broker, flow)
    assert res["values"] == {"street": STREET}


async def test_share_without_asking(settings, vault):
    item = add_address(vault, share_mode="auto")
    sender = FakeSender()
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    broker = make_broker(settings, vault, sender)
    res = await with_broker(broker, lambda: broker.request_item(item, ["street", "city"], "delivery form"))
    assert res == {"status": "done", "item": "Home address", "values": {"street": STREET, "city": "Lisboa"}}
    assert vault.audit_log()[0].action == "shared without asking"
    assert STREET not in json.dumps([c["data"] for c in sender.calls])


@pytest.mark.parametrize(
    "args,error",
    [
        (("nope", ["street"], "x"), "no item"),
        (("ITEM", ["iban"], "x"), "fields"),
        (("ITEM", [], "x"), "fields"),
        (("ITEM", ["street"], ""), "say what"),
        (("ITEM", ["street"], "x" * 600), "say what"),
    ],
)
async def test_bad_requests_refused(settings, vault, args, error):
    item = add_address(vault)
    broker = make_broker(settings, vault)
    args = tuple(item if a == "ITEM" else a for a in args)
    res = await with_broker(broker, lambda: broker.request_item(*args))
    assert res["status"] == "refused" and error in res["error"]


async def test_pending_cap(settings, vault):
    item = add_address(vault)
    broker = make_broker(settings, vault)

    async def flow():
        for _ in range(MAX_PENDING):
            assert (await broker.request_item(item, ["street"], "x"))["status"] == "pending"
        return await broker.request_item(item, ["street"], "x")

    assert (await with_broker(broker, flow))["status"] == "refused"


async def test_long_poll_returns_on_decision(settings, vault):
    item = add_address(vault)
    broker = make_broker(settings, vault)

    async def flow():
        sub = await broker.request_item(item, ["street"], "x")

        async def answer():
            await anyio.sleep(0.2)
            broker.decide(sub["id"], True, ["street"], EMAIL)

        async with anyio.create_task_group() as tg:
            tg.start_soon(answer)
            with anyio.fail_after(5):
                return await broker.get_result(sub["id"], wait_seconds=10)

    assert (await with_broker(broker, flow))["status"] == "done"


async def test_deleted_item_cannot_be_shared(settings, vault):
    item = add_address(vault)
    broker = make_broker(settings, vault)

    async def flow():
        sub = await broker.request_item(item, ["street"], "x")
        vault.delete_item(item)
        with pytest.raises(VaultError):
            broker.decide(sub["id"], True, ["street"], EMAIL)

    await with_broker(broker, flow)


async def test_restart_forgets_requests(settings, vault, vault_path, clock):
    from secret_broker.vault import Vault

    from .conftest import FAST_KDF

    item = add_address(vault)
    broker = make_broker(settings, vault)
    sub = await with_broker(broker, lambda: broker.request_item(item, ["street"], "x"))
    fresh = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    fresh.unlock(PASSPHRASE)
    res = await make_broker(settings, fresh).get_result(sub["id"])
    assert res["status"] == "unknown"
