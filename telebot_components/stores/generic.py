import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from hashlib import md5
from typing import (
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Generic,
    Optional,
    Protocol,
    TypeVar,
    cast,
)

import tenacity

from telebot_components.constants.times import MONTH
from telebot_components.redis_utils.interface import RedisInterface

T = TypeVar("T")


class str_able(Protocol):
    def __str__(self) -> str:
        ...


WrappedFuncT = TypeVar("WrappedFuncT")


logger = logging.getLogger(__name__)


def redis_retry() -> Callable[[WrappedFuncT], WrappedFuncT]:
    return tenacity.retry(  # type: ignore
        wait=tenacity.wait.wait_random_exponential(multiplier=1, max=30, exp_base=2, min=0.5),
        stop=tenacity.stop.stop_after_delay(max_delay=60),
        retry=tenacity.retry_if_exception_type(),
        after=tenacity.after.after_log(logger, log_level=logging.WARNING),
    )


@dataclass
class GenericStore(Generic[T]):
    name: str  # used to identify a particular store
    prefix: str  # used to identify bot that uses the store
    redis: RedisInterface
    expiration_time: Optional[timedelta] = MONTH
    dumper: Callable[[T], str] = json.dumps
    loader: Callable[[str], T] = json.loads

    _prefix_registry: ClassVar[set[str]] = set()

    def __post_init__(self):
        self.logger = logging.getLogger(f"{__name__}[{self.prefix}-{self.name}]")
        # adding prefix hash to allow stores with nested prefixes
        # e.g. stores with prefixes 'a' and 'ab' could cause a collision but
        # we transform them to 'a-0cc17' and 'ab-187ef' and voila
        plain_prefix = f"{self.prefix}-{self.name}"
        prefix_hash = md5(plain_prefix.encode("utf-8")).hexdigest()[:5]
        self._full_prefix = f"{plain_prefix}-{prefix_hash}-"
        if self._full_prefix in self._prefix_registry:
            raise ValueError(
                f"Attempt to create {self.__class__.__name__} with prefix {self._full_prefix!r} already in use"
            )
        else:
            self._prefix_registry.add(self._full_prefix)

    def __del__(self):
        self._prefix_registry.discard(self._full_prefix)

    @classmethod
    def allow_duplicate_stores(cls, prefix: str):
        cls._prefix_registry = {fp for fp in cls._prefix_registry if not fp.startswith(prefix)}

    def _full_key(self, key: str_able) -> str:
        return f"{self._full_prefix}{key}"

    @redis_retry()
    async def drop(self, key: str_able) -> bool:
        n_deleted = await self.redis.delete(self._full_key(key))
        return n_deleted == 1

    @redis_retry()
    async def exists(self, key: str_able) -> bool:
        return (await self.redis.exists(self._full_key(key))) == 1

    @redis_retry()
    async def list_keys(self) -> list[str]:
        matching_full_keys = await self.redis.keys(self._full_prefix + "*")
        return [fk.decode("utf-8").removeprefix(self._full_prefix) for fk in matching_full_keys]


ItemT = TypeVar("ItemT")


@dataclass
class KeySetStore(GenericStore[ItemT]):
    async def add(self, key: str_able, item: ItemT, reset_ttl: bool = True) -> bool:
        return await self.add_multiple(key, [item], reset_ttl)

    @redis_retry()
    async def add_multiple(self, key: str_able, items: list[ItemT], reset_ttl: bool = True) -> bool:
        async with self.redis.pipeline() as pipe:
            item_dumps = [self.dumper(item).encode("utf-8") for item in items]
            await pipe.sadd(self._full_key(key), *item_dumps)
            if reset_ttl and self.expiration_time is not None:
                await pipe.expire(self._full_key(key), self.expiration_time)

            results = await pipe.execute()
            return all(r == 1 for r in results)

    @redis_retry()
    async def pop_multiple(self, key: str_able, count: int) -> list[ItemT]:
        dumps = await self.redis.spop(self._full_key(key), count=count)
        if dumps is None:
            return []
        if isinstance(dumps, bytes):
            dumps_list = [dumps]
        else:
            dumps_list = dumps
        return [self.loader(d.decode("utf-8")) for d in dumps_list]

    @redis_retry()
    async def remove(self, key: str_able, item: ItemT) -> bool:
        n_removed = await self.redis.srem(self._full_key(key), self.dumper(item).encode("utf-8"))
        return n_removed == 1

    @redis_retry()
    async def all(self, key: str_able) -> set[ItemT]:
        item_dumps = await self.redis.smembers(self._full_key(key))
        return {self.loader(item_dump.decode("utf-8")) for item_dump in item_dumps}

    @redis_retry()
    async def includes(self, key: str_able, item: ItemT) -> bool:
        return (await self.redis.sismember(self._full_key(key), self.dumper(item).encode("utf-8"))) == 1


