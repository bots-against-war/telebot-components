import asyncio
import string
import uuid
from datetime import timedelta
from typing import Optional

from telebot import types as tg
from telebot.test_util import MockedAsyncTeleBot

from telebot_components.feedback import (
    FeedbackConfig,
    FeedbackHandler,
    ServiceMessages,
    UserAnonymization,
)
from telebot_components.feedback.anti_spam import AntiSpam, AntiSpamConfig
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.category import Category, CategoryStore
from telebot_components.stores.forum_topics import (
    CategoryForumTopicStore,
    ForumTopicSpec,
    ForumTopicStore,
    ForumTopicStoreErrorMessages,
)
from tests.utils import (
    TelegramServerMock,
    TimeSupplier,
    assert_list_of_required_subdicts,
)

ADMIN_CHAT_ID = 111
ADMIN_USER_ID = 1312
USER_ID = 420


def create_mock_feedback_handler(
    redis: RedisInterface,
    is_throttling: bool,
    has_categories: bool,
    has_forum_topics: bool,
    user_anonymization: UserAnonymization = UserAnonymization.LEGACY,
) -> FeedbackHandler:
    bot_prefix = uuid.uuid4().hex[:8]

    if has_categories:
        category_1 = Category(name="one", hashtag="one")
        category_2 = Category(name="two", hashtag="two")
        category_3 = Category(name="three", hashtag="three")
        category_store: Optional[CategoryStore] = CategoryStore(
            bot_prefix=bot_prefix,
            redis=redis,
            categories=[category_1, category_2, category_3],
            category_expiration_time=None,
        )
    else:
        category_store = None

    if has_forum_topics:
        if not has_categories:
            raise ValueError("forum topics require categories")
        forum_topic_1 = ForumTopicSpec(name="topic 1")
        forum_topic_2 = ForumTopicSpec(name="topic 2")
        forum_topic_3 = ForumTopicSpec(name="topic 3")
        forum_topic_store: Optional[CategoryForumTopicStore] = CategoryForumTopicStore(
            forum_topic_store=ForumTopicStore(
                redis=redis,
                bot_prefix=bot_prefix,
                admin_chat_id=ADMIN_CHAT_ID,
                topics=[forum_topic_1, forum_topic_2, forum_topic_3],
                error_messages=ForumTopicStoreErrorMessages(
                    admin_chat_is_not_forum_error="not a forum! will check again in {} sec",
                    cant_create_topic="error creating topic {}: {}; will try again in {} sec",
                ),
            ),
            forum_topic_by_category={
                category_1: forum_topic_1,
                category_2: forum_topic_2,
                category_3: forum_topic_3,
            },
        )
    else:
        forum_topic_store = None

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
            user_anonymization=user_anonymization,
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
        category_store=category_store,
        forum_topic_store=forum_topic_store,
    )


