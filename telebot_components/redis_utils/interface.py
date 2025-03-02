import datetime
from abc import ABC, abstractmethod
from typing import Mapping, Optional, Union

# type defs copied from redis


class RedisInterface(ABC):
    """Abstract interface for the parts of redis.asyncio.Redis class we're using. Update when using
    new methods from Redis, not listed here, and when updating Redis client library.

    Note that this is an interface for Redis configured to not decode responses and return plain bytes
    (i.e. it must not specify decode_responses=True option).

    This is not a true typeshed because Redis library uses a lot of dynamic features and complex inheritance.
    When using real Redis instance in place of RedisInterface, mypy may complain, but we have to ignore it.
    """

    @abstractmethod
    def pipeline(self, transaction: bool = True, shard_hint: Optional[str] = None) -> "RedisPipelineInterface":
        """
        Return a new pipeline object that can queue multiple commands for
        later execution. ``transaction`` indicates whether all commands
        should be executed atomically. Apart from making a group of operations
        atomic, pipelines are useful for reducing the back-and-forth overhead
        between the client and server.

        Note that pipeline implements the same RedisInterface
        """
        ...

    @abstractmethod
    async def set(
        self,
        name: str,
        value: bytes,
        ex: Optional[datetime.timedelta] = None,
        *args,
        **kwargs,
    ) -> bool:
        """
        Set the value at key ``name`` to ``value``
        ``ex`` sets an expire flag on key ``name`` for ``ex`` seconds.
        ``px`` sets an expire flag on key ``name`` for ``px`` milliseconds.
        ``nx`` if set to True, set the value at key ``name`` to ``value`` only
            if it does not exist.
        ``xx`` if set to True, set the value at key ``name`` to ``value`` only
            if it already exists.
        ``keepttl`` if True, retain the time to live associated with the key.
            (Available since Redis 6.0)
        """
        ...

    @abstractmethod
    async def get(self, name: str) -> Optional[bytes]:
        """
        Return the value at key ``name``, or None if the key doesn't exist
        """
        ...

    @abstractmethod
    async def expire(self, name: str, time: datetime.timedelta) -> int:
        """
        Set an expire flag on key ``name`` for ``time`` seconds. ``time``
        can be represented by an integer or a Python timedelta object.
        """
        ...

    @abstractmethod
    async def delete(self, *names: str) -> int:
        """Delete one or more keys specified by ``names`` and return number of deleted keys"""
        ...

    @abstractmethod
    async def copy(
        self,
        source: str,
        destination: str,
        destination_db: Union[str, None] = None,
        replace: bool = False,
    ) -> bool:
        """
        Copy the value stored in the ``source`` key to the ``destination`` key.

        ``destination_db`` an alternative destination database. By default,
        the ``destination`` key is created in the source Redis database.

        ``replace`` whether the ``destination`` key should be removed before
        copying the value to it. By default, the value is not copied if
        the ``destination`` key already exists.

        For more information see https://redis.io/commands/copy
        """
        ...

    @abstractmethod
    async def rename(self, src: str, dst: str) -> bool:
        """
        Rename key ``src`` to ``dst``

        For more information see https://redis.io/commands/rename
        """
        ...

    @abstractmethod
    async def sadd(self, name: str, *values: bytes) -> int:
        """Add ``value(s)`` to set ``name`` and return number of values added to the set"""
        ...

    @abstractmethod
    async def srem(self, name: str, *values: bytes) -> int:
        """Remove ``values`` from set ``name`` and return number of actually removed values"""

    @abstractmethod
    async def smembers(self, name: str) -> list[bytes]:
        """Return all members of the set ``name``"""
        ...

    @abstractmethod
    async def sismember(self, name: str, value: bytes) -> int:
        """Return a boolean indicating if ``value`` is a member of set ``name``"""
        ...

    @abstractmethod
    async def spop(self, name: str, count: Optional[int] = None) -> Optional[Union[bytes, list[bytes]]]:
        """Remove and return a random member of set ``name``, or an array of members, when count is specified"""
        ...

    @abstractmethod
    async def incr(self, name: str) -> int:
        """Increments the value of ``key`` by 1 and return its value after the operation.
        If no key exists, the value will be initialized as 0 and then incremented.
        """
        ...

    @abstractmethod
    async def rpush(self, name: str, *values: bytes) -> int:
        """Push ``values`` onto the tail of the list ``name`` and return list length after the operation"""
        ...

    @abstractmethod
    async def rpop(
        self,
        name: str,
        count: Optional[int] = None,
    ) -> Union[bytes, list[bytes], None]:
        """
        Removes and returns the last elements of the list ``name``.

        By default, the command pops a single element from the end of the list.
        When provided with the optional ``count`` argument, the reply will
        consist of up to count elements, depending on the list's length.
        """
        ...

    @abstractmethod
    async def lrange(self, name: str, start: int, end: int) -> list[bytes]:
        """
        Return a slice of the list ``name`` between
        position ``start`` and ``end``
        ``start`` and ``end`` can be negative numbers just like
        Python slicing notation
        """
        ...

    @abstractmethod
    async def llen(self, name: str) -> int:
        """
        Return the length of the list ``name``

        For more information see https://redis.io/commands/llen
        """
        ...

    @abstractmethod
    async def lset(self, name: str, index: int, value: bytes) -> bool:
        """
        Set element at ``index`` of list ``name`` to ``value``

        For more information see https://redis.io/commands/lset
        """

    @abstractmethod
    async def ltrim(self, name: str, start: int, end: int) -> bool:
        """
        Trim the list ``name``, removing all values not within the slice
        between ``start`` and ``end``

        ``start`` and ``end`` can be negative numbers just like
        Python slicing notation

        For more information see https://redis.io/commands/ltrim
        """
        ...

    @abstractmethod
    async def exists(self, *names: str) -> int:
        """Returns the number of ``names`` that exist"""
        ...

    @abstractmethod
    async def keys(self, pattern: str = "*") -> list[bytes]:
        """Returns a list of keys matching ``pattern``. Note that keys are returned as bytes"""
        ...

    @abstractmethod
    async def hset(
        self,
        name: str,
        key: Optional[str] = None,
        value: Optional[bytes] = None,
        mapping: Optional[Mapping[str, bytes]] = None,
        items: Optional[list[Union[str, bytes]]] = None,
    ) -> int:
        """
        Set ``key`` to ``value`` within hash ``name``,
        ``mapping`` accepts a dict of key/value pairs that will be
        added to hash ``name``.
        ``items`` accepts a list of key/value pairs that will be
        added to hash ``name``.
        Returns the number of fields that were added.

        For more information see https://redis.io/commands/hset
        """
        ...

    @abstractmethod
    async def hget(self, name: str, key: str) -> Optional[bytes]:
        """Return the value of ``key`` within the hash ``name``"""
        ...

    @abstractmethod
    async def hkeys(self, name: str) -> list[bytes]:
        """Return the list of keys within hash ``name``"""
        ...

    @abstractmethod
    async def hvals(self, name: str) -> list[bytes]:
        """Return the list of values within hash ``name``"""
        ...

    @abstractmethod
    async def hlen(self, name: str) -> int:
        """
        Return the number of elements in hash ``name``

        For more information see https://redis.io/commands/hlen
        """
        ...

    @abstractmethod
    async def hgetall(self, name: str) -> dict[bytes, bytes]:
        """
        Return a Python dict of the hash's name/value pairs

        For more information see https://redis.io/commands/hgetall
        """
        ...

    @abstractmethod
    async def hdel(self, name: str, *keys: str) -> int:
        """Delete ``keys`` from hash ``name``"""
        ...

    @abstractmethod
    async def xadd(
        self,
        name: str,
        fields: dict[str, bytes],
        id: int | str = "*",
        maxlen: int | None = None,
        approximate: bool = True,
        nomkstream: bool = False,
        minid: int | str | None = None,
        limit: int | None = None,
    ) -> bytes | None:
        """
        Add to a stream.

        For more information see https://redis.io/commands/xadd
        """
        ...

    @abstractmethod
    async def xack(self, name: str, groupname: str, *ids: int | str | bytes) -> int:
        """
        Acknowledges the successful processing of one or more messages.

        For more information see https://redis.io/commands/xack
        """
        ...

    @abstractmethod
    async def xgroup_create(
        self,
        name: str,
        groupname: str,
        id: int | str = "$",
        mkstream: bool = False,
        entries_read: Optional[int] = None,
    ) -> bool:
        """
        Create a new consumer group associated with a stream.

        For more information see https://redis.io/commands/xgroup-create
        """

    @abstractmethod
    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        start_id: str | int = "0-0",
        count: Union[int, None] = None,
        justid: bool = False,
    ) -> tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]], list[bytes]]:
        """
        Transfers ownership of pending stream entries that match the specified
        criteria. Conceptually, equivalent to calling XPENDING and then XCLAIM,
        but provides a more straightforward way to deal with message delivery
        failures via SCAN-like semantics.

        For more information see https://redis.io/commands/xautoclaim
        """
        ...

    @abstractmethod
    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str | int],
        count: Union[int, None] = None,
        block: Union[int, None] = None,
        noack: bool = False,
    ) -> list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]:
        """
        Read from a stream via a consumer group.

        For more information see https://redis.io/commands/xreadgroup
        """
        ...


RedisCmdReturn = Union[bytes, list[bytes], None, int, str]


class RedisPipelineInterface(RedisInterface):
    @abstractmethod
    async def execute(self, raise_on_error: bool = True) -> list[RedisCmdReturn]:
        """Execute all the commands in the current pipeline"""
        ...

    @abstractmethod
    async def __aenter__(self) -> "RedisPipelineInterface": ...

    @abstractmethod
    async def __aexit__(self, exc_type, exc_value, traceback): ...
