import logging

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner
from telebot.types import constants as tg_constants

from telebot_components.broadcast import BroadcastHandler, QueuedBroadcast
from telebot_components.broadcast.message_senders import MessageCopySender
from telebot_components.broadcast.subscriber import Subscriber
from telebot_components.redis_utils.interface import RedisInterface


def create_broadcsting_bot(redis: RedisInterface, token: str, admin_chat_id: int):
    bot_prefix = "example-broadcasting-bot"
    bot = AsyncTeleBot(token)

    broadcast_handler = BroadcastHandler[None](
        redis,
        bot_prefix,
        deletable_broadcasts=True,
    )

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

    @bot.message_handler(commands=["unbroadcast"], chat_id=[admin_chat_id])
    async def unbroadcast_cmd(message: tg.Message):
        topic = message.text_content.removeprefix("/unbroadcast").strip()
        if not topic:
            await bot.reply_to(message, "Please specify topic to unbroadcast the last message from")
            return
        await broadcast_handler.delete_last_broadcast(topic)

    async def on_broadcast_start(queued_broadcast: QueuedBroadcast, subs: list[Subscriber]):
        await bot.send_message(admin_chat_id, f"Starting: {queued_broadcast} to {len(subs)} subs")

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

    async def main() -> None:
        load_dotenv()
        logging.basicConfig(level=logging.INFO)

        redis_url = os.environ.get("REDIS_URL")
        redis: RedisInterface
        if redis_url is None:
            redis = RedisEmulation()
        else:
            redis = Redis.from_url(redis_url)  # type: ignore

        admin_chat_id = int(os.environ["ADMIN_CHAT_ID"])
        bot_runner = create_broadcsting_bot(
            redis=redis,
            token=os.environ["TOKEN"],
            admin_chat_id=admin_chat_id,
        )
        print(await bot_runner.bot.get_me())
        print(await bot_runner.bot.get_chat(admin_chat_id))
        await bot_runner.run_polling()

    asyncio.run(main())
