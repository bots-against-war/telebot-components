import logging
from typing import Optional

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner
from telebot.util import extract_arguments

from telebot_components.redis_utils.emulation import RedisEmulation
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.forum_topics import (
    ForumTopicSpec,
    ForumTopicStore,
    ForumTopicStoreErrorMessages,
)


def create_forum_topics_bot(token: str, redis: RedisInterface, admin_chat_id: int):
    bot_prefix = "example-menu-bot"
    bot = AsyncTeleBot(token)
    logging.basicConfig(level=logging.INFO)

    cabbage_topic = ForumTopicSpec(name="cabbage")
    potato_topic = ForumTopicSpec(name="potato")
    forum_topics_store = ForumTopicStore(
        redis=redis,
        bot_prefix=bot_prefix,
        admin_chat_id=admin_chat_id,
        topics=[cabbage_topic, potato_topic],
        error_messages=ForumTopicStoreErrorMessages(
            "not a forum! will try again in {} sec",
            "can't setup topics! error during {}: {}; will try again in {} sec",
        ),
    )

    @bot.message_handler(commands=["start", "help"])
    async def start_cmd_handler(message: tg.Message):
        await bot.send_message(
            message.from_user.id, text="hi, use /cabbage to send message to cabbage topic, and /potato for potato"
        )

    @bot.message_handler(commands=["cabbage"])
    async def cabbage_handler(message: tg.Message):
        message_thread_id = await forum_topics_store.get_message_thread_id(cabbage_topic.id)
        command_args = extract_arguments(message.text_content)
        await bot.send_message(
            admin_chat_id,
            message_thread_id=message_thread_id,
            text=command_args or "<nothing>",
        )

    @bot.message_handler(commands=["potato"])
    async def potato_handler(message: tg.Message):
        message_thread_id = await forum_topics_store.get_message_thread_id(potato_topic.id)
        command_args = extract_arguments(message.text_content)
        await bot.send_message(
            admin_chat_id,
            message_thread_id=message_thread_id,
            text=command_args or "<nothing>",
        )

    return BotRunner(
        bot_prefix=bot_prefix,
        bot=bot,
        background_jobs=[forum_topics_store.setup(bot, retry_interval_sec=300)],
    )


if __name__ == "__main__":
    import asyncio
    import os

    from dotenv import load_dotenv

    load_dotenv()

    bot_runner = create_forum_topics_bot(
        token=os.environ["TOKEN"],
        redis=RedisEmulation(),
        admin_chat_id=int(os.environ["ADMIN_CHAT_ID"]),
    )

    asyncio.run(bot_runner.run_polling())
