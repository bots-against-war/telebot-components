import base64
import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import toml  # type: ignore
from cryptography.fernet import Fernet

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import KeyDictStore

logger = logging.getLogger(__name__)


@dataclass
class SaveSecretResult:
    is_saved: bool
    message: str


OwnerId = Union[str, int]


ADMIN_OWNER_ID: OwnerId = 0


class SecretStore(ABC):
    def __init__(self, scope_secrets_to_user: bool) -> None:
        self.scope_secrets_to_user = scope_secrets_to_user

    def to_env_specific(self, owner_id: OwnerId) -> OwnerId:
        if self.scope_secrets_to_user:
            return owner_id
        else:
            return ADMIN_OWNER_ID  # store all secrets as admin secrets

    @abstractmethod
    async def get_secret(self, secret_name: str, owner_id: OwnerId = ADMIN_OWNER_ID) -> Optional[str]:
        """For internal use only -- nevere expose to the user!"""
        ...

    async def get_required_secret(self, secret_name: str, owner_id: OwnerId = ADMIN_OWNER_ID) -> str:
        secret = await self.get_secret(secret_name, owner_id)
        if secret is None:
            raise RuntimeError(f"Required secret {secret_name!r} ({owner_id = }) not found")
        else:
            return secret

    @abstractmethod
    async def list_secrets(self, owner_id: OwnerId = ADMIN_OWNER_ID) -> list[str]:
        """For admin chat only — never expose to the user"""
        ...

    @abstractmethod
    async def list_owners(self) -> list[OwnerId]:
        ...

    @abstractmethod
    async def save_secret(
        self, secret_name: str, secret_value: str, owner_id: OwnerId, allow_update: bool = False
    ) -> SaveSecretResult:
        ...

    @abstractmethod
    async def remove_secret(self, secret_name: str, owner_id: OwnerId) -> bool:
        ...

    def user_to_owner_id(self, user_id: int) -> OwnerId:
        user_id_bytes = str(user_id).encode("utf-8")
        owner_id_bytes = hashlib.sha256(user_id_bytes).digest()
        owner_id = int.from_bytes(owner_id_bytes[:8], byteorder="little")
        return self.to_env_specific(owner_id)


class RedisSecretStore(SecretStore):
    """Redis-backed secret store with symmetric encryption"""

    def __init__(
        self,
        redis: RedisInterface,
        encryption_key: str,
        secrets_per_user: int,
        secret_max_len: int,
        scope_secrets_to_user: bool,
    ) -> None:
        super().__init__(scope_secrets_to_user=scope_secrets_to_user)
        self.fernet = Fernet(encryption_key)
        self.secrets_per_user = secrets_per_user
        self.secret_max_len = secret_max_len

        self._store = KeyDictStore[str](
            name="secret",
            prefix="global",
            redis=redis,
            expiration_time=None,
            dumper=lambda s: s,
            loader=lambda s: s,
        )

    async def get_secret(self, secret_name: str, owner_id: OwnerId = ADMIN_OWNER_ID) -> Optional[str]:
        encrypted_b64 = await self._store.get_subkey(self.to_env_specific(owner_id), secret_name)
        if encrypted_b64 is None:
            return None
        try:
            encrypted = base64.b64decode(encrypted_b64)
            return self.fernet.decrypt(encrypted).decode("utf-8")
        except Exception:
            logger.exception(f"Error decrypting secret {secret_name!r} (belongs to {owner_id = })")
            return None

    async def list_owners(self) -> list[OwnerId]:
        return sorted({self.to_env_specific(oid) for oid in await self._store.list_keys()})

    async def list_secrets(self, owner_id: OwnerId = ADMIN_OWNER_ID) -> list[str]:
        return await self._store.list_subkeys(self.to_env_specific(owner_id))

    async def save_secret(
        self, secret_name: str, secret_value: str, owner_id: OwnerId, allow_update: bool = False
    ) -> SaveSecretResult:
        owner_id = self.to_env_specific(owner_id)
        if owner_id != ADMIN_OWNER_ID and len(await self.list_secrets(owner_id)) > self.secrets_per_user:
            return SaveSecretResult(is_saved=False, message="⚠️ Secrets quota for the user is exhausted")
        if not allow_update and await self._store.get_subkey(owner_id, secret_name) is not None:
            return SaveSecretResult(is_saved=False, message="⚠️ Secret already exists")
        secret_value_bytes = secret_value.encode("utf-8")
        if len(secret_value_bytes) > self.secret_max_len:
            return SaveSecretResult(
                is_saved=False,
                message=f"⚠️ Secret length exceeds max allowed secret length ({self.secret_max_len} bytes)",
            )
        encrypted_b64 = base64.b64encode(self.fernet.encrypt(secret_value_bytes)).decode("ascii")
        if await self._store.set_subkey(owner_id, secret_name, encrypted_b64):
            return SaveSecretResult(is_saved=True, message="✅")
        else:
            return SaveSecretResult(is_saved=False, message="⚠️ Error saving the secret to database")

    async def remove_secret(self, secret_name: str, owner_id: OwnerId) -> bool:
        return await self._store.remove_subkey(self.to_env_specific(owner_id), secret_name)


class TomlFileSecretStore(SecretStore):
    """File secret storage for local testing"""

    def __init__(self, path: Path) -> None:
        super().__init__(scope_secrets_to_user=False)
        self.path = path
        try:
            self._secrets: dict[str, str] = toml.load(self.path)
        except FileNotFoundError:
            logger.warning(f"Secrets file not found, running without secrets: {self.path.absolute()}")
            self._secrets = dict()

    async def get_secret(self, secret_name: str, owner_id: OwnerId = ADMIN_OWNER_ID) -> Optional[str]:
        return self._secrets.get(secret_name)

    async def list_secrets(self, owner_id: OwnerId = ADMIN_OWNER_ID) -> list[str]:
        return list(self._secrets.keys())

    async def list_owners(self) -> list[OwnerId]:
        return [ADMIN_OWNER_ID]

    async def save_secret(
        self, secret_name: str, secret_value: str, owner_id: OwnerId, allow_update: bool = False
    ) -> SaveSecretResult:
        raise NotImplementedError("This is a read-only secret store")

    async def remove_secret(self, secret_name: str, owner_id: OwnerId) -> bool:
        raise NotImplementedError("This is a read-only secret store")
