import logging

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner
from telebot.types import constants as tg_constants

from telebot_components.constants import times
from telebot_components.feedback import FeedbackConfig, FeedbackHandler, ServiceMessages
from telebot_components.feedback.anti_spam import DisabledAntiSpam
from telebot_components.feedback.integration.aux_feedback_handler import (
    AuxFeedbackHandlerIntegration,
)
from telebot_components.redis_utils.interface import RedisInterface


async def create_feedback_bot(
    redis: RedisInterface, token: str, main_admin_chat_id: int, aux_admin_chat_ids: list[int]
):
    bot_prefix = "example-feedback-mirrored-admin-chat-bot"
    bot = AsyncTeleBot(token)

    logging.basicConfig(level=logging.INFO)

    @bot.message_handler(commands=["start", "help"], chat_types=[tg_constants.ChatType.private])
    async def start_cmd_handler(message: tg.Message):
        await bot.send_message(message.from_user.id, "hi")

    def feedback_handler_factory(name: str, admin_chat_id: int) -> FeedbackHandler:
        return FeedbackHandler(
            admin_chat_id,
            redis,
            bot_prefix,
            config=FeedbackConfig(
                message_log_to_admin_chat=True,
                force_category_selection=False,
                hashtags_in_admin_chat=True,
                hashtag_message_rarer_than=times.FIVE_MINUTES,
                unanswered_hashtag="unanswered",
                confirm_forwarded_to_admin_rarer_than=times.FIVE_MINUTES,
                full_user_anonymization=False,
            ),
            anti_spam=DisabledAntiSpam(),
            service_messages=ServiceMessages(
                forwarded_to_admin_ok="fwd ok",
                throttling_template="no more",
                copied_to_user_ok="copied ok",
                can_not_delete_message="can't delete :(",
                deleted_message_ok="deleted ok",
            ),
            name=name,
        )

    main_feedback_handler = feedback_handler_factory("", main_admin_chat_id)
    main_feedback_handler.integrations.extend(
        [
            AuxFeedbackHandlerIntegration(
                feedback_handler=feedback_handler_factory(f"aux admin chat #{idx + 1}", aux_admin_chat_id),
                bot_prefix=bot_prefix,
                redis=redis,
            )
            for idx, aux_admin_chat_id in enumerate(aux_admin_chat_ids)
        ]
    )

    await main_feedback_handler.setup(bot)

    @bot.message_handler(commands=["test"])
    async def test_cmd_handler(message: tg.Message) -> None:
        await main_feedback_handler.emulate_user_message(
            bot=bot,
            user=message.from_user,
            text="user just entered /test",
        )

    return BotRunner(
        bot_prefix=bot_prefix,
        bot=bot,
        background_jobs=main_feedback_handler.background_jobs(base_url=None, server_listening_future=None),
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
            main_admin_chat_id=int(os.environ["MAIN_ADMIN_CHAT_ID"]),
            aux_admin_chat_ids=[int(aaci) for aaci in os.environ["AUX_ADMIN_CHAT_IDS"].split(",")],
        )
        await bot_runner.run_polling()

    asyncio.run(main())