async def test_feedback_handler_throttling(redis: RedisInterface):
    bot = MockedAsyncTeleBot("token")
    feedback_handler = create_mock_feedback_handler(
        redis,
        is_throttling=True,
        has_categories=False,
        has_forum_topics=False,
    )

    await feedback_handler.setup(bot)
    assert not bot.method_calls

    @bot.message_handler(commands=["start"])
    async def welcome_user(m: tg.Message):
        await bot.reply_to(m, "Welcome to the Drug Selling Bot, please tell us what you need!")

    telegram = TelegramServerMock(admin_chats={ADMIN_CHAT_ID})

    await telegram.send_message_to_bot(bot, user_id=USER_ID, text="/start")
    await telegram.send_message_to_bot(bot, user_id=USER_ID, text="Hi I want to buy drugs")
    await telegram.send_message_to_bot(bot, user_id=USER_ID, text="Could you help me?")
    await telegram.send_message_to_bot(bot, user_id=USER_ID, text="Please")
    await telegram.send_message_to_bot(bot, user_id=USER_ID, text="Please please")

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

    await telegram.send_message_to_bot(
        bot,
        user_id=ADMIN_USER_ID,
        chat_id=ADMIN_CHAT_ID,
        text="This message should be ignored",
    )
    await telegram.send_message_to_bot(
        bot,
        user_id=ADMIN_USER_ID,
        chat_id=ADMIN_CHAT_ID,
        text="You can buy a lot of drugs from us",
        reply_to_message_id=4,
    )
    await telegram.send_message_to_bot(
        bot,
        user_id=ADMIN_USER_ID,
        chat_id=ADMIN_CHAT_ID,
        text="Please tell us more about what you need",
        reply_to_message_id=4,
    )
    await telegram.send_message_to_bot(
        bot,
        user_id=ADMIN_USER_ID,
        chat_id=ADMIN_CHAT_ID,
        text="And who you are",
        reply_to_message_id=6,
    )

    assert set(bot.method_calls.keys()) == {"copy_message", "send_message", "delete_message", "get_chat"}
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
    feedback_handler = create_mock_feedback_handler(
        redis,
        is_throttling=False,
        has_categories=False,
        has_forum_topics=False,
    )

    await feedback_handler.setup(bot)
    assert not bot.method_calls

    telegram = TelegramServerMock(admin_chats={ADMIN_CHAT_ID})

    # user sends all 26 letter of the alphabet
    for letter in string.ascii_uppercase:
        await telegram.send_message_to_bot(bot, user_id=USER_ID, text=letter)

    assert set(bot.method_calls.keys()) == {"send_message", "forward_message"}
    assert_list_of_required_subdicts(
        actual_dicts=[mc.full_kwargs for mc in bot.method_calls["forward_message"]],
        required_subdicts=[{"chat_id": 111, "from_chat_id": 420, "message_id": idx} for idx in range(1, 27)],
    )
    bot.method_calls.clear()

    # admin requests message log
    await telegram.send_message_to_bot(
        bot,
        user_id=ADMIN_USER_ID,
        chat_id=ADMIN_CHAT_ID,
        text="/log",
        reply_to_message_id=26,
    )
    assert set(bot.method_calls.keys()) == {"send_message", "get_chat", "forward_message"}
    assert [mc.full_kwargs for mc in bot.method_calls["send_message"]] == [
        {"chat_id": 111, "text": "📜 Log page 1 / 6"},
        {"chat_id": 111, "text": "⬆️ Log page 1 / 6\nNext: <code>/log 2</code>", "parse_mode": "HTML"},
    ]
    assert [mc.full_kwargs for mc in bot.method_calls["forward_message"]] == [
        {"chat_id": 111, "from_chat_id": 111, "message_id": message_id} for message_id in range(4, 13, 2)
    ]
    bot.method_calls.clear()

    # admin requests message log page 5 by answering to another message
    await telegram.send_message_to_bot(
        bot, user_id=ADMIN_USER_ID, chat_id=ADMIN_CHAT_ID, text="/log 5", reply_to_message_id=4
    )
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
    await telegram.send_message_to_bot(
        bot,
        user_id=ADMIN_USER_ID,
        chat_id=ADMIN_CHAT_ID,
        text="/log -1",
        reply_to_message_id=44,
    )
    assert set(bot.method_calls.keys()) == {"send_message", "forward_message"}
    assert [mc.full_kwargs for mc in bot.method_calls["send_message"]] == [
        {"chat_id": 111, "text": "📜 Log page 6 / 6"},
        {"chat_id": 111, "text": "⬆️ Log page 6 / 6", "parse_mode": "HTML"},
    ]
    assert [mc.full_kwargs for mc in bot.method_calls["forward_message"]] == [
        {"chat_id": 111, "from_chat_id": 111, "message_id": 54}
    ]
    bot.method_calls.clear()


async def test_forum_topics_for_categories(redis: RedisInterface, time_supplier: TimeSupplier) -> None:
    bot = MockedAsyncTeleBot("token123134134")
    feedback_handler = create_mock_feedback_handler(
        redis,
        is_throttling=False,
        has_categories=True,
        has_forum_topics=True,
    )
    await feedback_handler.setup(bot)

    bot.add_return_values("get_chat", tg.Chat(id=ADMIN_CHAT_ID, type="supergroup", is_forum=True))
    bot.add_return_values(
        "create_forum_topic",
        *[
            tg.ForumTopic(
                message_thread_id=100 + idx,
                name=f"topic {idx}",
                icon_color=0,
            )
            for idx in range(3)
        ],
    )

    # actual setup for forum topic store
    await asyncio.wait_for(
        asyncio.gather(*feedback_handler.background_jobs(None, None)),
        timeout=1,
    )

    # setup calls check

    get_chat_calls = bot.method_calls.pop("get_chat")
    assert len(get_chat_calls) == 1
    assert get_chat_calls[0].full_kwargs == {"chat_id": ADMIN_CHAT_ID}

    create_forum_topic_calls = bot.method_calls.pop("create_forum_topic")
    assert len(create_forum_topic_calls) == 3
    assert [c.full_kwargs for c in create_forum_topic_calls] == [
        {"chat_id": 111, "name": "topic 1", "icon_color": 7322096, "icon_custom_emoji_id": None},
        {"chat_id": 111, "name": "topic 2", "icon_color": 16766590, "icon_custom_emoji_id": None},
        {"chat_id": 111, "name": "topic 3", "icon_color": 13338331, "icon_custom_emoji_id": None},
    ]

    bot.method_calls.clear()

    telegram = TelegramServerMock(admin_chats={ADMIN_CHAT_ID})

    assert feedback_handler.category_store
    await feedback_handler.category_store.save_user_category(
        tg.User(id=USER_ID, is_bot=False, first_name="User"),
        Category(name="two"),
    )

    await telegram.send_message_to_bot(bot, user_id=USER_ID, text="hello I am user in category 1")

    assert_list_of_required_subdicts(
        [c.full_kwargs for c in bot.method_calls["send_message"]],
        [
            {"chat_id": 111, "text": "#hey_there #two", "message_thread_id": 101},
            {"chat_id": 420, "text": "thanks", "reply_to_message_id": 1},
        ],
    )
    assert_list_of_required_subdicts(
        [c.full_kwargs for c in bot.method_calls["forward_message"]],
        [{"chat_id": 111, "from_chat_id": 420, "message_id": 1, "message_thread_id": 101}],
    )
