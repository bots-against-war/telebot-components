import abc
import asyncio
import copy
import json
import os
import time as time_module
from collections import defaultdict
from datetime import timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable, Coroutine, Mapping, Optional, Union

from telebot_components.redis_utils.interface import (
    RedisCmdReturn,
    RedisInterface,
    RedisPipelineInterface,
)


class RedisEmulation(RedisInterface):
    """Inmemory redis emulation, compliant with interface, useful for local runs and tests."""

    def __init__(self, response_delay: float | None = None) -> None:
        self.values: dict[str, bytes] = dict()
        self.sets: dict[str, set[bytes]] = defaultdict(set)
        self.lists: dict[str, list[bytes]] = defaultdict(list)
        self.hashes: dict[str, dict[str, bytes]] = defaultdict(dict)
        self.key_eviction_time: dict[str, float] = dict()

        self.response_delay = response_delay

    @property
    def storages(self) -> tuple[dict[str, Any], ...]:
        return (self.values, self.sets, self.lists, self.hashes)

    def pipeline(self, transaction: bool = True, shard_hint: Optional[str] = None) -> "RedisPipelineEmulatiom":
        return RedisPipelineEmulatiom(self)

    async def _bookkeeping(self, key: str):
        if key not in self.key_eviction_time:
            return
        evict_at = self.key_eviction_time[key]
        if time_module.time() <= evict_at:
            return
        self.key_eviction_time.pop(key)
        for storage in self.storages:
            if key in storage:
                storage.pop(key)

        if self.response_delay is not None:
            await asyncio.sleep(self.response_delay)

    def _remove_from_storages(self, key: str) -> int:
        n_popped = 0
        for storage in self.storages:
            if storage.pop(key, None) is not None:
                n_popped += 1
        return n_popped

    async def set(
        self,
        name: str,
        value: bytes,
        ex: Optional[timedelta] = None,
        *args,
        **kwargs,
    ) -> bool:
        await self._bookkeeping(name)
        self._remove_from_storages(name)
        self.values[name] = value
        if ex is not None:
            self.key_eviction_time[name] = time_module.time() + ex.total_seconds()
        return True

    async def get(self, name: str) -> Optional[bytes]:
        await self._bookkeeping(name)
        return self.values.get(name)

    async def delete(self, *names: str) -> int:
        for name in names:
            await self._bookkeeping(name)
        n_deleted = 0
        for key in names:
            n_deleted += self._remove_from_storages(key)
        return n_deleted

    async def copy(
        self,
        source: str,
        destination: str,
        destination_db: Union[str, None] = None,
        replace: bool = False,
    ) -> bool:
        """Note: dbs are not supported, so destination_db param is ignored"""
        for name in (source, destination):
            await self._bookkeeping(name)
        for storage in self.storages:
            if destination in storage:
                if replace:
                    storage.pop(destination)
                else:
                    return False
        for storage in self.storages:
            if source in storage:
                storage[destination] = copy.deepcopy(storage[source])
                return True
        return False

    async def rename(self, src: str, dst: str) -> bool:
        for name in (src, dst):
            await self._bookkeeping(name)
        for storage in self.storages:
            if src in storage:
                await self.delete(dst)
                storage[dst] = storage.pop(src)
                return True
        raise KeyError(f"src key does not exist: {src}")

    async def expire(self, name: str, time: timedelta) -> int:
        self.key_eviction_time[name] = time_module.time() + time.total_seconds()
        return 1

    async def sadd(self, name: str, *values: bytes) -> int:
        await self._bookkeeping(name)
        target_set = self.sets[name]
        new_values = {v for v in values if v not in target_set}
        target_set.update(new_values)
        return len(new_values)

    async def srem(self, name: str, *values: bytes) -> int:
        await self._bookkeeping(name)
        target_set = self.sets[name]
        values_to_remove = {v for v in values if v in target_set}
        target_set.difference_update(values_to_remove)
        return len(values_to_remove)

    async def smembers(self, name: str) -> list[bytes]:
        await self._bookkeeping(name)
        return list(self.sets[name])

    async def spop(self, name: str, count: Optional[int] = None) -> Optional[Union[bytes, list[bytes]]]:
        await self._bookkeeping(name)
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
        await self._bookkeeping(name)
        return int(value in self.sets.get(name, set()))

    async def incr(self, name: str) -> int:
        await self._bookkeeping(name)
        current_value_bytes = self.values.get(name)
        if current_value_bytes is None:
            current_value = 0
        else:
            current_value = int(current_value_bytes.decode("utf-8"))
        new_value = current_value + 1
        self.values[name] = str(new_value).encode("utf-8")
        return new_value

    async def rpush(self, name: str, *values: bytes) -> int:
        await self._bookkeeping(name)
        for v in values:
            self.lists[name].append(v)
        return len(self.lists[name])

    async def rpop(
        self,
        name: str,
        count: Optional[int] = None,
    ) -> Union[bytes, list[bytes], None]:
        await self._bookkeeping(name)
        pop_elements = count or 1
        lst = self.lists.get(name)
        if not lst:
            return None
        popped: list[bytes] = []
        for _ in range(pop_elements):
            if not lst:
                return popped
            popped.append(lst.pop())
        if pop_elements == 1:
            return popped[0]
        else:
            return popped or None

    def _redis_slice(self, list_: list[Any], start: int, end: int) -> list[Any]:
        """Redis-style list indexing (inclusive end, liberal out-of bounds treatment)"""
        length = len(list_)
        if start > length:
            return []
        if end > 0:
            end = min(end, length)
        elif end == -1:
            end = length
        end += 1  # redis' `end` is inclusive, python's is exclusive
        return list_[start:end]

    async def lrange(self, name: str, start: int, end: int) -> list[bytes]:
        await self._bookkeeping(name)
        if name not in self.lists:
            return []
        list_ = self.lists[name]
        if not isinstance(list_, list):
            raise TypeError("lrange on non-list key")
        return self._redis_slice(list_, start, end)

    async def llen(self, name: str) -> int:
        await self._bookkeeping(name)
        return len(self.lists.get(name, []))

    async def lset(self, name: str, index: int, value: bytes) -> bool:
        await self._bookkeeping(name)
        list_ = self.lists.get(name)
        if list_ is None:
            raise KeyError(f"no such key: {name}")
        try:
            list_[index] = value
        except Exception:
            raise KeyError(f"index out of range: {name}[{index}]")

        return True

    async def ltrim(self, name: str, start: int, end: int) -> bool:
        await self._bookkeeping(name)
        if name in self.lists:
            self.lists[name] = self._redis_slice(self.lists[name], start, end)
        return True

    async def exists(self, *names: str) -> int:
        n_exist = 0
        for name in names:
            for storage in self.storages:
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
        for storage in self.storages:
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
        items: Optional[list[Union[str, bytes]]] = None,
    ) -> int:
        await self._bookkeeping(name)
        if (key is None and value is None) and not mapping and not items:
            raise ValueError("'hset' with no key value pairs")
        updates: dict[str, bytes] = dict()
        if mapping:
            updates.update(mapping)
        if key is not None and value is not None:
            updates[key] = value
        if items:
            for k, v in zip(items[:-1:2], items[1::2]):
                updates[k] = v  # type: ignore
        self.hashes[name].update(updates)
        return len(updates)

    async def hget(self, name: str, key: str) -> Optional[bytes]:
        return self.hashes.get(name, {}).get(key)

    async def hkeys(self, name: str) -> list[bytes]:
        # NOTE: redis client does not decode anything received from Redis by default,
        # so we have to re-encode keys from a hash
        await self._bookkeeping(name)
        return [key.encode("utf-8") for key in self.hashes.get(name, {}).keys()]

    async def hvals(self, name: str) -> list[bytes]:
        await self._bookkeeping(name)
        return [value for value in self.hashes.get(name, {}).values()]

    async def hlen(self, name: str) -> int:
        await self._bookkeeping(name)
        return len(self.hashes.get(name, {}))

    async def hgetall(self, name: str) -> dict[bytes, bytes]:
        await self._bookkeeping(name)
        return {key.encode("utf-8"): value for key, value in self.hashes.get(name, {}).items()}

    async def hdel(self, name: str, *keys: str) -> int:
        await self._bookkeeping(name)
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

    def __init__(self, redis: RedisInterface):
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

    async def copy(
        self,
        source: str,
        destination: str,
        destination_db: Union[str, None] = None,
        replace: bool = False,
    ) -> bool:
        self._stack.append(self.redis_em.copy(source, destination, destination_db, replace))
        return False

    async def rename(self, src: str, dst: str) -> bool:
        self._stack.append(self.redis_em.rename(src, dst))
        return True

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

    async def rpop(
        self,
        name: str,
        count: Optional[int] = None,
    ) -> Union[bytes, list[bytes], None]:
        self._stack.append(self.redis_em.rpop(name, count))
        return None

    async def lrange(self, name: str, start: int, end: int) -> list[bytes]:
        self._stack.append(self.redis_em.lrange(name, start, end))
        return []

    async def ltrim(self, name: str, start: int, end: int) -> bool:
        self._stack.append(self.redis_em.ltrim(name, start, end))
        return True

    async def llen(self, name: str) -> int:
        self._stack.append(self.redis_em.llen(name))
        return 0

    async def lset(self, name: str, index: int, value: bytes) -> bool:
        self._stack.append(self.redis_em.lset(name, index, value))
        return False

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
        items: Optional[list[Union[str, bytes]]] = None,
    ) -> int:
        self._stack.append(self.redis_em.hset(name, key, value, mapping, items))
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

    async def hlen(self, name: str) -> int:
        self._stack.append(self.redis_em.hlen(name))
        return 0

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


