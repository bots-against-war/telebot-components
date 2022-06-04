import os
from typing import AsyncGenerator
from aioresponses import aioresponses

import pytest
import pytest_mock
from redis.asyncio import Redis
import telebot  # type: ignore

from telebot_components.redis_utils.emulation import RedisEmulation
from telebot_components.redis_utils.interface import RedisInterface
from tests.utils import TimeSupplier, using_real_redis


@pytest.fixture
async def redis() -> AsyncGenerator[RedisInterface, None]:
    if using_real_redis():
        redis = Redis.from_url(os.getenv("REDIS_URL"))
        for db_index in range(1, 11):
            await redis.select(db_index)
            async for key in redis.scan_iter("*"):
                break  # at least one key is found - we shouldn't use this db for tests
            else:
                break  # from outer cycle - found empty database
        else:
            raise RuntimeError(f"Didn't found an empy Redis DB for testing")
        yield redis
        await redis.flushdb(asynchronous=False)
    else:
        yield RedisEmulation()


@pytest.fixture
def time_supplier(mocker: pytest_mock.MockerFixture) -> TimeSupplier:
    return TimeSupplier(mocker)



@pytest.fixture
async def mock_request() -> aioresponses:
    with aioresponses() as m:
        yield m
    await telebot.api.session_manager.close_session()
