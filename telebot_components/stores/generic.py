import json
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Callable, Generic, Optional, Protocol, TypeVar, Set

from telebot_components.constants.time import LARGE_EXPIRATION_TIME
from telebot_components.redis.interface import RedisInterface


T = TypeVar("T")


class str_able(Protocol):
    def __str__(self) -> str:
        ...


@dataclass
class GenericStore(Generic[T]):
    name: str  # used to identifiy a particular store
    prefix: str  # used to identify bot that uses the store
    redis: RedisInterface
    expiration_time: timedelta = LARGE_EXPIRATION_TIME
    dumper: Callable[[T], str] = json.dumps
    loader: Callable[[str], T] = json.loads

    def __post_init__(self):
        self.logger = logging.getLogger(f"{__name__}.{self.prefix}-{self.name}")

    def _full_key(self, key: str_able) -> str:
        return "-".join([self.prefix, self.name, str(key)])


SetItemT = TypeVar("SetItemT")


class KeySetStore(GenericStore[SetItemT]):
    async def add(self, key: str_able, item: SetItemT) -> bool:
        async with self.redis.pipeline() as pipe:
            await pipe.sadd(self._full_key(key), self.dumper(item).encode("utf-8"))
            await pipe.expire(self._full_key(key), self.expiration_time)
            try:
                n_added, is_timeout_set = await pipe.execute()
                return n_added == 1 and is_timeout_set == 1
            except Exception:
                self.logger.exception("Unexpected error adding item `")
                return False

    async def remove(self, key: str_able, item: SetItemT) -> bool:
        n_removed = await self.redis.srem(self._full_key(key), self.dumper(item).encode("utf-8"))
        return n_removed == 1

    async def drop(self, key: str_able) -> bool:
        n_deleted = await self.redis.delete(self._full_key(key))
        return n_deleted == 1

    async def all(self, key: str_able) -> Set[SetItemT]:
        try:
            item_dumps = await self.redis.smembers(self._full_key(key))
            return {self.loader(item_dump.decode("utf-8")) for item_dump in item_dumps}
        except Exception:
            self.logger.exception("Unexpected error retrieving all set items, returning empty set")
            return set()


@dataclass
class SetStore(GenericStore[SetItemT]):
    const_key: str = "const"

    def __post_init__(self):
        self._key_set_store = KeySetStore[SetItemT](
            name=self.name,
            prefix=self.prefix,
            redis=self.redis,
            expiration_time=self.expiration_time,
            dumper=self.dumper,
            loader=self.loader,
        )

    async def add(self, item: SetItemT):
        return await self._key_set_store.add(self.const_key, item)

    async def remove(self, item: SetItemT):
        return await self._key_set_store.remove(self.const_key, item)

    async def drop(self):
        return await self._key_set_store.drop(self.const_key)

    async def all(self):
        return await self._key_set_store.all(self.const_key)


ValueT = TypeVar("ValueT")


class KeyValueStore(GenericStore[ValueT]):
    async def save(self, key: str_able, value: ValueT) -> bool:
        return await self.redis.set(
            self._full_key(key),
            self.dumper(value).encode("utf-8"),
            ex=self.expiration_time,
        )

    async def load(self, key: str_able) -> Optional[ValueT]:
        try:
            value_dump = await self.redis.get(self._full_key(key))
            if value_dump is None:
                return None
            return self.loader(value_dump.decode("utf-8"))
        except Exception:
            self.logger.exception("Unexpected error loading value")
            return None

    async def delete(self, key: str_able) -> bool:
        n_deleted = await self.redis.delete(self._full_key(key))
        return n_deleted == 1
