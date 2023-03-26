import datetime
from typing import Optional

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.test_util import MockedAsyncTeleBot

from telebot_components.feedback import FeedbackConfig, FeedbackHandler, ServiceMessages
from telebot_components.feedback.anti_spam import DisabledAntiSpam
from telebot_components.feedback.integration.interface import (
    FeedbackHandlerIntegration,
    UserMessageRepliedFromIntegrationEvent,
)
from telebot_components.feedback.types import UserMessageRepliedEvent
from telebot_components.redis_utils.emulation import RedisEmulation
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.category import Category
from tests.utils import assert_list_of_required_subdicts


class MockFeedbackHandlerIntegration(FeedbackHandlerIntegration):
    def __init__(self, name: str) -> None:
        self._name = name
        self.handled_messages: list[tuple[tg.Message, int]] = []
        self.handled_user_message_replied_events: list[UserMessageRepliedEvent] = []

    def name(self) -> str:
        return self._name

    async def handle_user_message(
        self, message: tg.Message, admin_chat_message_id: int, category: Optional[Category], bot: AsyncTeleBot
    ) -> None:
        self.handled_messages.append((message, admin_chat_message_id))

    async def handle_user_message_replied_elsewhere(self, event: UserMessageRepliedEvent) -> None:
        self.handled_user_message_replied_events.append(event)


ADMIN_USER_ID = 123456
ADMIN_CHAT_ID = 111000111000
USER_ID = 7890123


async def test_feedback_handler_integration_basic(redis: RedisInterface) -> None:
    bot = MockedAsyncTeleBot(token="whatever")
    integrations = (
        MockFeedbackHandlerIntegration("mock integration 1"),
        MockFeedbackHandlerIntegration("mock integration 2"),
        MockFeedbackHandlerIntegration("mock integration 3"),
    )
    feedback_handler = FeedbackHandler(
        admin_chat_id=ADMIN_CHAT_ID,
        redis=redis,
        bot_prefix="test-feedback-handler-integrations-bot",
        config=FeedbackConfig(
            message_log_to_admin_chat=True,
            force_category_selection=False,
            hashtags_in_admin_chat=True,
            hashtag_message_rarer_than=None,
            unanswered_hashtag=None,
            full_user_anonymization=True,
        ),
        anti_spam=DisabledAntiSpam(),
        service_messages=ServiceMessages(
            forwarded_to_admin_ok="fwd ok",
            copied_to_user_ok="copied ok",
        ),
        integrations=list(integrations),
    )

    await feedback_handler.setup(bot)

    _message_id_counter = 0

    async def _send_message_to_bot(in_admin_chat: bool, text: str, reply_to_message_id: Optional[int] = None):
        nonlocal _message_id_counter
        _message_id_counter += 1
        user_id = ADMIN_USER_ID if in_admin_chat else USER_ID

        update_json = {
            "update_id": 19283649187364,
            "message": {
                "message_id": _message_id_counter,
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": "Admin" if in_admin_chat else "User",
                },
                "chat": {
                    "id": ADMIN_CHAT_ID if in_admin_chat else user_id,
                    "type": "supergroup" if in_admin_chat else "private",
                },
                "date": int(datetime.datetime.now().timestamp()),
                "text": text,
            },
        }

        if reply_to_message_id is not None:
            update_json["message"]["reply_to_message"] = {  # type: ignore
                "message_id": reply_to_message_id,
                "from": {
                    "id": 1,
                    "is_bot": True,
                    "first_name": "Bot",
                },
                "chat": {
                    "id": ADMIN_CHAT_ID,
                    "type": "supergroup",
                },
                "date": 1662891416,
                "text": "replied-to-message-text",
            }
        await bot.process_new_updates([tg.Update.de_json(update_json)])  # type: ignore

    for integration in integrations:
        assert integration.message_replied_callback is not None

    await _send_message_to_bot(in_admin_chat=False, text="Hello this is user please respond")
    for integration in integrations:
        assert len(integration.handled_messages) == 1
        message, admin_message_id = integration.handled_messages[0]
        assert admin_message_id == 4
        assert message.text == "Hello this is user please respond"

    user_message_replied_event = UserMessageRepliedFromIntegrationEvent(
        bot=bot,
        origin_chat_id=USER_ID,
        reply_text="Hello from integration",
        reply_has_attachments=False,
        reply_author="some guy",
        reply_link=None,
        main_admin_chat_message_id=4,
        integration=integrations[0],
    )
    assert integrations[0].message_replied_callback is not None
    await integrations[0].message_replied_callback(user_message_replied_event)
    for integration in integrations[1:]:
        assert len(integration.handled_user_message_replied_events) == 1
        assert integration.handled_user_message_replied_events[0] == user_message_replied_event

    assert_list_of_required_subdicts(
        [mc.full_kwargs for mc in bot.method_calls["send_message"]],
        [
            {"chat_id": ADMIN_CHAT_ID, "text": "🤭🎺👪☔"},
            {"chat_id": USER_ID, "text": "fwd ok", "reply_to_message_id": 1},
            {
                "chat_id": ADMIN_CHAT_ID,
                "reply_to_message_id": 4,
                "text": "💬 <b>some guy</b> via mock integration 1\n\nHello from integration",
                "parse_mode": "HTML",
            },
        ],
    )
    assert_list_of_required_subdicts(
        [mc.full_kwargs for mc in bot.method_calls["copy_message"]],
        [{"chat_id": ADMIN_CHAT_ID, "from_chat_id": USER_ID, "message_id": 1}],
    )
