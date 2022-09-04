import asyncio
from dataclasses import dataclass
import json
import logging
import random
import time
from functools import partial
from typing import Any, Awaitable, Callable, Optional, TypedDict, TypeVar

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot import api as telegram_api

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import KeyDictStore, KeyListStore, KeySetStore, KeyValueStore
from telebot_components.broadcast.message_senders import AbstractMessageSender, MessageSenderContext, TextSender
from telebot_components.broadcast.subscriber import Subscriber

from telebot_components.utils import restart_on_errors


logger = logging.getLogger(__name__)


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

    async def topics(self) -> list[str]:
        """At least one person should be subscribed to the topic"""
        return await self.subscribers_by_topic_store.list_keys()

    async def currently_active_topics(self) -> list[str]:
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
        user_ids = await self.subscribers_by_topic_store.list_subkeys(topic)
        maybe_subscribers = [await self.subscribers_by_topic_store.get_subkey(topic, user_id) for user_id in user_ids]
        return [s for s in maybe_subscribers if s is not None]

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
        self, bot: AsyncTeleBot, on_broadcast_start: Optional[BroadcastCallback] = None
    ):
        pass

    @restart_on_errors
    async def _process_broadcasts_queue(
        self, on_broadcast_start: Optional[BroadcastCallback] = None
    ):
        while True:
            if time.time() < self.next_broadcast_queue_processing_time:
                await asyncio.sleep(5)
                continue
            logger.info("Processing broadcast queue")
            queued_broadcasts = await self.broadcast_queue_store.all(self.CONST_KEY)
            dequeued_broadcasts: list[QueuedBroadcast] = []
            for qb in queued_broadcasts:
                if qb.start_time > time.time():
                    continue
                if (await self.current_broadcast_by_topic_store.load(qb.topic)) is None:
                    logger.info(
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
                        await on_broadcast_start(qb)
                else:
                    logger.info(
                        f"Overdue broadcast on topic {qb.topic} "
                        + f"scheduled {time.time() - qb.start_time:3f} sec ago "
                        + f"with sender {qb.sender}, waiting for previous broadcast on this topic to finish"
                    )
            if dequeued_broadcasts:
                logger.info(f"{len(dequeued_broadcasts)} broadcasts popped from the queue and started broadcasting")
                for b in dequeued_broadcasts:
                    await self.broadcast_queue_store.remove(self.CONST_KEY, b)
                queued_broadcasts = await self.broadcast_queue_store.all(self.CONST_KEY)
            self.next_broadcast_queue_processing_time = min([qb.start_time for qb in queued_broadcasts])
            logger.info(
                f"The next broadcast queue processing will happen in {self.next_broadcast_queue_processing_time - time.time():.2f} sec"
            )

    @restart_on_errors
    async def _broadcast(self, bot: AsyncTeleBot, on_broadcast_end: Optional[BroadcastCallback] = None):
        BATCH_SIZE = 200  # each batch should take around 10-20 sec to complete
        self.is_broadcasting = bool(await self.currently_active_topics())
        while True:
            await asyncio.sleep(1)
            if not self.is_broadcasting:
                continue

            logger.info("Sending messages to subscribers")
            topics_to_send = await self.currently_active_topics()
            # sorting in decreasing priority order (top priority = first)
            topics_to_send.sort(key=self.topic_priority_key, reverse=True)
            logger.info(f"Current topic priority: {topics_to_send}")

            logger.info(f"Loading subscriber batch to send (target size {BATCH_SIZE})")
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
                        logger.info(f"Broadcast completed: {broadcast}")
                        if on_broadcast_end is not None:
                            await on_broadcast_end(broadcast)
                    continue
                logger.info(f"{len(subscribers)} subscribers from topic {topic} with sender {broadcast}")
                batch.extend([(broadcast, subscriber) for subscriber in subscribers])

            if not batch:
                logger.info("No subscribers to sent to, seems like we've finished sending messages, standing by")
                self.is_broadcasting = False
                continue

            logger.info(f"Sending batch of {len(batch)} messages...")
            success_count = 0
            request_times: list[float] = []
            for broadcast, subscriber in batch:
                # respecting Telegram rate limit, see https://core.telegram.org/bots/faq#broadcasting-to-users
                last_second_request_times = [t for t in request_times if t > time.time() - 1]
                if len(last_second_request_times) >= 20:
                    # if sending request right now would violate rate limit, we sleep for some time
                    # so that the 1 second time window moves forward enough to include less than 20 requests
                    last_20_request_times = sorted(last_second_request_times)[-20:]
                    first_request_from_last_20_time = last_20_request_times[0]
                    await asyncio.sleep(first_request_from_last_20_time - (time.time() - 1))

                while True:  # retry loop
                    try:
                        request_times.append(time.time())
                        await broadcast.sender.send(MessageSenderContext(bot, subscriber))
                        success_count += 1
                    except telegram_api.ApiHTTPException as exc:
                        if exc.response.status == 429:
                            logger.exception(f"Rate limiting error received from Telegram: {exc!r}")
                            await asyncio.sleep(1)
                            continue
                        else:
                            logger.info(f"HTTP error received from Telegram: {exc!r}")
                    except Exception:
                        logger.exception(f"Unexpected error sending message to {subscriber = }")
                    break  # exiting retry loop

            logger.info(
                f"Batch sent: {success_count} / {len(batch)} messages are successful; "
                + f"took around {max(request_times) - min(request_times):.3f} sec"
            )