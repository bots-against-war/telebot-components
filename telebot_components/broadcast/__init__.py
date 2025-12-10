import asyncio
import functools
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
from telebot_components.utils import AsyncFunctionT

prevent_shutdown_on_consuming_queue = PreventShutdown("consuming broadcast queue")
prevent_shutdown_on_broadcasting = PreventShutdown("broadcasting")


def log_fatal_error(function: AsyncFunctionT) -> AsyncFunctionT:
    @functools.wraps(function)
    async def decorated(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except Exception:
            logging.exception(f"Fatal error in {function.__qualname__}, exiting it")

    return decorated  # type: ignore


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
OnBroadcastEndCallback = Callable[[QueuedBroadcast], Awaitable[Any]]
OnBroadcastStartCallback = Callable[[QueuedBroadcast, list[Subscriber]], Awaitable[Any]]


@dataclass(frozen=True)
class MessageBroadcastingConfig:
    batch_size = 200  # each batch should take around 10-20 sec to complete
    messages_per_second_limit = 20  # telegram rate limit is around 30 msg/sec, but we play safe
    message_send_retries: int = 10

    constant_backoff_sec: float | None = 1.0
    random_exp_backoff_mult_base_sec: tuple[float, float] | None = None
    from_header_backoff: bool = True


class BroadcastHandler:
    def __init__(
        self,
        redis: RedisInterface,
        bot_prefix: str,
        topic_priority_key: Callable[[str], float] = lambda _: random.random(),
        broadcasting_config: MessageBroadcastingConfig = MessageBroadcastingConfig(),
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
        self._broadcasting_config = broadcasting_config

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
        new_qb = QueuedBroadcast(sender=sender, topic=topic, start_time=schedule_at or time.time())
        if await self.broadcast_queue_store.add(self.CONST_KEY, new_qb):
            self.next_broadcast_queue_processing_time = min(
                self.next_broadcast_queue_processing_time, new_qb.start_time
            )
            return True
        else:
            return False

    async def background_job(
        self,
        bot: AsyncTeleBot,
        on_broadcast_start: Optional[OnBroadcastStartCallback] = None,
        on_broadcast_end: Optional[OnBroadcastEndCallback] = None,
    ):
        await asyncio.gather(
            self._consume_broadcasts_queue(on_broadcast_start),
            self._send_current_broadcasts(bot, on_broadcast_end),
        )

    @prevent_shutdown_on_consuming_queue
    @log_fatal_error
    async def _consume_broadcasts_queue(self, on_broadcast_start: OnBroadcastStartCallback | None = None):
        while True:
            if time.time() < self.next_broadcast_queue_processing_time:
                async with prevent_shutdown_on_consuming_queue.allow_shutdown():
                    await asyncio.sleep(5)
                continue

            self.logger.info("Processing broadcast queue")
            broadcast_queue = await self.broadcast_queue_store.pop_multiple(
                self.CONST_KEY,
                count=50000,  # NOTE: works only if real queue is shorter than this
            )
            self.logger.info(f"Found a total of {len(broadcast_queue)} queued broadcasts")
            new_broadcast_queue: list[QueuedBroadcast] = []
            for qb in broadcast_queue:
                if (
                    qb.start_time > time.time()
                    or (await self.current_broadcast_by_topic_store.load(qb.topic)) is not None
                ):
                    self.logger.debug(
                        f"Not starting {qb}, either it's too early "
                        + "or waiting for previous broadcast on the same topic"
                    )
                    new_broadcast_queue.append(qb)
                    continue

                self.is_broadcasting = True
                subscribers = await self.topic_subscribers(qb.topic)
                self.logger.info(
                    f"Starting broadcast on topic {qb.topic} to {len(subscribers)} subscribers with sender {qb.sender}; "
                    + f"was scheduled {time.time() - qb.start_time:3f} sec ago"
                )
                await self.current_broadcast_by_topic_store.save(qb.topic, qb)
                await self.current_pending_subscribers_by_topic_store.add_multiple(qb.topic, subscribers)
                if on_broadcast_start is not None:
                    try:
                        await on_broadcast_start(qb, subscribers)
                    except Exception:
                        self.logger.exception("Unexpected error in on_broadcast_start callback, ignoring")

            self.logger.info(
                f"Of {len(broadcast_queue)} broadcast(s) found in queue, "
                f"{len(broadcast_queue) - len(new_broadcast_queue)} started broadcasting, "
                + f"{len(new_broadcast_queue)} is/are put back in the queue"
            )
            if new_broadcast_queue:
                await self.broadcast_queue_store.add_multiple(self.CONST_KEY, new_broadcast_queue)
                self.next_broadcast_queue_processing_time = min([qb.start_time for qb in broadcast_queue])
            else:
                self.next_broadcast_queue_processing_time = time.time() + 300

            self.logger.info(
                "The next broadcast queue processing scheduled "
                + f"in {self.next_broadcast_queue_processing_time - time.time():.2f} sec"
            )

    @prevent_shutdown_on_broadcasting
    @log_fatal_error
    async def _send_current_broadcasts(
        self, bot: AsyncTeleBot, on_broadcast_end: Optional[OnBroadcastEndCallback] = None
    ):
        self.is_broadcasting = bool(await self.currently_broadcasting_topics())
        while True:
            async with prevent_shutdown_on_broadcasting.allow_shutdown():
                await asyncio.sleep(0.5)
            if not self.is_broadcasting:
                continue

            topics_to_send = await self.currently_broadcasting_topics()
            # sorting in decreasing priority order (top priority = first)
            topics_to_send.sort(key=self.topic_priority_key, reverse=True)
            self.logger.info(f"Broadcasting messages to subscribers; topic priority: {topics_to_send}")

            self.logger.info(f"Loading subscriber batch to send (target size {self._broadcasting_config.batch_size})")
            batch: list[tuple[QueuedBroadcast, Subscriber]] = []
            for topic in topics_to_send:
                batch_from_topic = self._broadcasting_config.batch_size - len(batch)
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
                broadcast: QueuedBroadcast, subscriber: Subscriber, predelay: float
            ) -> bool:
                await asyncio.sleep(predelay)
                for i_retry in range(self._broadcasting_config.message_send_retries):
                    try:
                        res = await broadcast.sender.send(MessageSenderContext(bot, subscriber))
                        return res if res is not None else True
                    except telegram_api.ApiHTTPException as exc:
                        if exc.response.status == 429:
                            self.logger.info(f"Rate limiting error received from Telegram: {exc!r}")
                            if self._broadcasting_config.constant_backoff_sec is not None:
                                await asyncio.sleep(self._broadcasting_config.constant_backoff_sec)
                            if self._broadcasting_config.random_exp_backoff_mult_base_sec is not None:
                                mult, base = self._broadcasting_config.random_exp_backoff_mult_base_sec
                                await asyncio.sleep(random.random() * mult * base**i_retry)
                            if (
                                self._broadcasting_config.from_header_backoff
                                and exc.error_parameters is not None
                                and exc.error_parameters.retry_after is not None
                            ):
                                await asyncio.sleep(exc.error_parameters.retry_after)
                        else:
                            self.logger.info(f"HTTP error received from Telegram: {exc!r}")
                            return False
                    except Exception:
                        self.logger.exception(f"Unexpected error sending message to {subscriber = }")
                        return False
                self.logger.error("All retry attempts exhausted :(")
                return False

            coroutines = [
                _send_broadcasted_message(
                    broadcast,
                    subscriber,
                    predelay=float(message_idx) / self._broadcasting_config.messages_per_second_limit,
                )
                for message_idx, (broadcast, subscriber) in enumerate(batch)
            ]
            success_flags = await asyncio.gather(*coroutines)

            self.logger.info(
                f"Batch sent: {sum(success_flags)} / {len(batch)} messages are successful; "
                + f"took {time.time() - start_time:.3f} sec"
            )
