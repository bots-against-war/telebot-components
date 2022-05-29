import datetime
from abc import ABC, abstractmethod
from typing import Optional, TypeVar, Union

# type defs copied from redis

AbsExpiryT = Union[int, datetime.datetime]
ExpiryT = Union[int, datetime.timedelta]
KeyT = str  # Main redis key space


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
        ex: Optional[ExpiryT] = None,
        px: Optional[ExpiryT] = None,
        nx: bool = False,
        xx: bool = False,
        keepttl: bool = False,
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
    async def expire(self, name: str, time: ExpiryT) -> int:
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
    async def sismember(self, name: KeyT, value: bytes) -> int:
        """Return a boolean indicating if ``value`` is a member of set ``name``"""
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
