from __future__ import annotations

import keyring
from keyring.errors import KeyringError, PasswordDeleteError


SERVICE_NAME = "steam-friend-relationship-map"
ALLOWED_SECRET_NAMES = {"steam_api_key", "neo4j_password"}


class SecretStorageError(RuntimeError):
    pass


class SecretStore:
    def __init__(self, service_name: str = SERVICE_NAME) -> None:
        self.service_name = service_name

    def get(self, name: str) -> str:
        self._validate_name(name)
        try:
            return keyring.get_password(self.service_name, name) or ""
        except KeyringError as exc:
            raise SecretStorageError(str(exc)) from exc

    def set(self, name: str, value: str) -> None:
        self._validate_name(name)
        if not value:
            raise SecretStorageError("secret value cannot be empty")
        try:
            keyring.set_password(self.service_name, name, value)
        except KeyringError as exc:
            raise SecretStorageError(str(exc)) from exc

    def delete(self, name: str) -> None:
        self._validate_name(name)
        try:
            keyring.delete_password(self.service_name, name)
        except PasswordDeleteError:
            return
        except KeyringError as exc:
            raise SecretStorageError(str(exc)) from exc

    def configured(self, name: str) -> bool:
        return bool(self.get(name))

    def _validate_name(self, name: str) -> None:
        if name not in ALLOWED_SECRET_NAMES:
            raise SecretStorageError(f"unsupported secret name: {name}")
