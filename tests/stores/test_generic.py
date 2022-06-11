from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional
from uuid import uuid4

import pytest
from _pytest import fixtures

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import (
    KeyFlagStore,
    KeyIntegerStore,
    KeyListStore,
    KeySetStore,
    KeyValueStore,
    SetStore,
    str_able,
)
from tests.utils import TimeSupplier, generate_str, using_real_redis

EXPIRATION_TIME_TEST_OPTIONS: list[Optional[timedelta]] = [None]

if not using_real_redis():
    EXPIRATION_TIME_TEST_OPTIONS.append(timedelta(seconds=30))


@pytest.fixture(params=EXPIRATION_TIME_TEST_OPTIONS)
def expiration_time(request: fixtures.SubRequest) -> Optional[timedelta]:
    return request.param


@dataclass
class CustomStrableKey:
    data: str

    def __str__(self) -> str:
        return str(hash(self.data))


@pytest.fixture(params=["foo", 1312, b"abcd", 3.1415, CustomStrableKey("hey there")])
def key(request: fixtures.SubRequest) -> str_able:
    return request.param


async def test_key_value_store(
    redis: RedisInterface, expiration_time: Optional[timedelta], key: str_able, time_supplier: TimeSupplier
):
    store = KeyValueStore[str](
        name="testing",
        prefix=generate_str(),
        redis=redis,
        expiration_time=expiration_time,
    )
    assert await store.load(key) is None
    value = generate_str()
    assert await store.save(key, value)
    assert await store.load(key) == value
    assert await store.drop(key)
    assert await store.load(key) is None
    assert await store.save(key, value)
    if expiration_time is not None:
        time_supplier.emulate_wait(expiration_time.total_seconds() + 1)
        assert await store.load(key) is None
    else:
        assert await store.load(key) == value


@pytest.fixture(
    params=[
        42,
        "hello world",
        ["potato", "cabbage"],
        {"mapping": "very good data structure", "array": ["sucks", "really", "bad"]},
        [1, 2, 3, {"key": "value"}],
        {"id": 1312, "nested": {"more-nested": {"text": "damn", "is_cool": True}, "something-else": ["what?", 1]}},
    ]
)
def jsonable_value(request: fixtures.SubRequest) -> Any:
    return request.param


async def test_key_value_store_json_serialization(redis: RedisInterface, key: str_able, jsonable_value: Any):
    store = KeyValueStore[Any](
        name="testing",
        prefix=generate_str(),
        redis=redis,
    )
    assert await store.load(key) is None
    assert await store.save(key, jsonable_value)
    assert await store.load(key) == jsonable_value


async def test_key_value_store_custom_serialization(redis: RedisInterface, key: str_able):
    @dataclass
    class UserData:
        name: str
        age: int

        def to_store(self) -> str:
            return f"{self.name}-{self.age}"

        @classmethod
        def from_store(cls, dump: str) -> "UserData":
            name, age_str = dump.split("-")
            return UserData(name=name, age=int(age_str))

    store = KeyValueStore[UserData](
        name="testing",
        prefix=generate_str(),
        redis=redis,
        dumper=lambda ud: ud.to_store(),
        loader=UserData.from_store,
    )
    value = UserData("shirley", 32)
    assert await store.save(key, value)
    assert await store.load(key) == value
    assert await store.drop(key)


async def test_key_list_store(redis: RedisInterface, key: str_able, jsonable_value: Any):
    store = KeyListStore[Any](
        name="testing",
        prefix=generate_str(),
        redis=redis,
    )
    for _ in range(10):
        assert await store.push(key, jsonable_value)
    assert await store.all(key) == [jsonable_value] * 10
    assert await store.drop(key)
    assert await store.all(key) == []


@pytest.fixture(params=[17, "stinky", None])
def hashable_value(request: fixtures.SubRequest) -> Any:
    return request.param


async def test_key_set_store(redis: RedisInterface, key: str_able, hashable_value: Any):
    store = KeySetStore[Any](
        name="testing",
        prefix=generate_str(),
        redis=redis,
    )
    for _ in range(10):
        await store.add(key, hashable_value)
    assert hashable_value in await store.all(key)
    assert await store.includes(key, hashable_value)
    assert await store.drop(key)
    assert await store.all(key) == set()


async def test_set_store(redis: RedisInterface, hashable_value: Any):
    store = SetStore[Any](
        name="testing",
        prefix=generate_str(),
        redis=redis,
    )
    for _ in range(10):
        await store.add(hashable_value)
    assert hashable_value in await store.all()
    assert await store.includes(hashable_value)
    assert await store.drop()
    assert await store.all() == set()


async def test_integer_store(redis: RedisInterface, key: str_able):
    store = KeyIntegerStore(
        name="testing",
        prefix=generate_str(),
        redis=redis,
    )
    assert await store.load(key) is None
    assert await store.increment(key) == 1
    assert await store.increment(key) == 2
    assert await store.increment(key) == 3
    assert await store.drop(key)
    assert await store.load(key) is None
    assert await store.increment(key) == 1


async def test_flag_store(redis: RedisInterface, key: str_able):
    store = KeyFlagStore(
        name="testing",
        prefix=generate_str(),
        redis=redis,
    )
    assert not (await store.is_flag_set(key))
    assert await store.set_flag(key)
    assert await store.is_flag_set(key)


async def test_list_keys(redis: RedisInterface):
    bot_prefix = generate_str()
    store_1 = KeyValueStore(
        name="testing",
        prefix=bot_prefix,
        redis=redis,
    )
    store_1_keys = [uuid4().hex for _ in range(100)]
    for k in store_1_keys:
        await store_1.save(k, uuid4().hex)

    store_2 = KeyValueStore(
        name="testing-something",
        prefix=bot_prefix,
        redis=redis,
    )
    store_2_keys = [uuid4().hex for _ in range(100)]
    for k in store_2_keys:
        await store_2.save(k, uuid4().hex)

    assert set(await store_1.list_keys()) == set(store_1_keys)
    assert set(await store_2.list_keys()) == set(store_2_keys)


async def test_cant_create_conflicting_stores(redis: RedisInterface):
    bot_prefix = generate_str()
    KeyValueStore(
        name="some-prefix",
        prefix=bot_prefix,
        redis=redis,
    )
    with pytest.raises(ValueError, match="Attempt to create KeyValueStore with prefix "):
        KeyValueStore(
            name="some-prefix",
            prefix=bot_prefix,
            redis=redis,
        )
