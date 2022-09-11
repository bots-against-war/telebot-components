from datetime import timedelta
from typing import Callable, Coroutine
from uuid import uuid4

import pytest
from _pytest import fixtures

from telebot_components.redis_utils.interface import RedisInterface
from tests.utils import TimeSupplier, pytest_skip_on_real_redis


async def test_basic_set_get(redis: RedisInterface):
    key, value = generate_key_value()
    await redis.set(key, value)
    assert (await redis.get(key)) == value


@pytest_skip_on_real_redis
async def test_set_get_with_expiration(redis: RedisInterface, time_supplier: TimeSupplier):
    key, value = generate_key_value()
    assert (await redis.get(key)) is None
    await redis.set(key, value, ex=timedelta(seconds=30))
    time_supplier.emulate_wait(10)
    assert (await redis.get(key)) == value
    time_supplier.emulate_wait(40)
    assert (await redis.get(key)) is None


@pytest_skip_on_real_redis
async def test_pipelining(redis: RedisInterface, time_supplier: TimeSupplier):
    key, value = generate_key_value()
    await redis.set(key, value, ex=timedelta(seconds=60))
    assert (await redis.get(key)) == value
    async with redis.pipeline() as pipe:
        await pipe.get(key)
        time_supplier.emulate_wait(70)
        await pipe.get(key)
        (get_res_1, get_res_2) = await pipe.execute()
    assert get_res_1 is None
    assert get_res_2 is None


async def test_sets(redis: RedisInterface):
    key, value1 = generate_key_value()
    value2, value3 = generate_values(2)

    for v in (value1, value2, value3):
        await redis.sadd(key, v)
    assert set(await redis.smembers(key)) == {value1, value2, value3}
    assert await redis.sismember(key, value2)
    assert await redis.srem(key, value1) == 1
    assert set(await redis.smembers(key)) == {value2, value3}

    value4, value5, value6 = generate_values(3)
    await redis.sadd(key, value3, value4, value5, value6)
    expected_set = {value2, value3, value4, value5, value6}

    popped_value = await redis.spop(key)
    assert isinstance(popped_value, bytes)
    assert popped_value in expected_set
    expected_set.remove(popped_value)
    assert set(await redis.smembers(key)) == expected_set

    popped_values = await redis.spop(key, count=2)
    assert isinstance(popped_values, list)
    for popped_value in popped_values:
        assert popped_value in expected_set
        expected_set.remove(popped_value)
    assert set(await redis.smembers(key)) == expected_set

    assert await redis.spop("non-existent-key") is None


@pytest_skip_on_real_redis
async def test_set_with_ttl(redis: RedisInterface, time_supplier: TimeSupplier):
    key, _ = generate_key_value()
    value1, value2, value3, value4 = generate_values(4)

    await redis.sadd(key, value1, value2)
    await redis.expire(key, timedelta(seconds=5))
    time_supplier.emulate_wait(15)
    await redis.sadd(key, value3, value4)
    assert set(await redis.smembers(key)) == {value3, value4}


@pytest_skip_on_real_redis
async def test_counter(redis: RedisInterface, time_supplier: TimeSupplier):
    key, _ = generate_key_value()
    assert await redis.incr(key) == 1
    assert await redis.incr(key) == 2
    assert await redis.incr(key) == 3
    assert await redis.get(key) == b"3"
    await redis.expire(key, timedelta(seconds=60))
    time_supplier.emulate_wait(61)
    assert await redis.get(key) is None
    assert await redis.incr(key) == 1
    assert await redis.incr(key) == 2
    assert await redis.get(key) == b"2"


async def test_list(redis: RedisInterface):
    key, _ = generate_key_value()
    assert await redis.lrange(key, 0, -1) == []
    values = generate_values(10)
    for v in values[:5]:
        await redis.rpush(key, v)
    await redis.rpush(key, *values[5:])
    assert await redis.lrange(key, 0, -1) == values
    assert await redis.lrange(key, 0, 1) == values[0:2]
    assert await redis.lrange(key, 4, 8) == values[4:9]
    assert await redis.lrange(key, 200, 100) == []
    assert await redis.lrange(key, 1, -2) == values[1:-1]
    assert await redis.lrange(key, 1, -9) == values[1:2]
    assert await redis.lrange(key, 1, -100) == []


@pytest_skip_on_real_redis
async def test_list_expiration(redis: RedisInterface, time_supplier: TimeSupplier):
    key, _ = generate_key_value()
    values = generate_values(10)
    await redis.rpush(key, *values)
    assert await redis.lrange(key, 0, -1) == values
    await redis.expire(key, timedelta(seconds=5))
    time_supplier.emulate_wait(6)
    assert await redis.lrange(key, 0, 1) == []
    new_values = generate_values(3)
    await redis.rpush(key, *new_values)
    assert await redis.lrange(key, 0, -1) == new_values


@pytest.fixture(params=["set", "sadd", "rpush"])
async def create_key_func(redis: RedisInterface, request: fixtures.SubRequest) -> Callable[[str], Coroutine]:
    method_name: str = request.param

    async def create_key(key: str):
        method = getattr(redis, method_name)
        await method(key, generate_values(1)[0])

    return create_key


@pytest.mark.parametrize(
    "keys, pattern, expected_matching_keys",
    [
        pytest.param(["hello"], "hello", [b"hello"]),
        pytest.param(["hello"], "hell?", [b"hello"]),
        pytest.param(["hello", "hello world"], "hell?", [b"hello"]),
        pytest.param(["hello", "hello world"], "he*", [b"hello", b"hello world"]),
        pytest.param(["one", "two", "three", "four"], "*", [b"one", b"two", b"three", b"four"]),
    ],
)
async def test_keys(
    keys: list[str],
    pattern: str,
    expected_matching_keys: list[str],
    create_key_func: Callable[[str], Coroutine],
    redis: RedisInterface,
):
    for key in keys:
        await create_key_func(key)

    matching_keys = await redis.keys(pattern)
    assert set(matching_keys) == set(expected_matching_keys)


def generate_key_value() -> tuple[str, bytes]:
    return uuid4().hex, uuid4().bytes


def generate_values(n: int) -> list[bytes]:
    return [uuid4().bytes for _ in range(n)]


async def test_hash_operations(redis: RedisInterface):
    assert await redis.hget("some-key", "some-subkey") is None
    assert await redis.hkeys("some-key") == []

    KEY = uuid4().hex
    assert await redis.hset(KEY, "1", b"hello") == 1
    assert await redis.hset(KEY, "2", b"world") == 1
    assert await redis.hset(KEY, "3", b"foo") == 1
    assert await redis.hset(KEY, "4", b"bar") == 1

    assert await redis.hget(KEY, "3") == b"foo"
    assert await redis.hkeys(KEY) == [b"1", b"2", b"3", b"4"]
    assert await redis.hvals(KEY) == [b"hello", b"world", b"foo", b"bar"]
