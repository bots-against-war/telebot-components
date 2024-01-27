import asyncio
import copy
import dataclasses
import random
from datetime import timedelta
from typing import Any, Callable, Optional, Type, TypedDict
from uuid import uuid4

import pytest
from _pytest import fixtures

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import (
    KeyDictStore,
    KeyFlagStore,
    KeyIntegerStore,
    KeyListStore,
    KeySetStore,
    KeyValueStore,
    KeyVersionedValueStore,
    SetStore,
    Snapshot,
    Version,
    str_able,
)
from tests.utils import TimeSupplier, generate_str, using_real_redis

EXPIRATION_TIME_TEST_OPTIONS: list[Optional[timedelta]] = [None]

if not using_real_redis():
    EXPIRATION_TIME_TEST_OPTIONS.append(timedelta(seconds=30))


@pytest.fixture(params=EXPIRATION_TIME_TEST_OPTIONS)
def expiration_time(request: fixtures.SubRequest) -> Optional[timedelta]:
    return request.param


@dataclasses.dataclass
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
    @dataclasses.dataclass
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


@pytest.fixture(
    params=[
        lambda: random.randint(0, 10000),
        lambda: "".join(random.choices("stinky", k=150)),
    ]
)
def jsonable_value_factory(request: fixtures.SubRequest) -> Callable[[], Any]:
    return request.param


async def test_key_set_store(redis: RedisInterface, key: str_able, jsonable_value_factory: Callable[[], Any]):
    store = KeySetStore[Any](
        name="testing",
        prefix=generate_str(),
        redis=redis,
    )

    values = [jsonable_value_factory() for _ in range(10)]

    for value in values:
        await store.add(key, value)

    for value in values:
        assert value in await store.all(key)
        assert await store.includes(key, value)

    assert await store.drop(key)
    assert await store.all(key) == set()

    values_one_by_one = [jsonable_value_factory() for _ in range(10)]
    for value in values_one_by_one:
        await store.add(key, value)

    values_bulk = [jsonable_value_factory() for _ in range(10)]
    await store.add_multiple(key, values_bulk)

    values_set = set(values_one_by_one + values_bulk)
    popped_set = set(await store.pop_multiple(key, count=3))
    assert popped_set.issubset(values_set)
    for popped in popped_set:
        values_set.discard(popped)
    assert await store.all(key) == values_set


async def test_set_store(redis: RedisInterface, jsonable_value_factory: Any):
    store = SetStore[Any](
        name="testing",
        prefix=generate_str(),
        redis=redis,
    )
    values = [jsonable_value_factory() for _ in range(10)]
    for value in values:
        await store.add(value)
    for value in values:
        assert value in await store.all()
        assert await store.includes(value)
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
    store_1 = KeyValueStore[str](
        name="testing",
        prefix=bot_prefix,
        redis=redis,
    )
    store_1_keys = [uuid4().hex for _ in range(100)]
    for k in store_1_keys:
        await store_1.save(k, uuid4().hex)

    store_2 = KeyValueStore[str](
        name="testing-something",
        prefix=bot_prefix,
        redis=redis,
    )
    store_2_keys = [uuid4().hex for _ in range(100)]
    for k in store_2_keys:
        await store_2.save(k, uuid4().hex)

    assert set(await store_1.list_keys()) == set(store_1_keys)
    assert set(await store_2.list_keys()) == set(store_2_keys)


async def test_cant_create_conflicting_stores(redis: RedisInterface, normal_store_behavior):
    bot_prefix = generate_str()
    store_1 = KeyValueStore[int](
        name="some-prefix",
        prefix=bot_prefix,
        redis=redis,
    )
    with pytest.raises(ValueError, match="Attempt to create KeyValueStore with prefix "):
        store_2 = KeyValueStore[int](
            name="some-prefix",
            prefix=bot_prefix,
            redis=redis,
        )


