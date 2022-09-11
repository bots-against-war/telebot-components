import time as time_module
from collections import defaultdict
from datetime import timedelta
from fnmatch import fnmatch
from typing import Coroutine, Mapping, Optional, Union

from telebot_components.redis_utils.interface import (
    RedisCmdReturn,
    RedisInterface,
    RedisPipelineInterface,
)


class RedisEmulation(RedisInterface):
    """Inmemory redis emulation, compliant with interface, useful for local runs and tests."""

    def __init__(self):
        self.values: dict[str, bytes] = dict()
        self.sets: dict[str, set[bytes]] = defaultdict(set)
        self.lists: dict[str, list[bytes]] = defaultdict(list)
        self.hashes: dict[str, dict[str, bytes]] = defaultdict(dict)
        self._storages = (self.sets, self.values, self.lists)
        self.key_eviction_time: dict[str, float] = dict()

    def pipeline(self, transaction: bool = True, shard_hint: Optional[str] = None) -> "RedisPipelineEmulatiom":
        return RedisPipelineEmulatiom(self)

    async def set(
        self,
        name: str,
        value: bytes,
        ex: Optional[timedelta] = None,
        *args,
        **kwargs,
    ) -> bool:
        self.values[name] = value
        if ex is not None:
            self.key_eviction_time[name] = time_module.time() + ex.total_seconds()
        return True

    def _evict_if_expired(self, key: str):
        if key not in self.key_eviction_time:
            return
        evict_at = self.key_eviction_time[key]
        if time_module.time() <= evict_at:
            return
        self.key_eviction_time.pop(key)
        for storage in self._storages:
            if key in storage:
                storage.pop(key)

    async def get(self, name: str) -> Optional[bytes]:
        self._evict_if_expired(name)
        return self.values.get(name)

    async def delete(self, *names: str) -> int:
        n_deleted = 0
        for key in names:
            for storage in self._storages:
                if storage.pop(key, None) is not None:
                    n_deleted += 1
        return n_deleted

    async def expire(self, name: str, time: timedelta) -> int:
        self.key_eviction_time[name] = time_module.time() + time.total_seconds()
        return 1

    async def sadd(self, name: str, *values: bytes) -> int:
        self._evict_if_expired(name)
        target_set = self.sets[name]
        new_values = {v for v in values if v not in target_set}
        target_set.update(new_values)
        return len(new_values)

    async def srem(self, name: str, *values: bytes) -> int:
        self._evict_if_expired(name)
        target_set = self.sets[name]
        values_to_remove = {v for v in values if v in target_set}
        target_set.difference_update(values_to_remove)
        return len(values_to_remove)

    async def smembers(self, name: str) -> list[bytes]:
        self._evict_if_expired(name)
        return list(self.sets[name])

    async def spop(self, name: str, count: Optional[int] = None) -> Optional[Union[bytes, list[bytes]]]:
        self._evict_if_expired(name)
        set_ = self.sets.get(name)
        if set_ is None:
            return None
        if count is None:
            try:
                return set_.pop()
            except KeyError:
                return None
        else:
            popped: list[bytes] = []
            for _ in range(count):
                try:
                    popped.append(set_.pop())
                except KeyError:
                    break
            return popped

    async def sismember(self, name: str, value: bytes) -> int:
        self._evict_if_expired(name)
        return int(value in self.sets.get(name, set()))

    async def incr(self, name: str) -> int:
        self._evict_if_expired(name)
        current_value_bytes = self.values.get(name)
        if current_value_bytes is None:
            current_value = 0
        else:
            current_value = int(current_value_bytes.decode("utf-8"))
        new_value = current_value + 1
        self.values[name] = str(new_value).encode("utf-8")
        return new_value

    async def rpush(self, name: str, *values: bytes) -> int:
        self._evict_if_expired(name)
        for v in values:
            self.lists[name].append(v)
        return len(self.lists[name])

    async def lrange(self, name: str, start: int, end: int) -> list[bytes]:
        self._evict_if_expired(name)
        if name not in self.lists:
            return []
        list_ = self.lists[name]
        if not isinstance(list_, list):
            raise TypeError("lrange on non-list key")
        length = len(list_)
        if start > length:
            return []
        start = max(start, 0)
        if end < -1:
            end += 1
        elif end == -1:
            end = length
        else:
            end = min(end, length)
            end += 1
        return list_[start:end]

    async def exists(self, *names: str) -> int:
        n_exist = 0
        for name in names:
            for storage in self._storages:
                if name in storage:
                    n_exist += 1
                    break
        return n_exist

    async def keys(self, pattern: str = "*") -> list[bytes]:
        """NOTE: this implementation uses fnmatch and may deviate from the actual Redis matching rules

        See docs for fnmatch: https://docs.python.org/3/library/fnmatch.html#module-fnmatch
        and for Redis KEYS: https://redis.io/commands/keys/
        """
        matches: list[bytes] = []
        for storage in self._storages:
            for key in storage:
                if fnmatch(key, pattern):
                    matches.append(key.encode("utf-8"))
        return matches

    async def hset(
        self,
        name: str,
        key: Optional[str] = None,
        value: Optional[bytes] = None,
        mapping: Optional[Mapping[str, bytes]] = None,
    ) -> int:
        if mapping is None:
            if key is None or value is None:
                raise TypeError("If mapping is not specified, key and value must be set")
            mapping = {key: value}
        self.hashes[name].update(mapping)
        return len(mapping)

    async def hget(self, name: str, key: str) -> Optional[bytes]:
        return self.hashes.get(name, {}).get(key)

    async def hkeys(self, name: str) -> list[bytes]:
        # NOTE: redis client does not decode anything received from Redis by default,
        # so we have to re-encode keys from a hash
        return [key.encode("utf-8") for key in self.hashes.get(name, {}).keys()]

    async def hvals(self, name: str) -> list[bytes]:
        return [value for value in self.hashes.get(name, {}).values()]

    async def hdel(self, name: str, *keys: str) -> int:
        count = 0
        hash_ = self.hashes.get(name, {})
        for k in keys:
            value = hash_.pop(k, None)
            if value is not None:
                count += 1
        return count


