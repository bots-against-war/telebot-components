import datetime

from telebot import types as tg
from telebot.test_util import MockedAsyncTeleBot

from telebot_components.feedback import (
    FeedbackConfig,
    FeedbackHandler,
    ServiceMessages,
    UserAnonymization,
)
from telebot_components.feedback.anti_spam import (
    AntiSpam,
    AntiSpamConfig,
    DisabledAntiSpam,
)
from telebot_components.feedback.integration.aux_feedback_handler import (
    AuxFeedbackHandlerIntegration,
)
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import GenericStore
from tests.utils import (
    TelegramServerMock,
    assert_list_of_required_subdicts,
    extract_full_kwargs,
)

USER_ID = 11001001
ADMIN_USER_ID = 80085
MAIN_ADMIN_CHAT_ID = 1312
AUX_ADMIN_CHAT_ID = 161


async def create_bot(
    bot_prefix: str,
    redis: RedisInterface,
    feedback_to_main_chat: bool,
    feedback_to_aux_chat: bool,
) -> MockedAsyncTeleBot:
    token = "dummy-token"
    bot = MockedAsyncTeleBot(token)

    aux_chat_handler = FeedbackHandler(
        name="admin-chat-mirror",
        admin_chat_id=AUX_ADMIN_CHAT_ID,
        redis=redis,
        bot_prefix=bot_prefix,
        config=FeedbackConfig(
            message_log_to_admin_chat=False,
            force_category_selection=False,
            hashtags_in_admin_chat=False,
            hashtag_message_rarer_than=datetime.timedelta(days=3),
            user_anonymization=UserAnonymization.FULL,
            forum_topic_per_user=True,
            user_forum_topic_lifetime=datetime.timedelta(days=60),
            unanswered_hashtag="new",
        ),
        anti_spam=DisabledAntiSpam(),  # handled by the main chat
        service_messages=ServiceMessages(
            copied_to_user_ok="ok",
            deleted_message_ok="deleted",
            can_not_delete_message="failed to delete",
            forwarded_to_admin_ok="forwarded (aux)",
            throttling_template="throttling (aux)",
        ),
    )

    main_chat_handler = FeedbackHandler(
        admin_chat_id=MAIN_ADMIN_CHAT_ID,
        redis=redis,
        bot_prefix=bot_prefix,
        config=FeedbackConfig(
            message_log_to_admin_chat=True,
            force_category_selection=False,
            hashtags_in_admin_chat=False,
            hashtag_message_rarer_than=datetime.timedelta(hours=3),
            unanswered_hashtag="new",
            confirm_forwarded_to_admin_rarer_than=datetime.timedelta(minutes=1),
        ),
        anti_spam=AntiSpam(
            redis=redis,
            bot_prefix=bot_prefix,
            config=AntiSpamConfig(
                throttle_after_messages=10,
                throttle_duration=datetime.timedelta(minutes=1),
                soft_ban_after_throttle_violations=20,
                soft_ban_duration=datetime.timedelta(days=3),
            ),
        ),
        service_messages=ServiceMessages(
            forwarded_to_admin_ok="forwarded (main)",
            throttling_template="throttling (main)",
            something_went_wrong="error (main)",
            copied_to_user_ok="ok",
        ),
        integrations=(
            [
                AuxFeedbackHandlerIntegration(
                    aux_chat_handler,
                    bot_prefix=bot_prefix,
                    redis=redis,
                )
            ]
            if feedback_to_aux_chat
            else []
        ),
    )

    if feedback_to_main_chat:
        await main_chat_handler.setup(bot)
    elif feedback_to_aux_chat:
        await aux_chat_handler.setup(bot)

    return bot


async def test_main_to_aux_chat_migration(redis: RedisInterface, normal_store_behavior: None) -> None:
    bot_prefix = "main-to-aux-chat-migration-test-bot-"

    before_bot = await create_bot(bot_prefix, redis, feedback_to_main_chat=True, feedback_to_aux_chat=True)
    GenericStore.allow_duplicate_stores(bot_prefix)

    before_bot.add_return_values(
        "create_forum_topic",
        tg.ForumTopic(
            message_thread_id=10000,
            name="whatever",
            icon_color=0,
        ),
    )

    telegram = TelegramServerMock(admin_chats={MAIN_ADMIN_CHAT_ID, AUX_ADMIN_CHAT_ID})

    await telegram.send_message_to_bot(before_bot, user_id=USER_ID, text="hello")
    assert set(before_bot.method_calls.keys()) == {
        "get_chat",
        "forward_message",
        "copy_message",
        "send_message",
        "create_forum_topic",
    }
    assert_list_of_required_subdicts(
        actual_dicts=extract_full_kwargs(before_bot.method_calls["send_message"]),
        required_subdicts=[{"chat_id": USER_ID, "text": "forwarded (main)"}],
    )
    assert_list_of_required_subdicts(
        actual_dicts=extract_full_kwargs(before_bot.method_calls["forward_message"]),
        required_subdicts=[{"chat_id": MAIN_ADMIN_CHAT_ID, "from_chat_id": USER_ID, "message_id": 1}],
    )
    assert_list_of_required_subdicts(
        actual_dicts=extract_full_kwargs(before_bot.method_calls["copy_message"]),
        required_subdicts=[{"chat_id": AUX_ADMIN_CHAT_ID, "from_chat_id": USER_ID, "message_id": 1}],
    )

    # switching off the old admin chat and moving to aux completely

    after_bot = await create_bot(bot_prefix, redis, feedback_to_aux_chat=True, feedback_to_main_chat=False)
    after_bot._latest_message_id_by_chat = before_bot._latest_message_id_by_chat
    GenericStore.allow_duplicate_stores(bot_prefix)
    await telegram.send_message_to_bot(after_bot, user_id=USER_ID, text="are you still there?")

    assert set(after_bot.method_calls.keys()) == {
        "get_chat",
        "copy_message",
        "send_message",
    }
    assert_list_of_required_subdicts(
        actual_dicts=extract_full_kwargs(after_bot.method_calls["send_message"]),
        required_subdicts=[{"chat_id": USER_ID, "text": "forwarded (aux)"}],
    )
    assert_list_of_required_subdicts(
        actual_dicts=extract_full_kwargs(after_bot.method_calls["copy_message"]),
        required_subdicts=[{"chat_id": AUX_ADMIN_CHAT_ID, "from_chat_id": USER_ID, "message_id": 2}],
    )
    after_bot.method_calls.clear()

    # answering to pre-migration message in aux chat (should work!)
    await telegram.send_message_to_bot(
        after_bot,
        user_id=ADMIN_USER_ID,
        chat_id=AUX_ADMIN_CHAT_ID,
        text="hi from aux admin chat",
        reply_to_message_id=2,
    )

    assert set(after_bot.method_calls.keys()) == {
        "copy_message",
        "send_message",
    }
    assert_list_of_required_subdicts(
        actual_dicts=extract_full_kwargs(after_bot.method_calls["copy_message"]),
        required_subdicts=[{"chat_id": USER_ID, "from_chat_id": AUX_ADMIN_CHAT_ID, "message_id": 3}],
    )
    assert_list_of_required_subdicts(
        actual_dicts=extract_full_kwargs(after_bot.method_calls["send_message"]),
        required_subdicts=[{"chat_id": 161, "text": "ok", "reply_to_message_id": 3}],
    )
