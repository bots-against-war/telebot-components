import logging

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner
from telebot.types import constants as tg_constants

from telebot_components.constants import times
from telebot_components.feedback import (
    FeedbackConfig,
    FeedbackHandler,
    ServiceMessages,
    UserAnonymization,
)
from telebot_components.feedback.anti_spam import AntiSpam, AntiSpamConfig
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.banned_users import BannedUsersStore
from telebot_components.stores.category import Category, CategoryStore
from telebot_components.stores.forum_topics import (
    CategoryForumTopicStore,
    ForumTopicSpec,
    ForumTopicStore,
    ForumTopicStoreErrorMessages,
)


async def create_feedback_bot(redis: RedisInterface, token: str, admin_chat_id: int):
    bot_prefix = "feedback-bot-with-forum-topic-per-category"
    bot = AsyncTeleBot(token)

    logging.basicConfig(level=logging.INFO)

    kiki_category = Category(name="kiki", hashtag="кики")
    bouba_category = Category(name="bouba", hashtag="буба")
    category_store = CategoryStore(
        bot_prefix,
        redis,
        categories=[kiki_category, bouba_category],
        category_expiration_time=times.FIVE_MINUTES,
    )

    kiki_topic = ForumTopicSpec.from_category(kiki_category)
    bouba_topic = ForumTopicSpec.from_category(bouba_category)
    forum_topic_store = CategoryForumTopicStore(
        forum_topic_store=ForumTopicStore(
            redis=redis,
            bot_prefix=bot_prefix,
            admin_chat_id=admin_chat_id,
            topics=[kiki_topic, bouba_topic],
            error_messages=ForumTopicStoreErrorMessages(
                admin_chat_is_not_forum_error="not a forum! will check again in {} sec",
                cant_create_topic="error creating topic {}: {}; will try again in {} sec",
            ),
            initialization_retry_interval_sec=60,
        ),
        forum_topic_by_category={
            kiki_category: kiki_topic,
            bouba_category: bouba_topic,
        },
    )

    banned_store = BannedUsersStore(redis, bot_prefix, cached=False)

    async def welcome(user: tg.User):
        await bot.send_message(user.id, "hey", reply_markup=(await category_store.markup_for_user(user)))

    @bot.message_handler(
        commands=["start", "help"], chat_types=[tg_constants.ChatType.private], func=banned_store.not_from_banned_user
    )
    async def start_cmd_handler(message: tg.Message):
        await welcome(message.from_user)

    feedback_handler = FeedbackHandler(
        admin_chat_id=admin_chat_id,
        redis=redis,
        bot_prefix=bot_prefix,
        config=FeedbackConfig(
            message_log_to_admin_chat=True,
            force_category_selection=True,
            hashtags_in_admin_chat=True,
            hashtag_message_rarer_than=times.FIVE_MINUTES,
            unanswered_hashtag="неотвечено",
            confirm_forwarded_to_admin_rarer_than=times.FIVE_MINUTES,
            user_anonymization=UserAnonymization.FULL,
        ),
        anti_spam=AntiSpam(
            redis,
            bot_prefix,
            config=AntiSpamConfig(
                throttle_after_messages=3,
                throttle_duration=times.FIVE_MINUTES,
                soft_ban_after_throttle_violations=5,
                soft_ban_duration=times.HOUR,
            ),
        ),
        service_messages=ServiceMessages(
            forwarded_to_admin_ok="forwarded!",
            you_must_select_category="select category first!",
            throttling_template="please send no more than {} messages in {}.",
            copied_to_user_ok="Скопировано в чат с пользователь_ницей ✨",
            can_not_delete_message="Невозможно удалить сообщение.",
            deleted_message_ok="Сообщение успешно удалено!",
        ),
        banned_users_store=banned_store,
        category_store=category_store,
        forum_topic_store=forum_topic_store,
    )

    async def on_category_selected(bot: AsyncTeleBot, menu_message: tg.Message, user: tg.User, new_option: Category):
        await bot.send_message(user.id, f"category selected: {new_option}")
        await feedback_handler.emulate_user_message(bot, user, f"just selected a category: {new_option}")

    category_store.setup(bot, on_category_selected=on_category_selected)
    await feedback_handler.setup(bot)

    return BotRunner(
        bot_prefix=bot_prefix,
        bot=bot,
        background_jobs=feedback_handler.background_jobs(base_url=None, server_listening_future=None),
    )


if __name__ == "__main__":
    import asyncio
    import os

    from dotenv import load_dotenv

    from telebot_components.redis_utils.emulation import RedisEmulation

    load_dotenv()

    async def main() -> None:
        redis = RedisEmulation()
        bot_runner = await create_feedback_bot(
            redis=redis,
            token=os.environ["TOKEN"],
            admin_chat_id=int(os.environ["ADMIN_CHAT_ID"]),
        )
        await bot_runner.run_polling()

    asyncio.run(main())
