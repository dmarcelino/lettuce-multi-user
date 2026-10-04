import json
import logging

from secret_broker.logs import SecretFilter
from secret_broker.push import Pusher

from .conftest import EMAIL, SECRET, FakeSender


def test_push_uses_broker_vapid_key_and_skips_removed_users(settings, vault):
    sender = FakeSender()
    vault.add_subscription("https://push.example/1", "k1", "a1", EMAIL)
    vault.add_subscription("https://push.example/2", "k2", "a2", "removed@example.com")
    assert Pusher(settings, vault, sender).send_sync({"title": "t", "body": "b", "url": "/secrets/", "tag": "x"}) == 1
    call = sender.calls[0]
    assert call["subscription_info"]["endpoint"] == "https://push.example/1"
    assert call["vapid_private_key"] == settings.vapid_private_key
    assert call["vapid_claims"] == {"sub": "mailto:ops@example.com"}
    assert json.loads(call["data"])["title"] == "t"


def test_gone_subscription_is_removed(settings, vault):
    vault.add_subscription("https://push.example/1", "k1", "a1", EMAIL)
    assert Pusher(settings, vault, FakeSender(fail_status=410)).send_sync({"title": "t"}) == 0
    assert vault.subscriptions() == []


def test_other_push_failure_keeps_subscription(settings, vault):
    vault.add_subscription("https://push.example/1", "k1", "a1", EMAIL)
    assert Pusher(settings, vault, FakeSender(fail_status=500)).send_sync({"title": "t"}) == 0
    assert len(vault.subscriptions()) == 1


def test_log_filter_redacts_values_while_in_use(caplog):
    f = SecretFilter()
    logger = logging.getLogger("test.redaction")
    logger.addFilter(f)
    try:
        with caplog.at_level(logging.INFO, logger="test.redaction"):
            with f.guarding([SECRET, "PT"]):
                logger.info("oops %s in PT", SECRET)
                try:
                    raise ValueError(SECRET)
                except ValueError:
                    logger.exception("failed")
            logger.info("after %s", "done")
    finally:
        logger.removeFilter(f)
    assert SECRET not in caplog.text
    assert "[REDACTED] in PT" in caplog.text and "after done" in caplog.text