@dataclass
class KeyListStore(GenericStore[ItemT]):
    @redis_retry()
    async def push(self, key: str_able, item: ItemT, reset_ttl: bool = True) -> int:
        async with self.redis.pipeline() as pipe:
            await pipe.rpush(self._full_key(key), self.dumper(item).encode("utf-8"))
            if reset_ttl and self.expiration_time is not None:
                await pipe.expire(self._full_key(key), self.expiration_time)
            after_push_len, *_ = await pipe.execute()
            return cast(int, after_push_len)

    @redis_retry()
    async def all(self, key: str_able) -> list[ItemT]:
        item_dumps = await self.redis.lrange(self._full_key(key), 0, -1)
        return [self.loader(item_dump.decode("utf-8")) for item_dump in item_dumps]


@dataclass
class SetStore(GenericStore[ItemT]):
    const_key: str = "const"

    def __post_init__(self):
        super().__post_init__()
        self._key_set_store = KeySetStore[ItemT](
            name=f"{self.name}-fixed",
            prefix=self.prefix,
            redis=self.redis,
            expiration_time=self.expiration_time,
            dumper=self.dumper,
            loader=self.loader,
        )

    async def add(self, item: ItemT):
        return await self._key_set_store.add(self.const_key, item)

    async def remove(self, item: ItemT):
        return await self._key_set_store.remove(self.const_key, item)

    async def drop(self):
        return await self._key_set_store.drop(self.const_key)

    async def all(self):
        return await self._key_set_store.all(self.const_key)

    async def includes(self, item: ItemT):
        return await self._key_set_store.includes(self.const_key, item)


ValueT = TypeVar("ValueT")


@dataclass
class KeyValueStore(GenericStore[ValueT]):
    @redis_retry()
    async def save(self, key: str_able, value: ValueT) -> bool:
        return await self.redis.set(
            self._full_key(key),
            self.dumper(value).encode("utf-8"),
            ex=self.expiration_time,
        )

    @redis_retry()
    async def touch(self, key: str_able) -> bool:
        if self.expiration_time is not None:
            return (await self.redis.expire(self._full_key(key), self.expiration_time)) == 1
        else:
            return True

    @redis_retry()
    async def load(self, key: str_able) -> Optional[ValueT]:
        value_dump = await self.redis.get(self._full_key(key))
        if value_dump is None:
            return None
        return self.loader(value_dump.decode("utf-8"))


@dataclass
class KeyIntegerStore(KeyValueStore[int]):
    dumper: Callable[[int], str] = str
    loader: Callable[[str], int] = int

    @redis_retry()
    async def increment(self, key: str_able, reset_ttl: bool = True) -> int:
        async with self.redis.pipeline() as pipe:
            await pipe.incr(self._full_key(key))
            if reset_ttl and self.expiration_time is not None:
                await pipe.expire(self._full_key(key), self.expiration_time)
            after_incr, *_ = await pipe.execute()
            return cast(int, after_incr)


@dataclass
class KeyFlagStore(GenericStore[bool]):
    @redis_retry()
    async def set_flag(self, key: str_able) -> bool:
        success = await self.redis.set(self._full_key(key), b"1", ex=self.expiration_time)
        return success == 1

    async def is_flag_set(self, key: str_able) -> bool:
        return await self.exists(key)

    async def unset_flag(self, key: str_able) -> bool:
        return await self.drop(key)


@dataclass
class KeyDictStore(GenericStore[ValueT]):
    @redis_retry()
    async def set_subkey(self, key: str_able, subkey: str_able, value: ValueT, reset_ttl: bool = True) -> bool:
        # NOTE: this method copy-pastes most of KeySetStore.add and KeyListStore.push
        #       we need some way to abstract this logic
        async with self.redis.pipeline() as pipe:
            await pipe.hset(self._full_key(key), str(subkey), self.dumper(value).encode("utf-8"))
            if reset_ttl and self.expiration_time is not None:
                await pipe.expire(self._full_key(key), self.expiration_time)
            n_added_keys, *_ = await pipe.execute()
            return n_added_keys == 1

    @redis_retry()
    async def get_subkey(self, key: str_able, subkey: str_able) -> Optional[ValueT]:
        value_dump = await self.redis.hget(self._full_key(key), str(subkey))
        if value_dump is None:
            return None
        return self.loader(value_dump.decode("utf-8"))

    @redis_retry()
    async def list_subkeys(self, key: str_able) -> list[str]:
        subkeys = await self.redis.hkeys(self._full_key(key))
        return [subkey.decode("utf-8") for subkey in subkeys]

    @redis_retry()
    async def list_values(self, key: str_able) -> list[ValueT]:
        value_dumps = await self.redis.hvals(self._full_key(key))
        return [self.loader(dump.decode("utf-8")) for dump in value_dumps]

    @redis_retry()
    async def remove_subkey(self, key: str_able, subkey: str_able) -> bool:
        return await self.redis.hdel(self._full_key(key), str(subkey)) == 1
