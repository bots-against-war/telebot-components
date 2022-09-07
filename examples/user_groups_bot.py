import logging

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner
from telebot_components.redis_utils.interface import RedisInterface

from telebot_components.stores.user_group import UserGroupStore


logging.basicConfig(level=logging.DEBUG)


def create_bot_with_user_groups(token: str, redis: RedisInterface):
    bot_prefix = "user-groups-example"
    bot = AsyncTeleBot(token)

    friends_group = UserGroupStore(redis, bot_prefix, "friends")
    enemies_group = UserGroupStore(redis, bot_prefix, "enemies")

    @bot.message_handler(commands=["kiss"])
    @friends_group.membership_required(bot)
    async def start_cmd_handler_for_friends(message: tg.Message):
        await bot.send_message(message.from_user.id, "<3")

    @bot.message_handler(commands=["fight"])
    @enemies_group.membership_required(bot)
    async def start_cmd_handler_for_enemies(message: tg.Message):
        await bot.send_message(message.from_user.id, "👊👊👊")

    @bot.message_handler(commands=["add"])
    async def add_user_identity_cmd_hanler(message: tg.Message):
        payload = message.text_content.removeprefix("/add").strip()
        if not payload:
            return
        group_name, identity = payload.split()
        if group_name == "friend":
            group = friends_group
        elif group_name == "enemy":
            group = enemies_group
        else:
            await bot.reply_to(message, "unknown group")
            return
        await group.add_identity(identity)
        await bot.reply_to(message, f"added {identity!r} to {group_name!r}")

    return BotRunner(
        bot_prefix=bot_prefix,
        bot=bot,
    )


if __name__ == "__main__":
    import asyncio
    import os

    from dotenv import load_dotenv

    from telebot_components.redis_utils.emulation import RedisEmulation

    load_dotenv()
    redis = RedisEmulation()
    bot_runner = create_bot_with_user_groups(token=os.environ["TOKEN"], redis=redis)
    asyncio.run(bot_runner.run_polling())
