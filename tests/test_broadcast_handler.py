import asyncio
import time
from dataclasses import dataclass

import pytest
from telebot import types as tg

from telebot_components.broadcast import BroadcastHandler, QueuedBroadcast
from telebot_components.broadcast.message_senders import (
    DataclassMessageSender,
    MessageSenderContext,
)
from telebot_components.redis_utils.interface import RedisInterface
from tests.utils import TimeSupplier

#                  subscriber id, mock message sender id, sent at
sent_messages: list[tuple[int, int, float]] = []


@dataclass(frozen=True)
class MockMessageSender(DataclassMessageSender):
    id_: int

    @classmethod
    def concrete_name(self) -> str:
        return "MockMessageSender"

    async def send(self, context: MessageSenderContext):
        sent_messages.append((context.subscriber["user_id"], self.id_, time.time()))


@pytest.fixture
def broadcast_handler(redis: RedisInterface) -> BroadcastHandler:
    sent_messages.clear()
    return BroadcastHandler(redis, "test")


async def test_broadcast_handler_basic(broadcast_handler: BroadcastHandler, time_supplier: TimeSupplier):
    TOPIC = "foo"

    broadcast_completed = asyncio.Future[None]()

    async def on_broadcast_end(queued_broadcast: QueuedBroadcast):
        broadcast_completed.set_result(None)

    for user_id in range(10000):
        await broadcast_handler.subscribe_to_topic(
            TOPIC,
            tg.User(id=user_id, is_bot=False, first_name="Some", last_name="One", username="hjkahdkjfhaiusdfasdjkhgf"),
        )

    # do not work with redis emulation
    # assert await broadcast_handler.topics() == ['foo']

    background_job_task = asyncio.create_task(
        broadcast_handler.background_job(
            bot=object(),  # type: ignore
            on_broadcast_end=on_broadcast_end,
        )
    )
    await broadcast_handler.new_broadcast(TOPIC, sender=MockMessageSender(1312))
    await broadcast_completed

    assert len(sent_messages) == 10000