class RedisPipelineEmulatiom(RedisEmulation, RedisPipelineInterface):
    """Simple pipeline emulation that just stores parent redis emulation coroutines
    in a list and awaits them on execute"""

    def __init__(self, redis: RedisEmulation):
        self.redis_em = redis
        self._stack: list[Coroutine[None, None, RedisCmdReturn]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args, **kwargs):
        pass

    async def set(self, name: str, value: bytes, ex: Optional[timedelta] = None, *args, **kwargs) -> bool:
        self._stack.append(self.redis_em.set(name, value, ex, *args, **kwargs))
        return False

    async def get(self, name: str) -> Optional[bytes]:
        self._stack.append(self.redis_em.get(name))
        return None

    async def delete(self, *names: str) -> int:
        self._stack.append(self.redis_em.delete(*names))
        return 0

    async def sadd(self, name: str, *values: bytes) -> int:
        self._stack.append(self.redis_em.sadd(name, *values))
        return 0

    async def srem(self, name: str, *values: bytes) -> int:
        self._stack.append(self.redis_em.srem(name, *values))
        return 0

    async def smembers(self, name: str) -> list[bytes]:
        self._stack.append(self.redis_em.smembers(name))
        return []

    async def sismember(self, name: str, value: bytes) -> int:
        self._stack.append(self.redis_em.sismember(name, value))
        return 0

    async def spop(self, name: str, count: Optional[int] = None) -> Optional[Union[bytes, list[bytes]]]:
        self._stack.append(self.redis_em.spop(name, count))
        return None

    async def incr(self, name: str) -> int:
        self._stack.append(self.redis_em.incr(name))
        return 0

    async def rpush(self, name: str, *values: bytes) -> int:
        self._stack.append(self.redis_em.rpush(name, *values))
        return 0

    async def lrange(self, name: str, start: int, end: int) -> list[bytes]:
        self._stack.append(self.redis_em.lrange(name, start, end))
        return []

    async def exists(self, *names: str) -> int:
        self._stack.append(self.redis_em.exists(*names))
        return 0

    async def keys(self, pattern: str = "*") -> list[bytes]:
        self._stack.append(self.redis_em.keys(pattern))
        return []

    async def expire(self, name: str, time: timedelta) -> int:
        self._stack.append(self.redis_em.expire(name, time))
        return 0

    async def hset(
        self,
        name: str,
        key: Optional[str] = None,
        value: Optional[bytes] = None,
        mapping: Optional[Mapping[str, bytes]] = None,
    ) -> int:
        self._stack.append(self.redis_em.hset(name, key, value, mapping))
        return 0

    async def hget(self, name: str, key: str) -> Optional[bytes]:
        self._stack.append(self.redis_em.hget(name, key))
        return None

    async def hkeys(self, name: str) -> list[bytes]:
        self._stack.append(self.redis_em.hkeys(name))
        return []

    async def hvals(self, name: str) -> list[bytes]:
        self._stack.append(self.redis_em.hvals(name))
        return []

    async def hdel(self, name: str, *keys: str) -> int:
        self._stack.append(self.redis_em.hdel(name, *keys))
        return 0

    async def execute(self, raise_on_error: bool = True) -> list[RedisCmdReturn]:
        results: list[RedisCmdReturn] = []
        for command_coro in self._stack:
            try:
                results.append(await command_coro)
            except Exception:
                if raise_on_error:
                    raise
                else:
                    results.append(None)
        return results