async def test_key_dict_store(redis: RedisInterface):
    class UserData(TypedDict):
        name: str
        age: int

    user_data_store = KeyDictStore[UserData](
        name="smth",
        prefix=generate_str(),
        redis=redis,
    )

    await user_data_store.set_subkey("good", 1, UserData(name="alex", age=27))
    await user_data_store.set_subkey("good", 2, UserData(name="maria", age=35))
    await user_data_store.set_subkey("good", 9, UserData(name="sasha", age=21))

    await user_data_store.set_subkey("bad", 1, UserData(name="vlad", age=69))
    await user_data_store.set_subkey("bad", 9, UserData(name="mark", age=25))

    assert await user_data_store.get_subkey("good", 1) == UserData(name="alex", age=27)
    assert await user_data_store.get_subkey("bad", 1) == UserData(name="vlad", age=69)

    assert await user_data_store.load("bad") == {
        "1": UserData(name="vlad", age=69),
        "9": UserData(name="mark", age=25),
    }
    assert await user_data_store.load("good") == {
        "1": UserData(name="alex", age=27),
        "2": UserData(name="maria", age=35),
        "9": UserData(name="sasha", age=21),
    }

    assert set(await user_data_store.list_subkeys("good")) == {"1", "2", "9"}

    good_values = await user_data_store.list_values("good")
    expected_good_values = [
        UserData(name="alex", age=27),
        UserData(name="maria", age=35),
        UserData(name="sasha", age=21),
    ]
    assert len(good_values) == len(expected_good_values)
    for v in good_values:
        assert v in expected_good_values
    for v in expected_good_values:
        assert v in good_values

    assert set(await user_data_store.list_subkeys("bad")) == {"1", "9"}
    bad_values = await user_data_store.list_values("bad")
    expected_bad_values = [
        UserData(name="vlad", age=69),
        UserData(name="mark", age=25),
    ]
    assert len(bad_values) == len(expected_bad_values)
    for v in bad_values:
        assert v in expected_bad_values
    for v in expected_bad_values:
        assert v in bad_values

    await user_data_store.remove_subkey("good", 2)
    assert set(await user_data_store.list_subkeys("good")) == {"1", "9"}
    await user_data_store.remove_subkey("good", "9")
    assert set(await user_data_store.list_subkeys("good")) == {"1"}
    assert await user_data_store.get_subkey("good", "9") is None


@pytest.mark.parametrize("store_class", [KeyValueStore, KeyVersionedValueStore])
async def test_key_versioned_value_store_compat(
    redis: RedisInterface,
    store_class: Type[KeyValueStore] | Type[KeyVersionedValueStore],
    key: str_able,
) -> None:
    store = store_class(
        name="testing-versioning-compat",
        prefix=generate_str(),
        redis=redis,
    )
    assert await store.load(key) is None
    value = generate_str()
    assert await store.save(key, value)
    assert await store.load(key) == value
    assert await store.drop(key)
    assert await store.load(key) is None
    assert await store.save(key, value)


@dataclasses.dataclass
class Thing:
    name: str
    parts: list["Thing"] = dataclasses.field(default_factory=list)

    @classmethod
    def from_dict(cls, dict: dict) -> "Thing":
        return Thing(
            name=dict["name"],
            parts=[Thing.from_dict(el) for el in dict["parts"]],
        )


@dataclasses.dataclass
class Inventory:
    name: str
    stock: list[Thing]
    ordered: list[Thing]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, snapshot: Snapshot) -> "Inventory":
        if not isinstance(snapshot, dict):
            raise TypeError()
        return Inventory(
            name=snapshot["name"],
            stock=[Thing.from_dict(el) for el in snapshot["stock"]],
            ordered=[Thing.from_dict(el) for el in snapshot["ordered"]],
        )


