import asyncio
import dataclasses
import datetime
import json
import logging
import uuid
from hashlib import md5
from typing import (
    Callable,
    Generator,
    Generic,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    TypeVar,
    cast,
)

import tenacity

from telebot_components.constants.times import MONTH
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.utils import tail
from telebot_components.utils.diff import (
    Diffable,
    DiffAction,
    InplacePatchImpossible,
    diff,
    patch,
)

T = TypeVar("T")


class str_able(Protocol):
    def __str__(self) -> str:
        ...


logger = logging.getLogger(__name__)


WrappedFuncT = TypeVar("WrappedFuncT")


def redis_retry() -> Callable[[WrappedFuncT], WrappedFuncT]:
    return tenacity.retry(  # type: ignore
        wait=tenacity.wait.wait_random_exponential(multiplier=1, max=30, exp_base=2, min=0.5),
        stop=tenacity.stop.stop_after_delay(max_delay=60),
        retry=tenacity.retry_if_exception_type(),
        after=tenacity.after.after_log(logger, log_level=logging.WARNING),
    )


@dataclasses.dataclass
class PrefixedStore:
    """
    Base store class that handles prefixing and also keeps prefix registry to prevent
    duplicate prefix stores and data corruption. Does not implement any data handling
    """

    name: str  # used to identify a particular store
    prefix: str  # used to identify bot that uses the store
    redis: RedisInterface

    def __post_init__(self):
        self.logger = logging.getLogger(f"{__name__}[{self.prefix}-{self.name}]")
        # adding prefix hash to allow stores with nested prefixes
        # e.g. stores with prefixes 'a' and 'ab' could cause a collision but
        # we transform them to 'a-0cc17' and 'ab-187ef' and voila
        plain_prefix = f"{self.prefix}-{self.name}"
        prefix_hash = md5(plain_prefix.encode("utf-8")).hexdigest()[:5]
        self._full_prefix = f"{plain_prefix}-{prefix_hash}-"

    @classmethod
    def allow_duplicate_stores(cls, prefix: str):
        logger.warning("allow_duplicate_stores is noop now, duplicate stores are globally allowed")


@dataclasses.dataclass
class SingleKeyStore(PrefixedStore, Generic[T]):
    """
    Common base class for stores that use a single key to store one entity
    (that is, most of them). Provides some common read methods, write
    methods are defined in subclasses.
    """

    expiration_time: Optional[datetime.timedelta] = MONTH
    dumper: Callable[[T], str] = json.dumps
    loader: Callable[[str], T] = json.loads

    def _full_key(self, key: str_able) -> str:
        return f"{self._full_prefix}{key}"

    @redis_retry()
    async def drop(self, key: str_able) -> bool:
        n_deleted = await self.redis.delete(self._full_key(key))
        return n_deleted == 1

    @redis_retry()
    async def copy(self, key: str_able, new_key: str_able) -> bool:
        return (
            await self.redis.copy(
                self._full_key(key),
                self._full_key(new_key),
                replace=True,
            )
            == 1
        )

    @redis_retry()
    async def rename(self, key: str_able, to: str_able) -> bool:
        return await self.redis.rename(src=self._full_key(key), dst=self._full_key(to)) is True

    @redis_retry()
    async def manual_expire(self, key: str_able, ttl: datetime.timedelta) -> None:
        await self.redis.expire(self._full_key(key), ttl)

    @redis_retry()
    async def exists(self, key: str_able) -> bool:
        return (await self.redis.exists(self._full_key(key))) == 1

    @redis_retry()
    async def list_keys(self) -> list[str]:
        return await self.find_keys(pattern="*")

    @redis_retry()
    async def find_keys(self, pattern: str) -> list[str]:
        matching_full_keys = await self.redis.keys(self._full_prefix + pattern)
        return [fk.decode("utf-8").removeprefix(self._full_prefix) for fk in matching_full_keys]


# old name for backwrads compatibility
GenericStore = SingleKeyStore


ItemT = TypeVar("ItemT")


