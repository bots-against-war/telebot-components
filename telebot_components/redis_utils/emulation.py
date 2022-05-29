from collections import defaultdict
from typing import Coroutine, Optional, Union


from telebot_components.redis_utils.interface import RedisInterface, RedisPipelineInterface, ExpiryT, RedisCmdReturn


class RedisEmulation(RedisInterface):
    """Inmemory redis emulation, compliant with interface, useful for local runs and tests.

    NOTE: key expiration is not currently emulated.
    """

    def __init__(self):
        self.values: dict[str, bytes] = dict()
        self.sets: dict[str, set[bytes]] = defaultdict(set)
        self._storages = (self.sets, self.values)

    def pipeline(self, transaction: bool = True, shard_hint: Optional[str] = None) -> "RedisPipelineEmulatiom":
        return RedisPipelineEmulatiom(self)

    async def set(
        self,
        name: str,
        value: bytes,
        ex: Optional[ExpiryT] = None,
        px: Optional[ExpiryT] = None,
        nx: bool = False,
        xx: bool = False,
        keepttl: bool = False,
    ) -> bool:
        self.values[name] = value  # expiration is not currently emulated!
        return True

    async def get(self, name: str) -> Optional[bytes]:
        return self.values.get(name)

    async def delete(self, *names: str) -> int:
        n_deleted = 0
        for key in names:
            for storage in self._storages:
                if storage.pop(key, None) is not None:
                    n_deleted += 1
        return n_deleted

    async def expire(self, name: str, time: ExpiryT) -> int:
        return 1

    async def sadd(self, name: str, *values: bytes) -> int:
        target_set = self.sets[name]
        new_values = {v for v in values if v not in target_set}
        target_set.update(new_values)
        return len(new_values)

    async def srem(self, name: str, *values: bytes) -> int:
        target_set = self.sets[name]
        values_to_remove = {v for v in values if v not in target_set}
        target_set.difference_update(values_to_remove)
        return len(values_to_remove)

    async def smembers(self, name: str) -> list[bytes]:
        return list(self.sets[name])


class RedisPipelineEmulatiom(RedisEmulation, RedisPipelineInterface):
    """Simple pipeline emulation that just stores parent redis emulation coroutines
    in a list and awaits them on execute"""

    def __init__(self, redis: RedisEmulation):
        self.redis = redis
        self._stack: list[Coroutine[None, None, RedisCmdReturn]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self):
        pass

    async def set(self, *args, **kwargs) -> bool:
        self._stack.append(self.redis.set(*args, **kwargs))
        return False

    async def get(self, name: str) -> Optional[bytes]:
        self._stack.append(self.redis.get(name))
        return None

    async def delete(self, *names: str) -> int:
        self._stack.append(self.redis.delete(*names))
        return 0

    async def sadd(self, name: str, *values: bytes) -> int:
        self._stack.append(self.redis.sadd(name, *values))
        return 0

    async def srem(self, name: str, *values: bytes) -> int:
        self._stack.append(self.redis.srem(name, *values))
        return 0

    async def smembers(self, name: str) -> list[bytes]:
        self._stack.append(self.redis.smembers(name))
        return []

    async def execute(self, raise_on_error: bool = True) -> list[RedisCmdReturn]:
        results: list[RedisCmdReturn] = []
        for cmd_coroutine in self._stack:
            try:
                results.append(await cmd_coroutine)
            except Exception:
                if raise_on_error:
                    raise
                else:
                    results.append(None)
        return results
