import hashlib
import hmac
import os

from passlib.context import CryptContext


password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if password_hash.startswith("$2"):
        try:
            return password_context.verify(password, password_hash)
        except (TypeError, ValueError):
            return False

    return verify_legacy_password(password, password_hash)


def verify_legacy_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = password_hash.split("$")
    except ValueError:
        return False

    if algorithm != "pbkdf2_sha256":
        return False

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        int(iterations),
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


def password_needs_upgrade(password_hash: str) -> bool:
    return not password_hash.startswith("$2")