async def test_key_versioned_store(redis: RedisInterface) -> None:
    versioned_store = KeyVersionedValueStore[Inventory, None](
        name="test",
        prefix=generate_str(),
        redis=redis,
        snapshot_dumper=Inventory.to_dict,
        snapshot_loader=Inventory.from_dict,
    )

    expected_versions: list[Inventory] = []

    async def save_version_and_check(inv: Inventory):
        inv = copy.deepcopy(inv)
        expected_versions.append(inv)
        await versioned_store.save("some_key", inv, meta=None)

        assert len(expected_versions) == await versioned_store.count_versions("some_key")

        for version_number, ver in enumerate(expected_versions):
            res = await versioned_store.load_version("some_key", version=version_number)
            assert res is not None, f"failed to load version {version_number}"
            ver_loaded, meta = res
            assert ver == ver_loaded
            assert meta is None

        res = await versioned_store.load_version("some_key", version=len(expected_versions) + 1)
        assert res is None

        res = await versioned_store.load_version("some_key", version=-1)
        assert res is not None
        ver_loaded, meta = res
        assert ver_loaded == expected_versions[-1]
        assert meta is None

    inv = Inventory(name="example", stock=[], ordered=[])
    await save_version_and_check(inv)

    inv.name = "other name"
    await save_version_and_check(inv)

    inv.ordered.append(
        Thing(
            "pc",
            parts=[
                Thing("cpu"),
                Thing("memory"),
                Thing("gpu"),
            ],
        )
    )
    await save_version_and_check(inv)

    inv.ordered.append(Thing("pen"))
    await save_version_and_check(inv)

    inv.ordered[0].parts[2].parts.append(Thing("fan"))
    await save_version_and_check(inv)

    pc = inv.ordered.pop(0)
    inv.stock.append(pc)
    await save_version_and_check(inv)

    inv.stock[0].parts[1].name = "ok"
    inv.stock.insert(0, Thing("book"))
    await save_version_and_check(inv)

    await asyncio.sleep(0.1)  # to let normalization happen in the background
    assert await versioned_store.load_raw_versions("new_key") == []
    assert await versioned_store.load_raw_versions("some_key") == [
        Version(
            snapshot=None,
            backdiff=[{"path": ["name"], "action": "change", "new": "example"}],
            meta=None,
        ),
        Version(
            snapshot=None,
            backdiff=[{"path": ["ordered"], "action": "remove_range", "start": 0, "length": 1}],
            meta=None,
        ),
        Version(
            snapshot=None,
            backdiff=[{"path": ["ordered"], "action": "remove_range", "start": 1, "length": 1}],
            meta=None,
        ),
        Version(
            snapshot=None,
            backdiff=[{"path": ["ordered", 0, "parts", 2, "parts"], "action": "remove_range", "start": 0, "length": 1}],
            meta=None,
        ),
        Version(
            snapshot=None,
            backdiff=[
                {"path": ["stock"], "action": "remove_range", "start": 0, "length": 1},
                {
                    "path": ["ordered"],
                    "action": "add_range",
                    "start": 0,
                    "values": [
                        {
                            "name": "pc",
                            "parts": [
                                {"name": "cpu", "parts": []},
                                {"name": "memory", "parts": []},
                                {"name": "gpu", "parts": [{"name": "fan", "parts": []}]},
                            ],
                        }
                    ],
                },
            ],
            meta=None,
        ),
        Version(
            snapshot=None,
            backdiff=[
                {"path": ["stock", 0, "name"], "action": "change", "new": "pc"},
                {
                    "path": ["stock", 0, "parts"],
                    "action": "add_range",
                    "start": 0,
                    "values": [
                        {"name": "cpu", "parts": []},
                        {"name": "memory", "parts": []},
                        {"name": "gpu", "parts": [{"name": "fan", "parts": []}]},
                    ],
                },
                {"path": ["stock"], "action": "remove_range", "start": 1, "length": 1},
            ],
            meta=None,
        ),
        Version(
            snapshot={
                "name": "other name",
                "stock": [
                    {"name": "book", "parts": []},
                    {
                        "name": "pc",
                        "parts": [
                            {"name": "cpu", "parts": []},
                            {"name": "ok", "parts": []},
                            {"name": "gpu", "parts": [{"name": "fan", "parts": []}]},
                        ],
                    },
                ],
                "ordered": [{"name": "pen", "parts": []}],
            },
            backdiff=None,
            meta=None,
        ),
    ]
