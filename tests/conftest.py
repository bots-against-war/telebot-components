import os
import shutil
from typing import AsyncGenerator, Generator

import pytest
import pytest_mock
import telebot
from aioresponses import aioresponses
from redis.asyncio import Redis  # type: ignore

from telebot_components.redis_utils.emulation import (
    PersistentRedisEmulation,
    RedisEmulation,
)
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import GenericStore
from tests.utils import TimeSupplier, using_real_redis

# HACK: this allows creating multiple distinct stores with the same prefix
GenericStore.RANDOMIZE_PREFIXES = True


# for tests that need normal store behavior (two instances with the same prefix point to the same data)
# can use this hacky fixture
@pytest.fixture
def normal_store_behavior() -> Generator[None, None, None]:
    GenericStore.RANDOMIZE_PREFIXES = False
    yield
    GenericStore.RANDOMIZE_PREFIXES = True


@pytest.fixture(params=["ephemeral_emulation", "persistent_emulation"] if not using_real_redis() else ["real"])
async def redis(request: pytest.FixtureRequest) -> AsyncGenerator[RedisInterface, None]:
    redis_type = request.param
    if redis_type == "real":
        # FIXME: the cleanup does not work properly for some reason...
        redis = Redis.from_url(os.getenv("REDIS_URL"))
        # await redis.flushall()
        for db_index in range(1, 11):
            await redis.select(db_index)
            db_size = await redis.dbsize()
            if db_size == 0:
                break  # found empty database
        else:
            raise RuntimeError("Couldn't found an empy Redis DB for testing")

        yield redis

        # with open("teardown.log", "a") as f:
        #     print("Starting redis teardown", file=f)
        #     await redis.select(db_index)
        #     info = await redis.client_info()
        #     print(info, file=f)

        await redis.flushdb()
        try:
            await redis.connection.disconnect()
        except Exception:
            pass
    elif redis_type == "ephemeral_emulation":
        yield RedisEmulation()
    elif redis_type == "persistent_emulation":
        redis = PersistentRedisEmulation(dirname=".test-redis-emulation")  # type: ignore
        shutil.rmtree(redis._persistent_dir, ignore_errors=True)
        redis.load_persistent_state()
        yield redis
        shutil.rmtree(redis._persistent_dir, ignore_errors=True)
    else:
        raise RuntimeError(f"Unknown redis type: {redis_type}")


@pytest.fixture
def time_supplier(mocker: pytest_mock.MockerFixture) -> TimeSupplier:
    return TimeSupplier(mocker)


@pytest.fixture
async def mock_request() -> AsyncGenerator[aioresponses, None]:
    with aioresponses() as m:
        yield m
    await telebot.api.session_manager.close_session()