class PersistentRedisEmulation(RedisInterface):
    """
    JSON-based persistent wrapper around regular inmemory RedisEmulation.

    Mypy will complain on this class' instantiation, but you can safely ignore it.
    """

    # TODO: add lists support, add tests, fix defaultdict creation

    def __init__(
        self,
        dirname: str = ".redis-emulation",
        dump: Callable[[Any], str] = lambda obj: json.dumps(obj, ensure_ascii=False, indent=2),
        load: Callable[[str], Any] = lambda json_dump: json.loads(json_dump),
    ) -> None:
        self.r = RedisEmulation()
        self.dirname = dirname
        self.dump = dump
        self.load = load
        self.load_persistent_state()

    def load_persistent_state(self) -> None:
        if self._values_file.exists():
            self.r.values = {k: v.encode("utf-8") for k, v in self.load(self._values_file.read_text()).items()}
        if self._lists_file.exists():
            self.r.lists = defaultdict(
                list,
                {k: [item.encode("utf-8") for item in v] for k, v in self.load(self._lists_file.read_text()).items()},
            )
        if self._sets_file.exists():
            self.r.sets = defaultdict(
                set,
                {k: {item.encode("utf-8") for item in s} for k, s in self.load(self._sets_file.read_text()).items()},
            )

        if self._hashes_file.exists():
            self.r.hashes = defaultdict(
                dict,
                {
                    k: {kk: v.encode("utf-8") for kk, v in d.items()}
                    for k, d in self.load(self._hashes_file.read_text()).items()
                },
            )
        if self._expiration_times_file.exists():
            self.r.key_eviction_time = self.load(self._expiration_times_file.read_text())

    def update_persistent_state(self) -> None:
        self._values_file.write_text(self.dump({k: v.decode("utf-8") for k, v in self.r.values.items()}))
        self._lists_file.write_text(
            self.dump({k: [item.decode("utf-8") for item in v] for k, v in self.r.lists.items()})
        )
        self._sets_file.write_text(self.dump({k: [item.decode("utf-8") for item in s] for k, s in self.r.sets.items()}))
        self._hashes_file.write_text(
            self.dump({k: {kk: v.decode("utf-8") for kk, v in d.items()} for k, d in self.r.hashes.items()})
        )
        self._expiration_times_file.write_text(self.dump(self.r.key_eviction_time))

    @property
    def _persistent_dir(self) -> Path:
        persistent_dir = Path(os.getcwd()) / self.dirname
        persistent_dir.mkdir(exist_ok=True)
        return persistent_dir

    @property
    def _values_file(self) -> Path:
        return self._persistent_dir / "values.json"

    @property
    def _lists_file(self) -> Path:
        return self._persistent_dir / "lists.json"

    @property
    def _sets_file(self) -> Path:
        return self._persistent_dir / "sets.json"

    @property
    def _hashes_file(self) -> Path:
        return self._persistent_dir / "hashes.json"

    @property
    def _expiration_times_file(self) -> Path:
        return self._persistent_dir / "key_expiration_times.json"

    def pipeline(self, transaction: bool = True, shard_hint: Optional[str] = None) -> RedisPipelineInterface:
        return RedisPipelineEmulatiom(self)


# monkey patching methods on PersistentRedisEmulation


def create_persistent_wrapper_method(redis_interface_method_name: str):
    async def method_wrapper(self: PersistentRedisEmulation, *args, **kwargs):
        wrapped_func = getattr(self.r, redis_interface_method_name)
        res = await wrapped_func(*args, **kwargs)
        self.update_persistent_state()
        return res

    return method_wrapper


for method_name in RedisInterface.__abstractmethods__:
    if method_name == "pipeline":
        continue
    setattr(PersistentRedisEmulation, method_name, create_persistent_wrapper_method(method_name))
abc.update_abstractmethods(PersistentRedisEmulation)  # type: ignore
