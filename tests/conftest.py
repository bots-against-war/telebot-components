import os
import shutil
from typing import AsyncGenerator

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
from tests.utils import TimeSupplier, using_real_redis


@pytest.fixture(params=["ephemeral_emulation", "persistent_emulation"] if not using_real_redis() else ["real"])
async def redis(request: pytest.FixtureRequest) -> AsyncGenerator[RedisInterface, None]:
    redis_type = request.param
    if redis_type == "real":
        redis_temp = Redis.from_url(os.environ["REDIS_URL"], single_connection_client=True)
        for free_db in range(1, 11):
            await redis_temp.select(free_db)
            db_size = await redis_temp.dbsize()
            if db_size == 0:
                break  # found empty database
        else:
            raise RuntimeError("Couldn't found an empy Redis DB for testing")
        await redis_temp.aclose(close_connection_pool=True)

        redis = Redis.from_url(os.environ["REDIS_URL"], db=free_db)
        yield redis
        await redis.flushdb()
        await redis.aclose(close_connection_pool=True)
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
