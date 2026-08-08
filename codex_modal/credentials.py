"""Proxy-token discovery and cross-platform local storage."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from dataclasses import dataclass

from .paths import CREDENTIALS_PATH, PROJECT_ROOT, STATE_ROOT


@dataclass(frozen=True)
class ProxyCredential:
    token_id: str
    token_secret: str

    @property
    def combined(self) -> str:
        return f"{self.token_id}.{self.token_secret}"


def parse_combined(value: str) -> ProxyCredential:
    value = value.strip()
    token_id, separator, token_secret = value.partition(".")
    if (
        separator != "."
        or not token_id.startswith("wk-")
        or not token_secret.startswith("ws-")
        or any(character.isspace() for character in value)
    ):
        raise ValueError("Expected a Modal proxy token in wk-<id>.ws-<secret> form.")
    return ProxyCredential(token_id, token_secret)


def from_environment() -> ProxyCredential | None:
    combined = os.environ.get("MODAL_PROXY_TOKEN")
    if combined:
        return parse_combined(combined)
    token_id = os.environ.get("MODAL_PROXY_TOKEN_ID")
    token_secret = os.environ.get("MODAL_PROXY_TOKEN_SECRET")
    if token_id and token_secret:
        return parse_combined(f"{token_id}.{token_secret}")
    return None


def _keyring_service() -> str:
    identity = hashlib.sha256(str(PROJECT_ROOT).encode("utf-8")).hexdigest()[:16]
    return f"codex-via-modal:{identity}"


def _keyring_value() -> str | None:
    try:
        import keyring

        backend = keyring.get_keyring()
        if float(getattr(backend, "priority", 0)) <= 0:
            return None
        return keyring.get_password(_keyring_service(), "modal-proxy-token")
    except Exception:
        return None


def load_proxy_token() -> ProxyCredential | None:
    environment_value = from_environment()
    if environment_value is not None:
        return environment_value

    stored = _keyring_value()
    if stored:
        try:
            return parse_combined(stored)
        except ValueError:
            pass

    try:
        document = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
        return parse_combined(str(document["proxy_token"]))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _store_with_keyring(credential: ProxyCredential) -> bool:
    try:
        import keyring

        backend = keyring.get_keyring()
        if float(getattr(backend, "priority", 0)) <= 0:
            return False
        keyring.set_password(
            _keyring_service(), "modal-proxy-token", credential.combined
        )
        return keyring.get_password(
            _keyring_service(), "modal-proxy-token"
        ) == credential.combined
    except Exception:
        return False


def _store_private_file(credential: ProxyCredential) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = CREDENTIALS_PATH.with_name(
        f".{CREDENTIALS_PATH.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    payload = json.dumps({"proxy_token": credential.combined}) + "\n"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        os.replace(temporary, CREDENTIALS_PATH)
        try:
            os.chmod(CREDENTIALS_PATH, 0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def store_proxy_token(credential: ProxyCredential) -> str:
    """Store a credential without ever echoing it; return the storage description."""

    if _store_with_keyring(credential):
        return "the operating-system credential store"
    _store_private_file(credential)
    warning = (
        "No usable Python keyring backend was available. The proxy token was stored in the "
        f"git-ignored file {CREDENTIALS_PATH} with owner-only permissions where supported."
    )
    print(f"WARNING: {warning}", file=sys.stderr)
    return "the project-local private credential file"
