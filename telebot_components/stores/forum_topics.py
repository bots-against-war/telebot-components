import asyncio
import itertools
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from async_lru import alru_cache
from telebot import AsyncTeleBot
from telebot.api import ApiHTTPException

from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.category import Category
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
    # this id is used to migrate legacy code or for advanced topic naming scenarios
    # (e.g. if topic must be renameable)
    _id: Optional[str] = None

    @property
    def id(self) -> str:
        return self._id or self.name

    @classmethod
    def from_category(cls, category: Category) -> "ForumTopicSpec":
        return ForumTopicSpec(name=category.name)


class ForumTopicStore:
    def __init__(
        self,
        redis: RedisInterface,
        bot_prefix: str,
        admin_chat_id: int,
        topics: list[ForumTopicSpec],
        error_messages: ForumTopicStoreErrorMessages,
        initialization_retry_interval_sec: Optional[float] = None,
    ) -> None:
        self.topics = topics
        self.topic_by_id = {t.id: t for t in topics}
        self.admin_chat_id = admin_chat_id
        self.error_messages = error_messages
        self.bot: Optional[AsyncTeleBot] = None
        self.is_initialization_in_progress = False
        self.is_initialized = False
        self.initialization_retry_interval_sec = initialization_retry_interval_sec
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

    async def setup(self, bot: AsyncTeleBot) -> None:
        self.bot = bot

    async def background_job(self) -> None:
        if self.bot is None:
            self.logger.error("Unable to initialize: bot has not been set up")
            return
        if self.is_initialized:
            self.logger.warning("already initialized")
            return
        if self.is_initialization_in_progress:
            self.logger.warning("another initialization is already in progress")
            return
        self.is_initialization_in_progress = True
        self.logger.info(f"Setting up forum topic store on admin chat id: {self.admin_chat_id}")
        while True:
            admin_chat = await self.bot.get_chat(self.admin_chat_id)
            if admin_chat.is_forum:
                self.logger.info("Admin chat is forum, continue initialization")
                break
            self.logger.info("Failed to setup form topic store, admin chat is not a forum")
            if self.initialization_retry_interval_sec is None:
                self.logger.info("Aborting")
                return
            await self.bot.send_message(
                chat_id=self.admin_chat_id,
                text=self.error_messages.admin_chat_is_not_forum_error.format(self.initialization_retry_interval_sec),
            )
            self.logger.info(f"Will try again in {self.initialization_retry_interval_sec} sec")
            await asyncio.sleep(self.initialization_retry_interval_sec)

        while True:
            try:
                for topic_spec, default_color in zip(self.topics, itertools.cycle(ForumTopicIconColor)):
                    existing_message_thread_id = await self.message_thread_id_by_topic.get_subkey(
                        self.admin_chat_id, topic_spec.id
                    )
                    if existing_message_thread_id is not None:
                        self.logger.info(
                            f"Found saved message thread id for {topic_spec}: "
                            + f"{existing_message_thread_id}, trying to sync state"
                        )
                        success = False
                        try:
                            success = await self.bot.edit_forum_topic(
                                chat_id=self.admin_chat_id,
                                message_thread_id=existing_message_thread_id,
                                name=topic_spec.name,
                                icon_custom_emoji_id=topic_spec.icon_custom_emoji_id,
                            )
                        except ApiHTTPException as e:
                            if e.error_description is not None and "TOPIC_NOT_MODIFIED" in e.error_description:
                                success = True
                            else:
                                self.logger.exception("Unexpected error syncing topic")

                        if success:
                            self.logger.info(f"Forum topic OK: {topic_spec}")
                            await asyncio.sleep(5)
                            continue
                        else:
                            self.logger.info(f"Failed to sync {topic_spec}, will create new one")

                    self.logger.info(f"Creating new forum topic for {topic_spec}")
                    created_topic = await self.bot.create_forum_topic(
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
                self.logger.exception("Unexpected error creating forum topics")
                if self.initialization_retry_interval_sec is None:
                    self.logger.info("Aborting")
                    return
                await self.bot.send_message(
                    self.admin_chat_id,
                    text=self.error_messages.cant_create_topic.format(
                        topic_spec.name, exc, self.initialization_retry_interval_sec
                    ),
                )
                self.logger.info(f"Will retry in {self.initialization_retry_interval_sec} sec")
                await asyncio.sleep(self.initialization_retry_interval_sec)

        self.is_initialized = True
        self.logger.info("Forum topics store set up")


@dataclass
class CategoryForumTopicStore:
    """Helper class to use category and forum topic stores together (e.g. in FeedbackHandler)"""

    forum_topic_store: ForumTopicStore
    forum_topic_by_category: dict[Optional[Category], ForumTopicSpec]  # None = forum topic for no category

    def __post_init__(self) -> None:
        # validating all forum topics are known
        for mapped_topic_spec in self.forum_topic_by_category.values():
            if mapped_topic_spec not in self.forum_topic_store.topics:
                raise ValueError(
                    "category -> forum topic mapping must include only topics added to the store, "
                    + f"but {mapped_topic_spec} is not"
                )

    def __hash__(self) -> int:
        return id(self)  # hash is required for alru cache below

    @alru_cache(maxsize=1_000)
    async def get_message_thread_id(self, category: Optional[Category]) -> Optional[int]:
        forum_topic = self.forum_topic_by_category.get(category)
        if forum_topic is None:
            return None
        else:
            return await self.forum_topic_store.get_message_thread_id(forum_topic.id)

    async def setup(self, bot: AsyncTeleBot) -> None:
        await self.forum_topic_store.setup(bot)

    async def background_job(self) -> None:
        await self.forum_topic_store.background_job()
