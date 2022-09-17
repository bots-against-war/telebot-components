from datetime import datetime, timedelta
from typing import Optional

import pytest
from telebot import types as tg
from telebot.test_util import MockedAsyncTeleBot

from telebot_components.feedback import FeedbackConfig, FeedbackHandler, ServiceMessages
from telebot_components.feedback.anti_spam import AntiSpam, AntiSpamConfig
from telebot_components.redis_utils.interface import RedisInterface
from tests.utils import assert_list_of_required_subdicts

ADMIN_CHAT_ID = 111
ADMIN_USER_ID = 1312
USER_ID = 420


@pytest.fixture
def feedback_handler(redis: RedisInterface) -> FeedbackHandler:
    return FeedbackHandler(
        admin_chat_id=ADMIN_CHAT_ID,
        redis=redis,
        bot_prefix="testing",
        config=FeedbackConfig(
            message_log_to_admin_chat=True,
            force_category_selection=False,
            hashtags_in_admin_chat=True,
            hashtag_message_rarer_than=None,
            unanswered_hashtag="hey_there",
        ),
        service_messages=ServiceMessages(
            forwarded_to_admin_ok="thanks",
            throttling_template="nope",
            copied_to_user_ok="nice",
        ),
        anti_spam=AntiSpam(
            redis=redis,
            bot_prefix="testing",
            config=AntiSpamConfig(
                throttle_after_messages=3,
                throttle_duration=timedelta(seconds=5),
                soft_ban_after_throttle_violations=100,
                soft_ban_duration=timedelta(days=3),
            ),
        ),
    )


async def test_feedback_handler(feedback_handler: FeedbackHandler):
    bot = MockedAsyncTeleBot("token")
    feedback_handler.setup(bot)

    @bot.message_handler(commands=["start"])
    async def welcome_user(m: tg.Message):
        await bot.reply_to(m, "Welcome to the Drug Selling Bot, please tell us what you need!")

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
                "date": int(datetime.now().timestamp()),
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

    await _send_message_to_bot(in_admin_chat=False, text="/start")
    await _send_message_to_bot(in_admin_chat=False, text="Hi I want to buy drugs")
    await _send_message_to_bot(in_admin_chat=False, text="Could you help me?")
    await _send_message_to_bot(in_admin_chat=False, text="Please")
    await _send_message_to_bot(in_admin_chat=False, text="Please please")

    assert set(bot.method_calls.keys()) == {"send_message", "forward_message"}
    assert_list_of_required_subdicts(
        actual_dicts=[mc.full_kwargs for mc in bot.method_calls["send_message"]],
        required_subdicts=[
            # welcome message
            {
                "chat_id": USER_ID,
                "text": "Welcome to the Drug Selling Bot, please tell us what you need!",
                "reply_to_message_id": 1,
            },
            # unread hashtag message in admin chat
            {"chat_id": 111, "text": "#hey_there"},
            # confirmations
            {"chat_id": 420, "text": "thanks", "reply_to_message_id": 2},
            {"chat_id": 420, "text": "thanks", "reply_to_message_id": 3},
            {"chat_id": 420, "text": "thanks", "reply_to_message_id": 4},
            # throttling message
            {"chat_id": 420, "text": "nope", "reply_to_message_id": 5},
        ],
    )
    assert_list_of_required_subdicts(
        actual_dicts=[mc.full_kwargs for mc in bot.method_calls["forward_message"]],
        required_subdicts=[
            {"chat_id": 111, "from_chat_id": 420, "message_id": 2},
            {"chat_id": 111, "from_chat_id": 420, "message_id": 3},
            {"chat_id": 111, "from_chat_id": 420, "message_id": 4},
        ],
    )
    bot.method_calls.clear()

    await _send_message_to_bot(in_admin_chat=True, text="This message should be ignored")
    await _send_message_to_bot(in_admin_chat=True, text="You can buy a lot of drugs from us", reply_to_message_id=4)
    await _send_message_to_bot(
        in_admin_chat=True, text="Please tell us more about what you need", reply_to_message_id=4
    )
    await _send_message_to_bot(in_admin_chat=True, text="And who you are", reply_to_message_id=6)

    assert set(bot.method_calls.keys()) == {"copy_message", "send_message", "delete_message"}
    assert_list_of_required_subdicts(
        actual_dicts=[mc.full_kwargs for mc in bot.method_calls["copy_message"]],
        required_subdicts=[
            {"chat_id": 420, "from_chat_id": 111, "message_id": 7},
            {"chat_id": 420, "from_chat_id": 111, "message_id": 8},
            {"chat_id": 420, "from_chat_id": 111, "message_id": 9},
        ],
    )
    assert_list_of_required_subdicts(
        actual_dicts=[mc.full_kwargs for mc in bot.method_calls["send_message"]],
        required_subdicts=[
            {"chat_id": 111, "text": "nice", "reply_to_message_id": 7},
            {"chat_id": 111, "text": "nice", "reply_to_message_id": 8},
            {"chat_id": 111, "text": "nice", "reply_to_message_id": 9},
        ],
    )
    assert_list_of_required_subdicts(
        actual_dicts=[mc.full_kwargs for mc in bot.method_calls["delete_message"]],
        required_subdicts=[
            # hashtag message is deleted (twice, the second is effectively a noop)
            {"chat_id": 111, "message_id": 2},
            {"chat_id": 111, "message_id": 2},
        ],
    )