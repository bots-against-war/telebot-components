import os
from typing import AsyncGenerator

import pytest
import pytest_mock
import telebot
from aioresponses import aioresponses
from redis.asyncio import Redis  # type: ignore

from telebot_components.redis_utils.emulation import RedisEmulation
from telebot_components.redis_utils.interface import RedisInterface
from tests.utils import TimeSupplier, using_real_redis


@pytest.fixture
async def redis() -> AsyncGenerator[RedisInterface, None]:
    if using_real_redis():
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
    else:
        yield RedisEmulation()


@pytest.fixture
def time_supplier(mocker: pytest_mock.MockerFixture) -> TimeSupplier:
    return TimeSupplier(mocker)


@pytest.fixture
async def mock_request() -> AsyncGenerator[aioresponses, None]:
    with aioresponses() as m:
        yield m
    await telebot.api.session_manager.close_session()
