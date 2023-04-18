import asyncio
import itertools
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from telebot import AsyncTeleBot

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import KeyDictStore


@dataclass
class ForumTopicStoreErrorMessages:
    """All messages may include {} placeholders"""

    admin_chat_is_not_forum_error: str  # placeholder: retry interval in seconds
    cant_create_topic: str  # placeholders: topic name, error, retry interval


class ForumTopicIconColor(Enum):
    """See https://core.telegram.org/bots/api#createforumtopic"""

    BLUE = 7322096
    YELLLOW = 16766590
    VIOLET = 13338331
    LIME = 9367192
    PINK = 16749490
    RED = 16478047


@dataclass
class ForumTopicSpec:
    name: str
    icon_color: Optional[ForumTopicIconColor] = None
    icon_custom_emoji_id: Optional[str] = None

    # NOTE: this is not a message_thread_id!
    # this id is used to migrate legacy code or for complex topic naming scenarios (e.g. topic must be renameable)
    _id: Optional[str] = None

    @property
    def id(self) -> str:
        return self._id or self.name


class ForumTopicStore:
    def __init__(
        self,
        redis: RedisInterface,
        bot_prefix: str,
        admin_chat_id: int,
        topics: list[ForumTopicSpec],
        error_messages: ForumTopicStoreErrorMessages,
    ) -> None:
        self.topics = topics
        self.topic_by_id = {t.id: t for t in topics}
        self.admin_chat_id = admin_chat_id
        self.error_messages = error_messages
        self.is_setup_in_progress = False
        self.is_initialized = False
        self.message_thread_id_by_topic = KeyDictStore[int](
            name="forum-topic-id",  # NOTE: this is a bit of a legacy name
            prefix=bot_prefix,
            redis=redis,
            expiration_time=None,
            dumper=str,
            loader=int,
        )
        self.logger = logging.getLogger(f"{__name__}[{bot_prefix}]")

    async def get_message_thread_id(self, topic_id: str) -> Optional[int]:
        if topic_id not in self.topic_by_id:
            raise ValueError(f"Unknown topic_id, available ids are: {list(self.topic_by_id.keys())}")
        if not self.is_initialized:
            self.logger.warning("Message thread id requested from the store until it is initialized, returning None")
            return None
        return await self.message_thread_id_by_topic.get_subkey(self.admin_chat_id, topic_id)

    async def setup(self, bot: AsyncTeleBot, retry_interval_sec: Optional[float]) -> None:
        if self.is_initialized:
            self.logger.warning(f"setup method called on an already initialized store")
            return
        if self.is_setup_in_progress:
            self.logger.warning(f"setup method called, but another one is already in progress")
            return
        self.is_setup_in_progress = True
        self.logger.info(f"Setting up forum topic store on admin chat id: {self.admin_chat_id}")
        while True:
            admin_chat = await bot.get_chat(self.admin_chat_id)
            if admin_chat.is_forum:
                self.logger.info("Admin chat is forum, continue initialization")
                break
            self.logger.info("Failed to setup form topic store, admin chat is not a forum")
            if retry_interval_sec is None:
                self.logger.info("Aborting")
                return
            await bot.send_message(
                chat_id=self.admin_chat_id,
                text=self.error_messages.admin_chat_is_not_forum_error.format(retry_interval_sec),
            )
            self.logger.info(f"Will try again in {retry_interval_sec} sec")
            await asyncio.sleep(retry_interval_sec)

        while True:
            try:
                for (topic_spec, default_color) in zip(self.topics, itertools.cycle(ForumTopicIconColor)):
                    existing_message_thread_id = await self.message_thread_id_by_topic.get_subkey(
                        self.admin_chat_id, topic_spec.id
                    )
                    if existing_message_thread_id is not None:
                        self.logger.info(
                            f"Found saved message thread id for {topic_spec}, validating and syncing state"
                        )
                        success = await bot.edit_forum_topic(
                            chat_id=self.admin_chat_id,
                            message_thread_id=existing_message_thread_id,
                            name=topic_spec.name,
                            icon_custom_emoji_id=topic_spec.icon_custom_emoji_id,
                        )
                        if success:
                            self.logger.info(f"Forum topic updated for {topic_spec}")
                            continue
                        else:
                            self.logger.info(f"Forum topic not updated for {topic_spec}, will create new one")

                    self.logger.info(f"Creating new forum topic for {topic_spec}")
                    created_topic = await bot.create_forum_topic(
                        chat_id=self.admin_chat_id,
                        name=topic_spec.name,
                        icon_color=(topic_spec.icon_color or default_color).value,
                        icon_custom_emoji_id=topic_spec.icon_custom_emoji_id,
                    )
                    await self.message_thread_id_by_topic.set_subkey(
                        self.admin_chat_id,
                        topic_spec.id,
                        created_topic.message_thread_id,
                    )
                    self.logger.info(f"Created and saved message thread id for {topic_spec}")
                    await asyncio.sleep(5)  # backoff to avoid rate-limiting problems
                self.logger.info("All topics are created")
                break
            except Exception as exc:
                self.logger.exception(f"Unexpected error creating forum topics")
                if retry_interval_sec is None:
                    self.logger.info("Aborting")
                    return
                await bot.send_message(
                    self.admin_chat_id,
                    text=self.error_messages.cant_create_topic.format(topic_spec.name, exc, retry_interval_sec),
                )
                self.logger.info(f"Will retry in {retry_interval_sec} sec")
                await asyncio.sleep(retry_interval_sec)

        self.is_initialized = True
        self.logger.info("Forum topics store set up")