@dataclasses.dataclass
class KeySetStore(SingleKeyStore[ItemT]):
    async def add(self, key: str_able, item: ItemT, reset_ttl: bool = True) -> bool:
        return await self.add_multiple(key, (item,), reset_ttl)

    @redis_retry()
    async def add_multiple(self, key: str_able, items: Iterable[ItemT], reset_ttl: bool = True) -> bool:
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


@dataclasses.dataclass
class KeyListStore(SingleKeyStore[ItemT]):
    @redis_retry()
    async def push_multiple(self, key: str_able, items: Iterable[ItemT], reset_ttl: bool = True) -> int:
        async with self.redis.pipeline() as pipe:
            await pipe.rpush(self._full_key(key), *[self.dumper(item).encode("utf-8") for item in items])
            if reset_ttl and self.expiration_time is not None:
                await pipe.expire(self._full_key(key), self.expiration_time)
            after_push_len, *_ = await pipe.execute()
            return cast(int, after_push_len)

    async def push(self, key: str_able, item: ItemT, reset_ttl: bool = True) -> int:
        return await self.push_multiple(key, (item,), reset_ttl=reset_ttl)

    @redis_retry()
    async def slice(self, key: str_able, start: int, end: int) -> list[ItemT] | None:
        """End index is inclusive, according to Redis convention and unlike Python convention"""
        item_dumps = await self.redis.lrange(self._full_key(key), start, end)
        return [self.loader(item_dump.decode("utf-8")) for item_dump in item_dumps] or None

    async def tail(self, key: str_able, start: int) -> list[ItemT] | None:
        return await self.slice(key, start=start, end=-1)

    async def all(self, key: str_able) -> list[ItemT]:
        return await self.tail(key, start=0) or []

    @redis_retry()
    async def length(self, key: str_able) -> int:
        return await self.redis.llen(self._full_key(key))

    @redis_retry()
    async def set(self, key: str_able, i: int, value: ItemT, reset_ttl: bool = True) -> bool:
        async with self.redis.pipeline() as pipe:
            await pipe.lset(self._full_key(key), i, self.dumper(value).encode("utf-8"))
            if reset_ttl and self.expiration_time is not None:
                await pipe.expire(self._full_key(key), self.expiration_time)
            after_push_len, *_ = await pipe.execute()
            return after_push_len is True

    @redis_retry()
    async def trim(self, key: str_able, last: int) -> None:
        await self.redis.ltrim(self._full_key(key), 0, last)


@dataclasses.dataclass
class SetStore(PrefixedStore, Generic[ItemT]):
    expiration_time: Optional[datetime.timedelta] = MONTH
    dumper: Callable[[ItemT], str] = json.dumps
    loader: Callable[[str], ItemT] = json.loads
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

    async def drop(self):
        return await self._key_set_store.drop(self.const_key)

    async def add(self, item: ItemT):
        return await self._key_set_store.add(self.const_key, item)

    async def remove(self, item: ItemT):
        return await self._key_set_store.remove(self.const_key, item)

    async def all(self):
        return await self._key_set_store.all(self.const_key)

    async def includes(self, item: ItemT):
        return await self._key_set_store.includes(self.const_key, item)


ValueT = TypeVar("ValueT")


@dataclasses.dataclass
class KeyValueStore(SingleKeyStore[ValueT]):
    @redis_retry()
    async def save(self, key: str_able, value: ValueT) -> bool:
        return await self.redis.set(
            self._full_key(key),
            self.dumper(value).encode("utf-8"),
            ex=self.expiration_time,
        )

    @redis_retry()
    async def save_multiple(self, mapping: Mapping[str, ValueT]) -> bool:
        async with self.redis.pipeline() as pipe:
            for key, value in mapping.items():
                await pipe.set(
                    self._full_key(key),
                    self.dumper(value).encode("utf-8"),
                    ex=self.expiration_time,
                )
            return all(await pipe.execute())

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

    @redis_retry()
    async def load_multiple(self, keys: Iterable[str_able]) -> list[Optional[ValueT]]:
        async with self.redis.pipeline() as pipe:
            for key in keys:
                await pipe.get(self._full_key(key))
            value_dumps: list[Optional[bytes]] = await pipe.execute()  # type: ignore
        return [self.loader(v.decode("utf-8")) if v is not None else None for v in value_dumps]


@dataclasses.dataclass
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


