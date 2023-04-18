import datetime
from typing import Optional

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.test_util import MockedAsyncTeleBot

from telebot_components.feedback import FeedbackConfig, FeedbackHandler, ServiceMessages
from telebot_components.feedback.anti_spam import DisabledAntiSpam
from telebot_components.feedback.integration.aux_feedback_handler import (
    AuxFeedbackHandlerIntegration,
)
from telebot_components.feedback.integration.interface import (
    FeedbackHandlerIntegration,
    UserMessageRepliedFromIntegrationEvent,
)
from telebot_components.feedback.types import UserMessageRepliedEvent
from telebot_components.redis_utils.emulation import RedisEmulation
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.category import Category
from tests.utils import assert_list_of_required_subdicts, extract_full_kwargs


class MockFeedbackHandlerIntegration(FeedbackHandlerIntegration):
    def __init__(self, name: str) -> None:
        self._name = name
        self.handled_messages: list[tuple[tg.Message, Optional[tg.Message]]] = []
        self.handled_user_message_replied_events: list[UserMessageRepliedEvent] = []

    def name(self) -> str:
        return self._name

    async def handle_user_message(
        self,
        admin_chat_message: tg.Message,
        user: tg.User,
        user_message: Optional[tg.Message],
        category: Optional[Category],
        bot: AsyncTeleBot,
    ) -> None:
        self.handled_messages.append((admin_chat_message, user_message))

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
        admin_chat_message, user_message = integration.handled_messages[0]
        assert admin_chat_message.id == 4
        assert admin_chat_message.text == "Hello this is user please respond"
        assert user_message is not None
        assert user_message.from_user.first_name == "User"
        assert user_message.from_user.id == USER_ID

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


