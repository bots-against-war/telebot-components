import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from functools import partial
from typing import Any, Awaitable, Callable, Optional, TypeVar

from telebot import AsyncTeleBot
from telebot import api as telegram_api
from telebot import types as tg
from telebot.graceful_shutdown import PreventShutdown

from telebot_components.broadcast.message_senders import (
    AbstractMessageSender,
    MessageSenderContext,
)
from telebot_components.broadcast.subscriber import Subscriber
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import KeyDictStore, KeySetStore, KeyValueStore
from telebot_components.utils import restart_on_errors

prevent_shutdown_on_consuming_queue = PreventShutdown("consuming broadcast queue")
prevent_shutdown_on_broadcasting = PreventShutdown("broadcasting")


@dataclass(frozen=True)
class QueuedBroadcast:
    sender: AbstractMessageSender
    topic: str
    start_time: float

    def dump(self) -> str:
        return json.dumps(
            {
                "sender": self.sender.dump(),
                "topic": self.topic,
                "start_time": self.start_time,
            }
        )

    @classmethod
    def load(cls, dump: str) -> "QueuedBroadcast":
        raw = json.loads(dump)
        return QueuedBroadcast(
            sender=AbstractMessageSender.load(raw["sender"]),
            topic=raw["topic"],
            start_time=raw["start_time"],
        )


TopicActionResultT = TypeVar("TopicActionResultT")
BroadcastCallback = Callable[[QueuedBroadcast], Awaitable[Any]]


