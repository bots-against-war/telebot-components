import logging
from typing import Type

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner
from telebot.types import constants as tg_constants

from telebot_components.constants import times
from telebot_components.feedback import FeedbackConfig, FeedbackHandler, ServiceMessages
from telebot_components.feedback.anti_spam import AntiSpam, AntiSpamConfig
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.banned_users import BannedUsersStore
from telebot_components.stores.category import Category, CategoryStore
from telebot_components.stores.language import (
    Language,
    LanguageSelectionMenuConfig,
    LanguageStore,
    any_text_to_str,
)


def create_feedback_bot(BotClass: Type[AsyncTeleBot], redis: RedisInterface, token: str, admin_chat_id: int):
    bot_prefix = "example-feedback-bot"
    bot = BotClass(token)

    logging.basicConfig(level=logging.DEBUG)

    language_store = LanguageStore(
        bot_prefix,
        redis,
        supported_languages=[Language.RU, Language.EN],
        default_language=Language.RU,
        menu_config=LanguageSelectionMenuConfig(emojj_buttons=True, select_with_checkmark=True),
    )

    category_store = CategoryStore(
        bot_prefix,
        redis,
        categories=[
            Category(
                id=1,
                name="Картошка",
                button_caption={Language.RU: "🥔 Картошка", Language.EN: "🥔 Potato"},
                hashtag="картофель",
            ),
            Category(
                id=2,
                name="Капуста",
                button_caption={Language.RU: "🥦 Капуста", Language.EN: "🥦 Cabbage"},
                hashtag="капуста",
            ),
        ],
        category_expiration_time=times.FIVE_MINUTES,
        language_store=language_store,
    )

    banned_store = BannedUsersStore(
        bot_prefix,
        redis,
        cached=False,
    )

    WELCOME_MESSAGE = {
        Language.RU: "Добро пожаловать! Это тестовый бот. Пожалуйста, выберите категорию. /language - выбрать язык",
        Language.EN: "Welcome! This is a test bot. Please select category. /language - select language",
    }
    ON_CATEGORY_SELECTED_MESSAGE = {
        Language.RU: "Категория сохранена: {}!",
        Language.EN: "Category saved: {}!",
    }

    language_store.validate_multilang(WELCOME_MESSAGE)
    language_store.validate_multilang(ON_CATEGORY_SELECTED_MESSAGE)

    async def welcome(user: tg.User):
        language = await language_store.get_user_language(user)
        await bot.send_message(
            user.id,
            WELCOME_MESSAGE[language],
            reply_markup=(await category_store.markup_for_user_localised(user, language)),
        )

    @bot.message_handler(
        commands=["start", "help"], chat_types=[tg_constants.ChatType.private], func=banned_store.not_from_banned_user
    )
    async def start_cmd_handler(message: tg.Message):
        await welcome(message.from_user)

    @bot.message_handler(
        commands=["language"], chat_types=[tg_constants.ChatType.private], func=banned_store.not_from_banned_user
    )
    async def select_language_cmd_handler(message: tg.Message):
        await bot.send_message(
            message.from_user.id,
            "Выберите язык / choose language",
            reply_markup=(await language_store.markup_for_user(message.from_user)),
        )

    feedback_handler = FeedbackHandler(
        admin_chat_id,
        bot_prefix,
        redis,
        config=FeedbackConfig(
            message_log_to_admin_chat=True,
            force_category_selection=True,
            hashtags_in_admin_chat=True,
            hashtag_message_rarer_than=times.FIVE_MINUTES,
            unanswered_hashtag="неотвечено",
            forwarded_to_admin_confirmations_throttle_duration=times.FIVE_MINUTES,
        ),
        anti_spam=AntiSpam(
            redis,
            bot_prefix,
            config=AntiSpamConfig(
                throttle_after_messages=3,
                throttle_duration=times.FIVE_MINUTES,
                soft_ban_after_throttle_violations=5,
                soft_ban_duration=times.HOUR,
            ),
        ),
        service_messages=ServiceMessages(
            forwarded_to_admin_ok={
                Language.RU: "Переслано!",
                Language.EN: "Forwarded!",
            },
            you_must_select_category={
                Language.RU: "Пожалуйста, сначала выберите категорию!",
                Language.EN: "Please select category first!",
            },
            throttling_template={
                Language.RU: "Пожалуйста, не присылайте в бот больше {} сообщений за {}.",
                Language.EN: "Please send no more than {} messages in {}.",
            },
            copied_to_user_ok="Скопировано в чат с пользовател_ьницей ✨",
        ),
        banned_users_store=banned_store,
        language_store=language_store,
        category_store=category_store,
        # trello_integration=TBD
    )

    async def on_language_selected(bot: AsyncTeleBot, menu_message: tg.Message, user: tg.User, new_option: Language):
        await welcome(user)

    language_store.setup(bot, on_language_change=on_language_selected)

    async def on_category_selected(bot: AsyncTeleBot, menu_message: tg.Message, user: tg.User, new_option: Category):
        language = await language_store.get_user_language(user)
        await bot.send_message(
            user.id, ON_CATEGORY_SELECTED_MESSAGE[language].format(any_text_to_str(new_option.button_caption, language))
        )

    category_store.setup(bot, on_category_selected=on_category_selected)

    feedback_handler.setup(bot)

    return BotRunner(
        name=bot_prefix,
        bot=bot,
    )


if __name__ == "__main__":
    import os

    from redis.asyncio import Redis  # type: ignore

    from telebot_components.redis_utils.emulation import RedisEmulation

    redis = RedisEmulation()
    # redis = Redis.from_url(os.environ["REDIS_URL"])
    bot_runner = create_feedback_bot(
        AsyncTeleBot,
        redis=redis,
        token=os.environ["TOKEN"],
        admin_chat_id=int(os.environ["ADMIN_CHAT_ID"]),
    )
    bot_runner.run_polling()
