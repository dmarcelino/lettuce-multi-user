"""Encryption for the vault file.

A random 256-bit data key encrypts every record with AES-256-GCM. The data key
is stored only wrapped (encrypted) with a key derived from the user's
passphrase by scrypt, so the file alone reveals nothing but item ids, types and
timestamps. Each ciphertext is bound to its row through the associated data,
so blobs cannot be swapped between rows.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
SALT_BYTES = 16


class CryptoError(Exception):
    """Wrong key, or the data was changed."""


@dataclass(frozen=True)
class KdfParams:
    # 2^17 * 8 * 128 bytes = 128 MiB per derivation: slow enough to make
    # guessing a passphrase from a copied file expensive.
    n: int = 2**17
    r: int = 8
    p: int = 1

    def to_dict(self) -> dict[str, int]:
        return {"n": self.n, "r": self.r, "p": self.p}


def new_key() -> bytes:
    return os.urandom(KEY_BYTES)


def new_salt() -> bytes:
    return os.urandom(SALT_BYTES)


def derive_key(passphrase: str, salt: bytes, params: KdfParams) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=params.n,
        r=params.r,
        p=params.p,
        maxmem=256 * 1024 * 1024,
        dklen=KEY_BYTES,
    )


def seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def open_sealed(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < NONCE_BYTES + 16:
        raise CryptoError("ciphertext too short")
    try:
        return AESGCM(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], aad)
    except InvalidTag as e:
        raise CryptoError("wrong key or tampered data") from e
