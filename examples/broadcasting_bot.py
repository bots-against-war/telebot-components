import logging

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner
from telebot.types import constants as tg_constants

from telebot_components.broadcast import BroadcastHandler, QueuedBroadcast
from telebot_components.broadcast.message_senders import MessageCopySender
from telebot_components.constants import times
from telebot_components.redis_utils.interface import RedisInterface


def create_broadcsting_bot(redis: RedisInterface, token: str, admin_chat_id: int):
    bot_prefix = "example-broadcasting-bot"
    bot = AsyncTeleBot(token)

    broadcast_handler = BroadcastHandler(redis, bot_prefix)

    logging.basicConfig(level=logging.DEBUG)

    @bot.message_handler(commands=["topics"])
    async def list_topics_cmd_handler(message: tg.Message):
        topics = await broadcast_handler.topics()
        await bot.reply_to(message, "\n".join(topics) if topics else "No topics yet")

    @bot.message_handler(commands=["subscribe"], chat_types=[tg_constants.ChatType.private])
    async def subscribe_cmd_handler(message: tg.Message):
        topic = message.text_content.removeprefix("/subscribe").strip()
        if not topic:
            await broadcast_handler.subscribe_to_all_topics(message.from_user)
            await bot.reply_to(message, "You have subscribed to all topics")
        else:
            await broadcast_handler.subscribe_to_topic(topic, message.from_user)
            await bot.reply_to(message, f"You have subscribed to {topic!r} topic")

    @bot.message_handler(commands=["unsubscribe"], chat_types=[tg_constants.ChatType.private])
    async def unsubscribe_cmd_handler(message: tg.Message):
        topic = message.text_content.removeprefix("/unsubscribe").strip()
        if not topic:
            await broadcast_handler.unsubscribe_from_all_topics(message.from_user)
            await bot.reply_to(message, "You have unsubscribed from all topics")
        else:
            await broadcast_handler.unsubscribe_from_topic(topic, message.from_user)
            await bot.reply_to(message, f"You have unsubscribed from {topic!r} topic")

    @bot.message_handler(commands=["broadcast"], chat_id=[admin_chat_id])
    async def broadcast_cmd(message: tg.Message):
        if message.reply_to_message is None:
            await bot.reply_to(message, "Command must be sent in reply to the message you want broadcasted")
            return
        topic = message.text_content.removeprefix("/broadcast").strip()
        if not topic:
            await bot.reply_to(message, "Please specify topic to broadcast the message")
            return
        await broadcast_handler.new_broadcast(topic, sender=MessageCopySender.from_message(message.reply_to_message))

    async def on_broadcast_start(queued_broadcast: QueuedBroadcast):
        await bot.send_message(admin_chat_id, f"Starting: {queued_broadcast}")

    async def on_broadcast_end(queued_broadcast: QueuedBroadcast):
        await bot.send_message(admin_chat_id, f"Completed: {queued_broadcast}")

    return BotRunner(
        bot_prefix=bot_prefix,
        bot=bot,
        background_jobs=[broadcast_handler.background_job(bot, on_broadcast_start, on_broadcast_end)],
    )


if __name__ == "__main__":
    import asyncio
    import os

    from dotenv import load_dotenv
    from redis.asyncio import Redis  # type: ignore

    from telebot_components.redis_utils.emulation import RedisEmulation
    from telebot_components.redis_utils.interface import RedisInterface

    load_dotenv()

    redis_url = os.environ.get("REDIS_URL")
    redis: RedisInterface
    if redis_url is None:
        redis = RedisEmulation()
    else:
        redis = Redis.from_url(os.environ["REDIS_URL"])
    bot_runner = create_broadcsting_bot(
        redis=redis,
        token=os.environ["TOKEN"],
        admin_chat_id=int(os.environ["ADMIN_CHAT_ID"]),
    )

    asyncio.run(bot_runner.run_polling())
