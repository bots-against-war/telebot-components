import asyncio
import json
import logging
import random
import time
from functools import partial
from typing import Awaitable, Callable, Optional, TypedDict, TypeVar

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot import api as telegram_api

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import KeyDictStore, KeySetStore, KeyValueStore
from telebot_components.subscription.message_senders import AbstractMessageSender, MessageSenderContext

from telebot_components.utils import restart_on_errors


logger = logging.getLogger(__name__)


class Subscriber(TypedDict):
    user_id: int
    username: Optional[str]
    full_name: str
    subscribed_at: float


TopicActionResultT = TypeVar("TopicActionResultT")


class SubscriptionHandler:
    def __init__(
        self,
        redis: RedisInterface,
        bot_prefix: str,
        topic_priority_key: Callable[[str], float] = lambda _: random.random(),
    ):
        self.subscribers_by_topic_store = KeyDictStore[Subscriber](
            name="subscribers-by-topic",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=None,
        )
        self.topic_priority_key = topic_priority_key
        self.is_currently_sending = False

        self.current_message_sender_by_topic = KeyValueStore[AbstractMessageSender](
            name="current-message-sender-by-topic",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=None,
            dumper=lambda ams: json.dumps(ams.dump()),
            loader=lambda ams_dump: AbstractMessageSender.load(json.loads(ams_dump)),
        )
        self.current_pending_subscribers_by_topic = KeySetStore[Subscriber](
            name="current-pending-subscribers-by-topic",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=None,
        )

    async def current_topics(self) -> list[str]:
        return await self.subscribers_by_topic_store.list_keys()

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
        for topic in await self.current_topics():
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

    @restart_on_errors
    async def send_messages_to_subscribers(self, bot: AsyncTeleBot):
        BATCH_SIZE = 200  # should take around 10 seconds to complete
        self.is_currently_sending = bool(await self.current_message_sender_by_topic.list_keys())
        while True:
            await asyncio.sleep(1)
            if not self.is_currently_sending:
                continue

            logger.info("Sending messages to subscribers")
            topics_to_send = await self.current_message_sender_by_topic.list_keys()
            # sorting in decreasing priority order (top priority = first)
            topics_to_send.sort(key=self.topic_priority_key, reverse=True)
            logger.info(f"Current topic priority: {topics_to_send}")

            logger.info(f"Loading subscriber batch to send (target size {BATCH_SIZE})")
            batch: list[tuple[AbstractMessageSender, Subscriber]] = []
            for topic in topics_to_send:
                batch_from_topic = BATCH_SIZE - len(batch)
                if batch_from_topic <= 0:
                    break
                subscribers = await self.current_pending_subscribers_by_topic.pop_multiple(
                    topic, count=batch_from_topic
                )
                message_sender = await self.current_message_sender_by_topic.load(topic)
                if not subscribers or message_sender is None:
                    await self.current_message_sender_by_topic.drop(topic)
                    await self.current_pending_subscribers_by_topic.drop(topic)
                    continue
                logger.info(f"{len(subscribers)} subscribers from topic {topic} with sender {message_sender}")
                batch.extend([(message_sender, subscriber) for subscriber in subscribers])

            if not batch:
                logger.info("No subscribers to sent to, seems like we've finished sending messages, standing by")
                self.is_currently_sending = False
                continue

            logger.info(f"Sending batch of {len(batch)} messages...")
            success_count = 0
            request_times: list[float] = []
            for message_sender, subscriber in batch:
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
                        await message_sender.send(MessageSenderContext(bot, subscriber["user_id"]))
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
                + f"took {max(request_times) - min(request_times):.3f} sec"
            )
