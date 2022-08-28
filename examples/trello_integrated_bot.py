import asyncio

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner
from telebot.types import constants as tg_constants

from telebot_components.constants import times
from telebot_components.feedback import FeedbackConfig, FeedbackHandler, ServiceMessages
from telebot_components.feedback.anti_spam import DisabledAntiSpam
from telebot_components.feedback.trello_integration import (
    TrelloIntegration,
    TrelloIntegrationCredentials,
    TrelloLabelColor,
    UnansweredLabelConfig,
)
from telebot_components.redis_utils.interface import RedisInterface


async def create_trello_integrated_feedback_bot(
    token: str,
    admin_chat_id: int,
    user_api_key: str,
    user_token: str,
    organization_name: str,
    board_name: str,
    reply_with_card_comments: bool,
    base_url: str,
    redis: RedisInterface,
    server_listening_future: asyncio.Future,
    unanswered_label: bool,
    unanswered_label_name: str,
    unanswered_label_color: TrelloLabelColor,
) -> BotRunner:
    bot_prefix = f"trello-integration-bot"

    bot = AsyncTeleBot(token)

    trello_integration = TrelloIntegration(
        bot=bot,
        redis=redis,
        bot_prefix=bot_prefix,
        admin_chat_id=admin_chat_id,
        reply_with_card_comments=reply_with_card_comments,
        credentials=TrelloIntegrationCredentials(
            user_api_key=user_api_key,
            user_token=user_token,
            organization_name=organization_name,
            board_name=board_name,
        ),
        unanswered_label=unanswered_label,
        unanswered_label_config=UnansweredLabelConfig(name=unanswered_label_name, color=unanswered_label_color),
    )

    await trello_integration.initialize()

    feedback_handler = FeedbackHandler(
        admin_chat_id,
        redis,
        bot_prefix,
        config=FeedbackConfig(
            message_log_to_admin_chat=True,
            force_category_selection=False,
            hashtags_in_admin_chat=True,
            hashtag_message_rarer_than=times.FIVE_MINUTES,
            unanswered_hashtag="новое",
        ),
        anti_spam=DisabledAntiSpam(),
        service_messages=ServiceMessages(
            forwarded_to_admin_ok="ok",
            you_must_select_category=None,
            throttling_template=None,
            copied_to_user_ok="response ok",
        ),
        trello_integration=trello_integration,
    )

    @bot.message_handler(commands=["start", "help"], chat_types=[tg_constants.ChatType.private])
    async def start_cmd_handler(message: tg.Message):
        await feedback_handler.emulate_user_message(
            bot=bot,
            user=message.from_user,
            text=f"{message.from_user.full_name} just sent /start",
            no_response=True,
        )
        await bot.send_message(message.from_user.id, "hi there")

    feedback_handler.setup(bot)

    if reply_with_card_comments:
        endpoints = await trello_integration.get_webhook_endpoints()
        background_jobs = [
            trello_integration.initialize_webhook(
                base_url=base_url,
                server_listening_future=server_listening_future,
            )
        ]
    else:
        endpoints = []
        background_jobs = []

    return BotRunner(
        bot_prefix=bot_prefix,
        bot=bot,
        aux_endpoints=endpoints,
        background_jobs=background_jobs,
    )


if __name__ == "__main__":
    """
    For Trello comment replies to work requires publicly available URL to set webhook.

    One-way Trello integration can be run locally with polling.
    """

    import asyncio
    import logging
    import os

    from dotenv import load_dotenv
    from redis.asyncio import Redis  # type: ignore
    from telebot.webhook import WebhookApp

    from telebot_components.redis_utils.emulation import RedisEmulation

    load_dotenv()

    redis_url = os.environ.get("REDIS_URL")
    redis = Redis.from_url(redis_url) if redis_url is not None else RedisEmulation()

    logging.basicConfig(level=logging.DEBUG)

    EXPORT_ONLY = True

    BASE_URL = "https://my-deployed-app.com"  # no trailing slash!; unused when ONE_WAY=True

    async def main():
        server_listening_future = asyncio.Future()
        bot_runner = await create_trello_integrated_feedback_bot(
            token=os.environ["TOKEN"],
            admin_chat_id=int(os.environ["ADMIN_CHAT_ID"]),
            user_api_key=os.environ["TRELLO_USER_API_KEY"],
            user_token=os.environ["TRELLO_USER_TOKEN"],
            organization_name=os.environ["TRELLO_ORG_NAME"],
            board_name=os.environ["TRELLO_BOARD_NAME"],
            reply_with_card_comments=not EXPORT_ONLY,
            base_url=BASE_URL,
            redis=redis,
            server_listening_future=server_listening_future,
            unanswered_label=True,
            unanswered_label_color="orange",
            unanswered_label_name="Not answered",
        )

        if EXPORT_ONLY:
            await bot_runner.run_polling()
        else:
            webhook_app = WebhookApp(base_url=BASE_URL)
            await webhook_app.add_bot_runner(bot_runner)

            async def on_server_listening():
                server_listening_future.set_result(None)

            await webhook_app.run(port=8080, on_server_listening=on_server_listening)

    asyncio.run(main())
