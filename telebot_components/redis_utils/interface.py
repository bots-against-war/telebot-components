import datetime
from abc import ABC, abstractmethod
from typing import Mapping, Optional, TypeVar, Union

# type defs copied from redis


class RedisInterface(ABC):
    """Abstract interface for the parts of redis.asyncio.Redis class we're using;
    please update when utilizing new methods.

    Note that Redis must not decode responses and return plain bytes
    (i.e. do NOT specify decode_responses=True option).

    When using real Redis instance in place of RedisInterface, mypy may complain, but we have to ignore it
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
        """Push ``values`` onto the tail of the list ``name`` and return list length the operation"""
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
    ) -> int:
        """
        Set ``key`` to ``value`` within hash ``name``,
        ``mapping`` accepts a dict of key/value pairs that that will be
        added to hash ``name``.
        Returns the number of fields that were added.
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
    async def hdel(self, name: str, *keys: str) -> int:
        """Delete ``keys`` from hash ``name``"""
        ...


RedisCmdReturn = Union[bytes, list[bytes], None, int]


class RedisPipelineInterface(RedisInterface):
    @abstractmethod
    async def execute(self, raise_on_error: bool = True) -> list[RedisCmdReturn]:
        """Execute all the commands in the current pipeline"""
        ...

    @abstractmethod
    async def __aenter__(self) -> "RedisPipelineInterface":
        ...

    @abstractmethod
    async def __aexit__(self, exc_type, exc_value, traceback):
        ...