@dataclasses.dataclass
class KeyFlagStore(SingleKeyStore[bool]):
    @redis_retry()
    async def set_flag(self, key: str_able) -> bool:
        success = await self.redis.set(self._full_key(key), b"1", ex=self.expiration_time)
        return success == 1

    async def is_flag_set(self, key: str_able) -> bool:
        return await self.exists(key)

    async def unset_flag(self, key: str_able) -> bool:
        return await self.drop(key)


@dataclasses.dataclass
class KeyDictStore(SingleKeyStore[ValueT]):
    @redis_retry()
    async def set_multiple_subkeys(
        self,
        key: str_able,
        subkey_to_value: Mapping[str_able, ValueT],
        reset_ttl: bool = True,
    ) -> bool:
        async with self.redis.pipeline() as pipe:
            await pipe.hset(
                self._full_key(key),
                mapping={str(subkey): self.dumper(value).encode("utf-8") for subkey, value in subkey_to_value.items()},
            )
            if reset_ttl and self.expiration_time is not None:
                await pipe.expire(self._full_key(key), self.expiration_time)
            n_added_keys, *_ = await pipe.execute()
            return n_added_keys == len(subkey_to_value)

    async def set_subkey(self, key: str_able, subkey: str_able, value: ValueT, reset_ttl: bool = True) -> bool:
        return await self.set_multiple_subkeys(key, {subkey: value}, reset_ttl=reset_ttl)

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
    async def count_values(self, key: str_able) -> int:
        return await self.redis.hlen(self._full_key(key))

    @redis_retry()
    async def load(self, key: str_able) -> dict[str, ValueT]:
        raw = await self.redis.hgetall(self._full_key(key))
        return {raw_key.decode("utf-8"): self.loader(raw_value.decode("utf-8")) for raw_key, raw_value in raw.items()}

    @redis_retry()
    async def remove_subkey(self, key: str_able, subkey: str_able) -> bool:
        return await self.redis.hdel(self._full_key(key), str(subkey)) == 1


VersionMetaT = TypeVar("VersionMetaT")  # must be jsonable type, e.g. string, dict or list
Snapshot = Diffable
Diff = list[DiffAction]


@dataclasses.dataclass
class Version(Generic[VersionMetaT]):
    snapshot: Snapshot | None
    backdiff: Diff | None  # diff from *next* version
    meta: VersionMetaT | None

    def dump(self) -> str:
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def load(cls, dump: str) -> "Version":
        return Version(**json.loads(dump))


@dataclasses.dataclass
class VersionCorruptionError(Exception):
    errmsg: str
    store_prefix: str
    key: str
    version_offset: int

    def __str__(self) -> str:
        return (
            f"{self.__class__.__name__} at {self.store_prefix}[{self.key}] "
            + f"offset {self.version_offset}: {self.errmsg}"
        )


