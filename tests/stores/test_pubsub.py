import asyncio
import datetime
import random
import time
from typing import Any
from uuid import uuid4

import pytest

from telebot_components.redis_utils.emulation import PersistentRedisEmulation, RedisEmulation
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import PubSub


async def test_redis_stream(redis: RedisInterface) -> None:
    if isinstance(redis, (RedisEmulation, PersistentRedisEmulation)):
        pytest.skip("Streams are not emulated")

    pubsub = PubSub[dict[str, Any]](name="example", prefix="bot-prefix", redis=redis)

    group = "group-" + str(uuid4())

    produced_data = [{"data": uuid4().hex, "timestamp": time.time() - random.random() * 10} for _ in range(15)]

    async def producer() -> None:
        await asyncio.sleep(0.1)
        for data in produced_data:
            await pubsub.publish(data)
            await asyncio.sleep(0.1)

    consumed_data: list[dict[str, Any]] = []

    async def consumer(idx: int, is_faulty: bool) -> None:
        while True:
            try:
                async for data in pubsub.consume(
                    group=group,
                    consumer_name=f"consumer-{idx}",
                    consume_at_once=1,
                    auto_retry_after=datetime.timedelta(seconds=2),
                    block_period=datetime.timedelta(seconds=0.1),
                ):
                    await asyncio.sleep(0.01 * random.random())
                    if is_faulty:
                        raise RuntimeError()
                    consumed_data.append(data)
            except RuntimeError:
                pass

    try:
        await asyncio.wait_for(
            asyncio.gather(
                producer(),
                *(consumer(i, is_faulty=False) for i in range(3)),
                consumer(idx=3, is_faulty=True),
            ),
            timeout=10,
        )
    except TimeoutError:
        pass

    key = lambda data: data["timestamp"]  # noqa: E731
    assert sorted(consumed_data, key=key) == sorted(produced_data, key=key)
