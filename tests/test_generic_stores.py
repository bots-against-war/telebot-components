from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional
from uuid import uuid4
import pytest
from _pytest import fixtures

from telebot_components.stores.generic import KeyListStore, KeyValueStore, str_able
from telebot_components.redis_utils.interface import RedisInterface

from tests.utils import TimeSupplier, using_real_redis


EXPIRATION_TIME_TEST_OPTIONS = [None]

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


def generate_str() -> str:
    return uuid4().hex


async def test_key_value_store(
    redis: RedisInterface, expiration_time: Optional[timedelta], key: str_able, time_supplier: TimeSupplier
) -> KeyValueStore:
    store = KeyValueStore[str](
        name="testing",
        prefix="test-bot",
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



@pytest.fixture(params=[
        42,
        "hello world",
        ["potato", "cabbage"],
        {"mapping": "very good data structure", "array": ["sucks", "really", "bad"]},
        [1, 2, 3, {"key": "value"}],
        {"id": 1312, "nested": {"more-nested": {"text": "damn", "is_cool": True}, "something-else": ["what?", 1]}},
    ])
def jsonable_value(request: fixtures.SubRequest) -> Any:
    return request.param


async def test_key_value_store_json_serialization(redis: RedisInterface, key: str_able, jsonable_value: Any) -> KeyValueStore:
    store = KeyValueStore(
        name="testing",
        prefix="test-bot",
        redis=redis,
    )
    assert await store.load(key) is None
    assert await store.save(key, jsonable_value)
    assert await store.load(key) == jsonable_value


async def test_key_value_store_custom_serialization(redis: RedisInterface, key: str_able) -> KeyValueStore:
    @dataclass
    class UserData:
        name: str
        age: int

        def to_store(self) -> str:
            return f"{self.name}-{self.age}"
        
        @classmethod
        def from_store(cls, dump: str) -> 'UserData':
            name, age_str = dump.split("-")
            return UserData(name=name, age=int(age_str))

    store = KeyValueStore[UserData](
        name="testing",
        prefix="test-bot",
        redis=redis,
        dumper=lambda ud: ud.to_store(),
        loader=UserData.from_store,
    )
    value = UserData("shirley", 32)
    assert await store.save(key, value)
    assert await store.load(key) == value
    assert await store.drop(key)


async def test_key_list_store(redis: RedisInterface, key: str_able, jsonable_value: Any):
    store = KeyListStore(
        name="testing",
        prefix="test-bot",
        redis=redis,
    )
    for _ in range(10):
        assert await store.push(key, jsonable_value)
    assert await store.all(key) == [jsonable_value] * 10
    assert await store.drop(key)
    assert await store.all(key) == []
