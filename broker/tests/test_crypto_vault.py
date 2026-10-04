import pytest

from secret_broker import crypto
from secret_broker.vault import AUTO, Locked, Vault, VaultError, build_fields

from .conftest import CARD, FAST_KDF, PASSPHRASE, SECRET, STREET, add_address, add_card


def test_seal_round_trip_and_unique_nonces():
    key = crypto.new_key()
    a = crypto.seal(key, b"hello", b"row:1")
    b = crypto.seal(key, b"hello", b"row:1")
    assert a != b
    assert crypto.open_sealed(key, a, b"row:1") == b"hello"


def test_tampering_or_moving_a_blob_fails():
    key = crypto.new_key()
    blob = crypto.seal(key, b"hello", b"item:a")
    with pytest.raises(crypto.CryptoError):
        crypto.open_sealed(key, blob, b"item:b")
    with pytest.raises(crypto.CryptoError):
        crypto.open_sealed(crypto.new_key(), blob, b"item:a")
    flipped = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(crypto.CryptoError):
        crypto.open_sealed(key, flipped, b"item:a")


def test_nothing_readable_on_disk(vault, vault_path):
    add_address(vault)
    add_card(vault)
    vault.audit("alfred@gmail.com", "shared on approval", None, f"Home address: {STREET}")
    raw = open(vault_path, "rb").read()
    for needle in (STREET, CARD, SECRET, "Home address", "Lisboa", "Visa", "Alfred Test", PASSPHRASE, "deliveries"):
        assert needle.encode() not in raw, needle


def test_restart_comes_back_locked(vault, vault_path, clock):
    item_id = add_address(vault)
    again = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    assert again.is_setup() and not again.is_unlocked()
    with pytest.raises(Locked):
        again.items()
    with pytest.raises(VaultError, match="wrong passphrase"):
        again.unlock("not the passphrase at all")
    again.unlock(PASSPHRASE)
    assert again.item(item_id).field("street").value == STREET


def test_setup_rules(vault_path, clock):
    v = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    assert not v.is_setup()
    with pytest.raises(VaultError, match="12"):
        v.setup("short")
    v.setup(PASSPHRASE)
    with pytest.raises(VaultError, match="already"):
        v.setup(PASSPHRASE)


def test_auto_lock_counts_only_user_activity(vault, clock):
    add_address(vault)
    clock.t += 11 * 3600
    vault.items()  # agent-side use does not keep it open
    clock.t += 2 * 3600
    assert not vault.is_unlocked()
    vault.unlock(PASSPHRASE)
    clock.t += 11 * 3600
    vault.touch()  # the person using the UI does
    clock.t += 2 * 3600
    assert vault.is_unlocked()


def test_lock(vault):
    vault.lock()
    with pytest.raises(Locked):
        vault.items()


def test_change_passphrase_keeps_items(vault, vault_path, clock):
    item_id = add_address(vault)
    with pytest.raises(VaultError):
        vault.change_passphrase("wrong wrong wrong", "another long passphrase")
    vault.change_passphrase(PASSPHRASE, "another long passphrase")
    again = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    with pytest.raises(VaultError):
        again.unlock(PASSPHRASE)
    again.unlock("another long passphrase")
    assert again.item(item_id).name == "Home address"


def test_reset_erases_everything(vault, vault_path):
    add_card(vault)
    vault.add_subscription("https://push.example/1", "k", "a", "alfred@gmail.com")
    vault.reset()
    assert not vault.is_setup() and not vault.is_unlocked()
    assert len(vault.subscriptions()) == 1
    assert CARD.encode() not in open(vault_path, "rb").read()


def test_sensitive_kinds_and_fields_cannot_share_without_asking(vault):
    card = build_fields("card", {"number": CARD}, [])
    with pytest.raises(VaultError, match="without asking"):
        vault.add_item("card", "Visa", "", card, AUTO)
    with_private = build_fields("address", {"street": STREET}, [("Door code", "4321", True)])
    with pytest.raises(VaultError, match="without asking"):
        vault.add_item("address", "Home", "", with_private, AUTO)
    plain = build_fields("membership", {"programme": "TAP", "member_number": "123456789"}, [])
    assert vault.item(vault.add_item("membership", "TAP", "", plain, AUTO)).share_mode == AUTO


def test_build_fields_rules():
    fields = build_fields(
        "login", {"website": "shop.example", "password": SECRET}, [("Recovery", "x", False), ("", "y", False)]
    )
    assert [(f.key, f.sensitive) for f in fields] == [("website", False), ("password", True), ("recovery", False)]
    dup = build_fields("note", {"text": "hello"}, [("Text", "again", False)])
    assert [f.key for f in dup] == ["text", "text_2"]
    with pytest.raises(VaultError):
        build_fields("note", {}, [])
    with pytest.raises(VaultError):
        build_fields("rocket", {"x": "y"}, [])


def test_audit_is_encrypted_and_readable_when_unlocked(vault):
    vault.audit("alfred@gmail.com", "viewed hidden fields", None, "Visa")
    assert vault.audit_log()[0].detail == "Visa"
    vault.lock()
    vault.audit("alfred@gmail.com", "failed unlock")
    with pytest.raises(Locked):
        vault.audit_log()
