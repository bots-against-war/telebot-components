import string
from datetime import timedelta
from typing import Any, Callable, Coroutine
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

    key2, value2 = generate_key_value()
    assert await redis.sadd(key2, value2)
    assert await redis.spop(key2) == value2
    assert await redis.spop(key2) is None


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
    values = [letter.encode("utf-8") for letter in string.ascii_letters[:10]]
    for v in values[:5]:
        await redis.rpush(key, v)
    assert await redis.llen(key) == 5
    await redis.rpush(key, *values[5:])
    assert await redis.llen(key) == 10

    assert await redis.lrange(key, 0, -1) == values
    assert await redis.lrange(key, 0, 1) == values[0:2]
    assert await redis.lrange(key, 4, 8) == values[4:9]
    assert await redis.lrange(key, 200, 100) == []
    assert await redis.lrange(key, 1, -2) == values[1:-1]
    assert await redis.lrange(key, 1, -9) == values[1:2]
    assert await redis.lrange(key, 1, -100) == []
    assert await redis.lrange(key, -1, -1) == [values[9]]
    assert await redis.lrange(key, -1000, -100) == []
    assert await redis.lrange("non-existent-key", 0, -1) == []

    assert await redis.lset(key, 5, b"edited") is True
    values_edited = values.copy()
    values_edited[5] = b"edited"
    assert await redis.lrange(key, 0, -1) == values_edited

    assert await redis.ltrim(key, 0, 3) is True
    assert await redis.lrange(key, 0, -1) == values_edited[:4]


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
    return uuid4().hex, uuid4().hex.encode("utf-8")


def generate_values(n: int) -> list[bytes]:
    return [uuid4().hex.encode("utf-8") for _ in range(n)]


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

    assert await redis.hgetall(KEY) == {
        b"1": b"hello",
        b"2": b"world",
        b"3": b"foo",
        b"4": b"bar",
    }

    assert await redis.hlen(KEY) == 4


@pytest.mark.parametrize(
    "hset_kwargs, expected_mapping",
    [
        pytest.param(
            dict(key="k0", value=b"v0"),
            {b"k0": b"v0"},
        ),
        pytest.param(
            dict(mapping={"k1": b"v1", "k2": b"v2"}),
            {b"k1": b"v1", b"k2": b"v2"},
        ),
        pytest.param(
            dict(items=["k3", b"v3", "k4", b"v4"]),
            {b"k3": b"v3", b"k4": b"v4"},
        ),
    ],
)
async def test_hset_multiple(redis: RedisInterface, hset_kwargs: dict[str, Any], expected_mapping: dict[str, bytes]):
    assert await redis.hset("dummy-key", **hset_kwargs)
    assert await redis.hgetall("dummy-key") == expected_mapping


async def test_rpush_rpop(redis: RedisInterface):
    for i in range(10):
        await redis.rpush("my-list-key", str(i).encode())

    assert await redis.rpop("my-list-key") == b"9"
    assert await redis.rpop("my-list-key", 3) == [b"8", b"7", b"6"]
    assert await redis.rpop("my-list-key", 100) == [b"5", b"4", b"3", b"2", b"1", b"0"]

    assert await redis.rpop("doesn't exist") is None
    assert await redis.rpop("doesn't exist", 100) is None


@pytest.mark.parametrize("is_copy", [True, False])
async def test_copy_or_rename_key(redis: RedisInterface, is_copy: bool) -> None:
    async def copy_or_rename(key1: str, key2: str):
        if is_copy:
            assert await redis.copy(key1, key2) is True
        else:
            assert await redis.rename(key1, key2) is True

    key1, key2 = "key1", "key2"
    await redis.set(key1, b"hello")
    await copy_or_rename(key1, key2)
    assert await redis.get(key1) == (b"hello" if is_copy else None)
    assert await redis.get(key2) == b"hello"

    key1, key2 = "key3", "key4"
    await redis.rpush(key1, b"1", b"2", b"3")
    await copy_or_rename(key1, key2)
    assert await redis.lrange(key1, 0, -1) == ([b"1", b"2", b"3"] if is_copy else [])
    assert await redis.lrange(key2, 0, -1) == [b"1", b"2", b"3"]

    key1, key2 = "key5", "key6"
    await redis.hset(key1, "subkey1", b"a")
    await redis.hset(key1, "subkey2", b"b")
    await copy_or_rename(key1, key2)
    assert await redis.hget(key1, "subkey1") == (b"a" if is_copy else None)
    assert await redis.hget(key1, "subkey2") == (b"b" if is_copy else None)
    assert await redis.hget(key2, "subkey1") == b"a"
    assert await redis.hget(key2, "subkey2") == b"b"


async def test_copy_key(redis: RedisInterface) -> None:
    assert not await redis.copy("non-existent", "target")

    await redis.set("source", b"1")
    await redis.set("target", b"2")
    # without replace
    assert not await redis.copy("source", "target", replace=False)
    assert await redis.get("source") == b"1"
    assert await redis.get("target") == b"2"
    # with replace
    assert await redis.copy("source", "target", replace=True)
    assert await redis.get("source") == b"1"
    assert await redis.get("target") == b"1"


async def test_rename_non_existent(redis: RedisInterface) -> None:
    with pytest.raises(Exception):
        assert await redis.rename("key-does-not-exist", "new")
