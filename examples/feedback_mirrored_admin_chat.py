import logging

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner
from telebot.types import constants as tg_constants

from telebot_components.constants import times
from telebot_components.feedback import FeedbackConfig, FeedbackHandler, ServiceMessages
from telebot_components.feedback.anti_spam import DisabledAntiSpam
from telebot_components.feedback.integration.aux_feedback_handler import AuxFeedbackHandlerIntegration
from telebot_components.redis_utils.interface import RedisInterface


async def create_feedback_bot(redis: RedisInterface, token: str, main_admin_chat_id: int, aux_admin_chat_id: int):
    bot_prefix = "example-feedback-mirrored-admin-chat-bot"
    bot = AsyncTeleBot(token)

    logging.basicConfig(level=logging.DEBUG)
    # TEMP
    logging.getLogger("telebot[example-feedback-mirrored-admin-chat-bot]").setLevel(logging.INFO)
    logging.getLogger("telebot.api").setLevel(logging.INFO)

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
                unanswered_hashtag="неотвечено",
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

    main_feedback_handler = feedback_handler_factory("main", main_admin_chat_id)
    aux_feedback_handler = feedback_handler_factory("aux", aux_admin_chat_id)
    main_feedback_handler.integrations.append(
        AuxFeedbackHandlerIntegration(
            feedback_handler=aux_feedback_handler,
            bot_prefix=bot_prefix,
            redis=redis,
        )
    )

    await main_feedback_handler.setup(bot)

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
            aux_admin_chat_id=int(os.environ["AUX_ADMIN_CHAT_ID"]),
        )
        await bot_runner.run_polling()

    asyncio.run(main())
