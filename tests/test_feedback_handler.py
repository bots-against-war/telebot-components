import string
import uuid
from datetime import datetime, timedelta
from typing import Optional

from telebot import types as tg
from telebot.test_util import MockedAsyncTeleBot

from telebot_components.feedback import FeedbackConfig, FeedbackHandler, ServiceMessages
from telebot_components.feedback.anti_spam import AntiSpam, AntiSpamConfig
from telebot_components.redis_utils.interface import RedisInterface
from tests.utils import TimeSupplier, assert_list_of_required_subdicts

ADMIN_CHAT_ID = 111
ADMIN_USER_ID = 1312
USER_ID = 420


def create_mock_feedback_handler(redis: RedisInterface, is_throttling: bool) -> FeedbackHandler:
    bot_prefix = uuid.uuid4().hex[:8]
    return FeedbackHandler(
        admin_chat_id=ADMIN_CHAT_ID,
        redis=redis,
        bot_prefix=bot_prefix,
        config=FeedbackConfig(
            message_log_to_admin_chat=True,
            message_log_page_size=5,
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
            bot_prefix=bot_prefix,
            config=AntiSpamConfig(
                throttle_after_messages=3 if is_throttling else 10000,
                throttle_duration=timedelta(seconds=5),
                soft_ban_after_throttle_violations=100,
                soft_ban_duration=timedelta(days=3),
            ),
        ),
    )


async def test_feedback_handler_throttling(redis: RedisInterface):
    bot = MockedAsyncTeleBot("token")
    feedback_handler = create_mock_feedback_handler(redis, is_throttling=True)

    await feedback_handler.setup(bot)
    assert not bot.method_calls

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


async def test_message_log(redis: RedisInterface, time_supplier: TimeSupplier):
    bot = MockedAsyncTeleBot("token")
    feedback_handler = create_mock_feedback_handler(redis, is_throttling=False)

    await feedback_handler.setup(bot)
    assert not bot.method_calls

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

    # user sends all 26 letter of the alphabet
    for letter in string.ascii_uppercase:
        await _send_message_to_bot(in_admin_chat=False, text=letter)

    assert set(bot.method_calls.keys()) == {"send_message", "forward_message"}
    assert_list_of_required_subdicts(
        actual_dicts=[mc.full_kwargs for mc in bot.method_calls["forward_message"]],
        required_subdicts=[{"chat_id": 111, "from_chat_id": 420, "message_id": idx} for idx in range(1, 27)],
    )
    bot.method_calls.clear()

    # admin requests message log
    await _send_message_to_bot(in_admin_chat=True, text="/log", reply_to_message_id=26)
    assert set(bot.method_calls.keys()) == {"send_message", "forward_message"}
    assert [mc.full_kwargs for mc in bot.method_calls["send_message"]] == [
        {"chat_id": 111, "text": "📜 Log page 1 / 6"},
        {"chat_id": 111, "text": "⬆️ Log page 1 / 6\nNext: <code>/log 2</code>", "parse_mode": "HTML"},
    ]
    assert [mc.full_kwargs for mc in bot.method_calls["forward_message"]] == [
        {"chat_id": 111, "from_chat_id": 111, "message_id": message_id} for message_id in range(4, 13, 2)
    ]
    bot.method_calls.clear()

    # admin requests message log page 5 by answering to another message
    await _send_message_to_bot(in_admin_chat=True, text="/log 5", reply_to_message_id=4)
    assert set(bot.method_calls.keys()) == {"send_message", "forward_message"}
    assert [mc.full_kwargs for mc in bot.method_calls["send_message"]] == [
        {"chat_id": 111, "text": "📜 Log page 5 / 6"},
        {"chat_id": 111, "text": "⬆️ Log page 5 / 6\nNext: <code>/log 6</code>", "parse_mode": "HTML"},
    ]
    assert [mc.full_kwargs for mc in bot.method_calls["forward_message"]] == [
        {"chat_id": 111, "from_chat_id": 111, "message_id": message_id} for message_id in range(44, 53, 2)
    ]
    bot.method_calls.clear()

    # admin requests message log on the last page (with only 1 message) by answering on a log message
    await _send_message_to_bot(in_admin_chat=True, text="/log -1", reply_to_message_id=44)
    assert set(bot.method_calls.keys()) == {"send_message", "forward_message"}
    assert [mc.full_kwargs for mc in bot.method_calls["send_message"]] == [
        {"chat_id": 111, "text": "📜 Log page 6 / 6"},
        {"chat_id": 111, "text": "⬆️ Log page 6 / 6", "parse_mode": "HTML"},
    ]
    assert [mc.full_kwargs for mc in bot.method_calls["forward_message"]] == [
        {"chat_id": 111, "from_chat_id": 111, "message_id": 54}
    ]
    bot.method_calls.clear()