@dataclasses.dataclass
class KeyVersionedValueStore(PrefixedStore, Generic[ValueT, VersionMetaT]):
    """
    Key-value store for structured data with multiple versions of the value available.
    Note that diffing is performed on data structure, not serialized value. So, for simple
    values like strings the store just stores all version values
    """

    snapshot_dumper: Callable[[ValueT], Snapshot] = lambda x: cast(Snapshot, x)
    snapshot_loader: Callable[[Snapshot], ValueT] = lambda x: cast(ValueT, x)

    def __post_init__(self) -> None:
        super().__post_init__()
        self._version_store = KeyListStore[Version[VersionMetaT]](
            name=f"{self.name}/versions",
            prefix=self.prefix,
            redis=self.redis,
            expiration_time=None,
            dumper=Version.dump,
            loader=Version.load,
        )
        self._background_tasks: set[asyncio.Task[None]] = set()

    # proxied methods

    async def drop(self, key: str_able) -> bool:
        return await self._version_store.drop(key)

    async def exists(self, key: str_able) -> bool:
        return await self._version_store.exists(key)

    async def list_keys(self) -> list[str]:
        return await self._version_store.list_keys()

    async def find_keys(self, pattern: str) -> list[str]:
        return await self._version_store.find_keys(pattern)

    async def load_raw_versions(self, key: str_able, start_version: int = 0) -> list[Version[VersionMetaT]]:
        return await self._version_store.tail(key, start=start_version) or []

    def _iter_versions(
        self,
        versions: list[Version],
        key: str,
    ) -> Generator[tuple[Snapshot, Diff | None], None, None]:
        """Generic iterator across versions, yielding both snapshot and backdiff representations"""
        if not versions:
            return

        current = versions[-1].snapshot
        if current is None:
            raise VersionCorruptionError(
                errmsg="The last version doesn't contain snapshot",
                store_prefix=self._full_prefix,
                key=key,
                version_offset=0,
            )
        yield current, None

        for i, version in enumerate(reversed(versions[:-1])):
            backdiff = version.backdiff
            if backdiff is not None:
                try:
                    patch(current, backdiff, in_place=True)
                except InplacePatchImpossible as e:
                    current = e.patched_value
                yield current, backdiff
            else:
                if version.snapshot is None:
                    raise VersionCorruptionError(
                        errmsg="Version does not contain snapshot nor backdiff",
                        store_prefix=self._full_prefix,
                        key=str(key),
                        version_offset=i + 1,
                    )
                backdiff = diff(current, version.snapshot)
                current = version.snapshot
                yield current, backdiff

    async def _normalize(self, key: str_able) -> None:
        try:
            self.logger.debug(f"Normalizing {key} converting snapshots to diff")
            versions = await self._version_store.all(key)
            self.logger.debug(f"Got {len(versions) = }")
            for offset, (_, backdiff) in enumerate(self._iter_versions(versions, key=str(key))):
                if backdiff is None:
                    continue
                index = len(versions) - 1 - offset
                current_version = versions[index]
                if current_version.backdiff is None:
                    self.logger.debug(f"Converting snapshot -> backdiff at offset {offset} (#{index})")
                    await self._version_store.set(
                        key,
                        index,
                        Version(
                            snapshot=None,
                            backdiff=backdiff,
                            meta=current_version.meta,
                        ),
                    )
        except Exception:
            self.logger.exception("Error converting values from snapshots to diffs")

    async def save(self, key: str_able, value: ValueT, meta: VersionMetaT | None = None) -> bool:
        added = await self._version_store.push(
            key,
            Version(
                snapshot=self.snapshot_dumper(value),
                backdiff=None,
                meta=meta,
            ),
        )
        snapshot_to_diff_task = asyncio.create_task(self._normalize(key))
        self._background_tasks.add(snapshot_to_diff_task)
        snapshot_to_diff_task.add_done_callback(self._background_tasks.discard)
        return added == 1

    async def count_versions(self, key: str_able) -> int:
        return await self._version_store.length(key)

    async def load_version(self, key: str_able, version: int = -1) -> tuple[ValueT, VersionMetaT | None] | None:
        versions = await self._version_store.tail(key, start=version)
        if versions is None:
            return None
        version_snapshot, _ = next(tail(1, self._iter_versions(versions, key=str(key))))
        return self.snapshot_loader(version_snapshot), versions[0].meta

    async def load(self, key: str_able) -> ValueT | None:
        """KeyValueStore-compatible method, see also load_version"""
        if res := await self.load_version(key, version=-1):
            return res[0]
        else:
            return None

    async def revert(self, key: str_able, to_version: int) -> tuple[ValueT, VersionMetaT | None] | None:
        if not await self._version_store.exists(key):
            return None

        temp_key = str(uuid.uuid4())
        await self._version_store.copy(key, temp_key)
        await self._version_store.manual_expire(temp_key, ttl=datetime.timedelta(minutes=30))

        versions = await self._version_store.tail(temp_key, start=to_version)
        if versions is None:
            await self._version_store.drop(temp_key)
            return None
        snapshot, _ = next(tail(1, self._iter_versions(versions, key=temp_key)))
        new_last_version = Version(snapshot=snapshot, backdiff=None, meta=versions[0].meta)

        await self._version_store.set(temp_key, i=to_version, value=new_last_version)
        await self._version_store.trim(temp_key, last=to_version)
        await self._version_store.rename(temp_key, key)
        return self.snapshot_loader(snapshot), new_last_version.meta
