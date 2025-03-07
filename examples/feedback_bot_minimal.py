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


async def create_feedback_bot(redis: RedisInterface, token: str, admin_chat_id: int):
    bot_prefix = "example-feedback-bot"
    bot = AsyncTeleBot(token)

    logging.basicConfig(level=logging.DEBUG)

    banned_store = BannedUsersStore(
        redis,
        bot_prefix,
        cached=False,
    )

    async def welcome(user: tg.User):
        await bot.send_message(user.id, "hello")

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
            force_category_selection=False,
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
                throttle_after_messages=10,
                throttle_duration=times.FIVE_MINUTES,
                soft_ban_after_throttle_violations=5,
                soft_ban_duration=times.HOUR,
            ),
        ),
        service_messages=ServiceMessages(
            forwarded_to_admin_reaction="👀",
            you_must_select_category=None,
            throttling_template="please don't send more than {} messages in {}.",
            copied_to_user_ok="sent to user",
            can_not_delete_message="can't delete message",
            deleted_message_ok="message deleted from chat with user",
        ),
        banned_users_store=banned_store,
    )
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
        print(await bot_runner.bot.get_me())
        await bot_runner.run_polling()

    asyncio.run(main())