async def test_aux_admin_chat_integration(redis: RedisInterface) -> None:
    bot = MockedAsyncTeleBot(token="whatever")
    bot_prefix = "test-feedback-handler-integrations-bot-2"

    def create_feedback_handler(name: str, admin_chat_id: int):
        return FeedbackHandler(
            admin_chat_id=admin_chat_id,
            redis=redis,
            name=name,
            bot_prefix=bot_prefix,
            config=FeedbackConfig(
                message_log_to_admin_chat=True,
                force_category_selection=False,
                hashtags_in_admin_chat=True,
                hashtag_message_rarer_than=None,
                unanswered_hashtag=None,
                full_user_anonymization=False,
            ),
            anti_spam=DisabledAntiSpam(),
            service_messages=ServiceMessages(
                forwarded_to_admin_ok="fwd ok",
                copied_to_user_ok="copied ok",
            ),
        )

    main_feedback_handler = create_feedback_handler(name="", admin_chat_id=ADMIN_CHAT_ID)
    AUX_ADMIN_CHAT_IDS = [1312, 161]
    for idx, aux_admin_chat_id in enumerate(AUX_ADMIN_CHAT_IDS):
        main_feedback_handler.integrations.append(
            AuxFeedbackHandlerIntegration(
                feedback_handler=create_feedback_handler(name=f"aux-admin-chat-{idx}", admin_chat_id=aux_admin_chat_id),
                bot_prefix=bot_prefix,
                redis=redis,
            )
        )

    await main_feedback_handler.setup(bot)
    for integration in main_feedback_handler.integrations:
        assert integration.message_replied_callback is not None

    _message_id_counter = 0

    async def _send_message_to_bot(
        chat_id: int,
        is_from_admin: bool,
        text: str,
        reply_to_message_id: Optional[int] = None,
    ):
        nonlocal _message_id_counter
        _message_id_counter += 1
        user_id = ADMIN_USER_ID if is_from_admin else USER_ID

        update_json = {
            "update_id": 19283649187364,
            "message": {
                "message_id": _message_id_counter,
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": "Admin" if is_from_admin else "User",
                },
                "chat": {
                    "id": chat_id,
                    "type": "supergroup" if chat_id in {ADMIN_CHAT_ID, *AUX_ADMIN_CHAT_IDS} else "private",
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

    await _send_message_to_bot(chat_id=USER_ID, is_from_admin=False, text="hello i am user")
    await _send_message_to_bot(chat_id=USER_ID, is_from_admin=False, text="i like cats")

    # the user is replied only once
    assert set(bot.method_calls.keys()) == {"forward_message", "send_message"}
    assert_list_of_required_subdicts(
        extract_full_kwargs(bot.method_calls["send_message"]),
        [
            {"chat_id": 7890123, "text": "fwd ok", "reply_to_message_id": 1},
            {"chat_id": 7890123, "text": "fwd ok", "reply_to_message_id": 2},
        ],
    )
    # the message is forwarded to all admin chats
    assert_list_of_required_subdicts(
        extract_full_kwargs(bot.method_calls["forward_message"]),
        [
            {"chat_id": ADMIN_CHAT_ID, "from_chat_id": USER_ID, "message_id": 1},
            *[
                {"chat_id": aux_admin_chat_id, "from_chat_id": USER_ID, "message_id": 1}
                for aux_admin_chat_id in AUX_ADMIN_CHAT_IDS
            ],
            {"chat_id": ADMIN_CHAT_ID, "from_chat_id": USER_ID, "message_id": 2},
            *[
                {"chat_id": aux_admin_chat_id, "from_chat_id": USER_ID, "message_id": 2}
                for aux_admin_chat_id in AUX_ADMIN_CHAT_IDS
            ],
        ],
    )

    bot.method_calls.clear()

    await _send_message_to_bot(
        chat_id=ADMIN_CHAT_ID, is_from_admin=True, text="hello from the main admin chat", reply_to_message_id=2
    )
    assert extract_full_kwargs(bot.method_calls["copy_message"]) == [
        {"chat_id": 7890123, "from_chat_id": 111000111000, "message_id": 3}
    ]
    assert_list_of_required_subdicts(
        extract_full_kwargs(bot.method_calls["send_message"]),
        [
            {
                "chat_id": ADMIN_CHAT_ID,
                "text": "copied ok",
                "reply_to_message_id": 3,
            },
            *[
                {
                    "chat_id": aux_admin_chat_id,
                    "reply_to_message_id": 2,
                    "text": '💬 <b>Admin</b> via <a href="https://t.me/c/111000111000/3">main admin chat</a>\n\nhello from the main admin chat',
                    "parse_mode": "HTML",
                }
                for aux_admin_chat_id in AUX_ADMIN_CHAT_IDS
            ],
        ],
    )
    bot.method_calls.clear()

    await _send_message_to_bot(
        chat_id=AUX_ADMIN_CHAT_IDS[0], is_from_admin=True, text="hello from aux admin chat 1", reply_to_message_id=2
    )
    assert extract_full_kwargs(bot.method_calls["copy_message"]) == [
        {"chat_id": 7890123, "from_chat_id": AUX_ADMIN_CHAT_IDS[0], "message_id": 4}
    ]
    assert_list_of_required_subdicts(
        extract_full_kwargs(bot.method_calls["send_message"]),
        [
            {
                "chat_id": AUX_ADMIN_CHAT_IDS[0],
                "text": "copied ok",
                "reply_to_message_id": 4,
            },
            {
                "chat_id": 111000111000,
                "reply_to_message_id": 2,
                "text": '💬 <b>Admin</b> via <a href="https://t.me/c/1312/4">aux-admin-chat-0</a>\n\nhello from aux admin chat 1',
                "parse_mode": "HTML",
            },
            {
                "chat_id": 161,
                "reply_to_message_id": 2,
                "text": '💬 <b>Admin</b> via <a href="https://t.me/c/1312/4">aux-admin-chat-0</a>\n\nhello from aux admin chat 1',
                "parse_mode": "HTML",
            },
        ],
    )
    bot.method_calls.clear()
