import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Generic, Optional, Protocol, TypeVar, cast

from telebot_components.constants.times import MONTH
from telebot_components.redis_utils.interface import RedisInterface

T = TypeVar("T")


class str_able(Protocol):
    def __str__(self) -> str:
        ...


@dataclass
class GenericStore(Generic[T]):
    name: str  # used to identifiy a particular store
    prefix: str  # used to identify bot that uses the store
    redis: RedisInterface
    expiration_time: Optional[timedelta] = MONTH
    dumper: Callable[[T], str] = json.dumps
    loader: Callable[[str], T] = json.loads

    def __post_init__(self):
        self.logger = logging.getLogger(f"{__name__}.{self.prefix}-{self.name}")

    def _full_key(self, key: str_able) -> str:
        return "-".join([self.prefix, self.name, str(key)])

    async def drop(self, key: str_able) -> bool:
        n_deleted = await self.redis.delete(self._full_key(key))
        return n_deleted == 1


ItemT = TypeVar("ItemT")


@dataclass
class KeySetStore(GenericStore[ItemT]):
    async def add(self, key: str_able, item: ItemT, reset_ttl: bool = True) -> bool:
        async with self.redis.pipeline() as pipe:
            await pipe.sadd(self._full_key(key), self.dumper(item).encode("utf-8"))
            if reset_ttl and self.expiration_time is not None:
                await pipe.expire(self._full_key(key), self.expiration_time)
            try:
                results = await pipe.execute()
                return all(r == 1 for r in results)
            except Exception:
                self.logger.exception("Unexpected error adding item")
                return False

    async def remove(self, key: str_able, item: ItemT) -> bool:
        n_removed = await self.redis.srem(self._full_key(key), self.dumper(item).encode("utf-8"))
        return n_removed == 1

    async def all(self, key: str_able) -> set[ItemT]:
        try:
            item_dumps = await self.redis.smembers(self._full_key(key))
            return {self.loader(item_dump.decode("utf-8")) for item_dump in item_dumps}
        except Exception:
            self.logger.exception("Unexpected error retrieving all set items, returning empty set")
            return set()

    async def includes(self, key: str_able, item: ItemT) -> bool:
        return (await self.redis.sismember(self._full_key(key), self.dumper(item).encode("utf-8"))) == 1


@dataclass
class KeyListStore(GenericStore[ItemT]):
    async def push(self, key: str_able, item: ItemT, reset_ttl: bool = True):
        async with self.redis.pipeline() as pipe:
            await pipe.rpush(self._full_key(key), self.dumper(item).encode("utf-8"))
            if reset_ttl and self.expiration_time is not None:
                await pipe.expire(self._full_key(key), self.expiration_time)
            after_push_len, *_ = await pipe.execute()
            return after_push_len

    async def all(self, key: str_able) -> list[ItemT]:
        try:
            item_dumps = await self.redis.lrange(self._full_key(key), 0, -1)
            return [self.loader(item_dump.decode("utf-8")) for item_dump in item_dumps]
        except Exception:
            self.logger.exception("Unexpected error retrieving all list items, returning empty list")
            return []


@dataclass
class SetStore(GenericStore[ItemT]):
    const_key: str = "const"

    def __post_init__(self):
        self._key_set_store = KeySetStore[ItemT](
            name=self.name,
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
    async def save(self, key: str_able, value: ValueT) -> bool:
        return await self.redis.set(
            self._full_key(key),
            self.dumper(value).encode("utf-8"),
            ex=self.expiration_time,
        )

    async def touch(self, key: str_able) -> bool:
        if self.expiration_time is not None:
            return (await self.redis.expire(self._full_key(key), self.expiration_time)) == 1
        else:
            return True

    async def load(self, key: str_able) -> Optional[ValueT]:
        try:
            value_dump = await self.redis.get(self._full_key(key))
            if value_dump is None:
                return None
            return self.loader(value_dump.decode("utf-8"))
        except Exception:
            self.logger.exception("Unexpected error loading value")
            return None


@dataclass
class KeyIntegerStore(KeyValueStore[int]):
    dumper: Callable[[int], str] = str
    loader: Callable[[str], int] = int

    async def increment(self, key: str_able, reset_ttl: bool = True) -> int:
        async with self.redis.pipeline() as pipe:
            await pipe.incr(self._full_key(key))
            if reset_ttl and self.expiration_time is not None:
                await pipe.expire(self._full_key(key), self.expiration_time)
            after_incr, *_ = await pipe.execute()
            return cast(int, after_incr)


@dataclass
class KeyFlagStore(GenericStore[bool]):
    async def set_flag(self, key: str_able) -> bool:
        success = await self.redis.set(self._full_key(key), b"1", ex=self.expiration_time)
        return success == 1

    async def is_flag_set(self, key: str_able) -> bool:
        return (await self.redis.exists(self._full_key(key))) == 1