class BroadcastHandler:
    def __init__(
        self,
        redis: RedisInterface,
        bot_prefix: str,
        topic_priority_key: Callable[[str], float] = lambda _: random.random(),
    ):
        self.topic_priority_key = topic_priority_key
        self.is_broadcasting = False
        self.next_broadcast_queue_processing_time = 0.0
        self.subscribers_by_topic_store = KeyDictStore[Subscriber](
            name="subscribers-by-topic",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=None,
        )
        self.current_broadcast_by_topic_store = KeyValueStore[QueuedBroadcast](
            name="current-message-sender-by-topic",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=None,
            dumper=lambda qb: qb.dump(),
            loader=QueuedBroadcast.load,
        )
        self.current_pending_subscribers_by_topic_store = KeySetStore[Subscriber](
            name="current-pending-subscribers-by-topic",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=None,
        )
        self.broadcast_queue_store = KeySetStore[QueuedBroadcast](
            name="queued-broadcasts",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=None,
            dumper=lambda qb: qb.dump(),
            loader=QueuedBroadcast.load,
        )
        self.logger = logging.getLogger(__name__ + f"[{bot_prefix}]")

    async def topics(self) -> list[str]:
        """At least one person should be subscribed to the topic"""
        return await self.subscribers_by_topic_store.list_keys()

    async def currently_broadcasting_topics(self) -> list[str]:
        """Topics we're broadcasting on at the moment"""
        return await self.current_broadcast_by_topic_store.list_keys()

    async def subscribe_to_topic(self, topic: str, user: tg.User) -> bool:
        return await self.subscribers_by_topic_store.set_subkey(
            key=topic,
            subkey=user.id,
            value=Subscriber(
                user_id=user.id,
                username=user.username,
                full_name=user.full_name,
                subscribed_at=time.time(),
            ),
        )

    async def _map_topics(self, func: Callable[[str], Awaitable[TopicActionResultT]]) -> dict[str, TopicActionResultT]:
        results: dict[str, TopicActionResultT] = dict()
        for topic in await self.topics():
            results[topic] = await func(topic)
        return results

    async def subscribe_to_all_topics(self, user: tg.User) -> bool:
        subscribe_results = await self._map_topics(partial(self.subscribe_to_topic, user=user))
        return all(subscribe_results.values())

    async def unsubscribe_from_topic(self, topic: str, user: tg.User) -> bool:
        return await self.subscribers_by_topic_store.remove_subkey(key=topic, subkey=user.id)

    async def unsubscribe_from_all_topics(self, user: tg.User) -> bool:
        unsubscribe_results = await self._map_topics(partial(self.unsubscribe_from_topic, user=user))
        return all(unsubscribe_results.values())

    async def topic_subscribers(self, topic: str) -> list[Subscriber]:
        return await self.subscribers_by_topic_store.list_values(topic)

    async def count_subscribers(self, topic: str) -> int:
        return await self.subscribers_by_topic_store.count_values(topic)

    async def all_subscribers(self) -> dict[str, list[Subscriber]]:
        return await self._map_topics(self.topic_subscribers)

    CONST_KEY = "const"

    async def new_broadcast(
        self, topic: str, sender: AbstractMessageSender, schedule_at: Optional[float] = None
    ) -> bool:
        queued_broadcast = QueuedBroadcast(sender=sender, topic=topic, start_time=schedule_at or time.time())
        if await self.broadcast_queue_store.add(self.CONST_KEY, queued_broadcast):
            self.next_broadcast_queue_processing_time = min(
                self.next_broadcast_queue_processing_time, queued_broadcast.start_time
            )
            return True
        else:
            return False

    async def background_job(
        self,
        bot: AsyncTeleBot,
        on_broadcast_start: Optional[BroadcastCallback] = None,
        on_broadcast_end: Optional[BroadcastCallback] = None,
    ):
        await asyncio.gather(
            self._consume_broadcasts_queue(on_broadcast_start),
            self._send_current_broadcasts(bot, on_broadcast_end),
        )

    @prevent_shutdown_on_consuming_queue
    @restart_on_errors
    async def _consume_broadcasts_queue(self, on_broadcast_start: Optional[BroadcastCallback] = None):
        while True:
            if time.time() < self.next_broadcast_queue_processing_time:
                async with prevent_shutdown_on_consuming_queue.allow_shutdown():
                    await asyncio.sleep(5)
                continue
            self.logger.info("Processing broadcast queue")
            queued_broadcasts = await self.broadcast_queue_store.all(self.CONST_KEY)
            self.logger.info(f"Found {len(queued_broadcasts)} queued broadcasts")
            dequeued_broadcasts: list[QueuedBroadcast] = []
            for qb in queued_broadcasts:
                if qb.start_time > time.time():
                    continue
                if (await self.current_broadcast_by_topic_store.load(qb.topic)) is None:
                    self.logger.info(
                        f"Starting broadcast on topic {qb.topic} "
                        + f"scheduled {time.time() - qb.start_time:3f} sec ago "
                        + f"with sender {qb.sender}"
                    )
                    await self.current_broadcast_by_topic_store.save(qb.topic, qb)
                    await self.current_pending_subscribers_by_topic_store.add_multiple(
                        qb.topic, await self.topic_subscribers(qb.topic)
                    )
                    dequeued_broadcasts.append(qb)
                    if on_broadcast_start is not None:
                        try:
                            await on_broadcast_start(qb)
                        except Exception:
                            self.logger.exception("Unexpected error in on_broadcast_start callback, ignoring")
                    self.is_broadcasting = True
                else:
                    self.logger.info(
                        f"Overdue broadcast on topic {qb.topic} "
                        + f"scheduled {time.time() - qb.start_time:3f} sec ago "
                        + f"with sender {qb.sender}, waiting for previous broadcast on this topic to finish"
                    )
            if dequeued_broadcasts:
                self.logger.info(
                    f"{len(dequeued_broadcasts)} broadcasts popped from the queue and started broadcasting"
                )
                for b in dequeued_broadcasts:
                    await self.broadcast_queue_store.remove(self.CONST_KEY, b)
                queued_broadcasts = await self.broadcast_queue_store.all(self.CONST_KEY)
            self.next_broadcast_queue_processing_time = (
                min([qb.start_time for qb in queued_broadcasts]) if queued_broadcasts else time.time() + 300
            )
            self.logger.info(
                "The next broadcast queue processing scheduled "
                + f"in {self.next_broadcast_queue_processing_time - time.time():.2f} sec"
            )

    @prevent_shutdown_on_broadcasting
    @restart_on_errors
    async def _send_current_broadcasts(self, bot: AsyncTeleBot, on_broadcast_end: Optional[BroadcastCallback] = None):
        BATCH_SIZE = 200  # each batch should take around 10-20 sec to complete
        MESSAGES_PER_SECOND_LIMIT = 20  # telegram rate limit is around 30 msg/sec, but we play safe

        self.is_broadcasting = bool(await self.currently_broadcasting_topics())
        while True:
            async with prevent_shutdown_on_broadcasting.allow_shutdown():
                await asyncio.sleep(0.1)
            if not self.is_broadcasting:
                continue

            self.logger.info("Sending messages to subscribers")
            topics_to_send = await self.currently_broadcasting_topics()
            # sorting in decreasing priority order (top priority = first)
            topics_to_send.sort(key=self.topic_priority_key, reverse=True)
            self.logger.info(f"Current topic priority: {topics_to_send}")

            self.logger.info(f"Loading subscriber batch to send (target size {BATCH_SIZE})")
            batch: list[tuple[QueuedBroadcast, Subscriber]] = []
            for topic in topics_to_send:
                batch_from_topic = BATCH_SIZE - len(batch)
                if batch_from_topic <= 0:
                    break
                subscribers = await self.current_pending_subscribers_by_topic_store.pop_multiple(
                    topic, count=batch_from_topic
                )
                broadcast = await self.current_broadcast_by_topic_store.load(topic)
                if not subscribers or broadcast is None:
                    await self.current_broadcast_by_topic_store.drop(topic)
                    await self.current_pending_subscribers_by_topic_store.drop(topic)
                    if broadcast is not None:
                        self.logger.info(f"Broadcast completed: {broadcast}")
                        if on_broadcast_end is not None:
                            try:
                                await on_broadcast_end(broadcast)
                            except Exception:
                                self.logger.exception("Unexpected error in on_broadcast_end callback, ignoring")
                    continue
                self.logger.info(f"{len(subscribers)} subscribers from topic {topic} with sender {broadcast}")
                batch.extend([(broadcast, subscriber) for subscriber in subscribers])

            if not batch:
                self.logger.info("No subscribers to send to, seems like the broadcast is done!")
                self.is_broadcasting = False
                continue

            self.logger.info(f"Sending a batch of {len(batch)} messages...")
            start_time = time.time()

            async def _send_broadcasted_message(
                broadcast: QueuedBroadcast, subscriber: Subscriber, delay: float
            ) -> bool:
                await asyncio.sleep(delay)
                for _ in range(100):  # retry loop
                    try:
                        await broadcast.sender.send(MessageSenderContext(bot, subscriber))
                        return True
                    except telegram_api.ApiHTTPException as exc:
                        if exc.response.status == 429:
                            self.logger.info(f"Rate limiting error received from Telegram: {exc!r}")
                            await asyncio.sleep(1)
                        else:
                            self.logger.info(f"HTTP error received from Telegram: {exc!r}")
                            return False
                    except Exception:
                        self.logger.exception(f"Unexpected error sending message to {subscriber = }")
                        return False
                self.logger.error("All retry attempts exhausted :(")
                return False

            coroutines = [
                _send_broadcasted_message(broadcast, subscriber, delay=float(message_idx) / MESSAGES_PER_SECOND_LIMIT)
                for message_idx, (broadcast, subscriber) in enumerate(batch)
            ]
            success_flags = await asyncio.gather(*coroutines)

            self.logger.info(
                f"Batch sent: {sum(success_flags)} / {len(batch)} messages are successful; "
                + f"took {time.time() - start_time:.3f} sec"
            )
