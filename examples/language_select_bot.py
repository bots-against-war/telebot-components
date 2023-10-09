import asyncio
import logging

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner

from telebot_components.redis_utils.emulation import RedisEmulation
from telebot_components.stores.language import (
    Language,
    LanguageChangeContext,
    LanguageSelectionMenuConfig,
    LanguageStore,
)


async def create_multilang_bot(token: str):
    bot_prefix = "language-selectors-bot"
    bot = AsyncTeleBot(token)
    redis = RedisEmulation()

    language_store = LanguageStore(
        redis=redis,
        bot_prefix=bot_prefix,
        supported_languages=[Language.RU, Language.EN, Language.UK],
        default_language=Language.EN,
        menu_config=LanguageSelectionMenuConfig(
            emojj_buttons=True,
            select_with_checkmark=True,
            prompt={
                Language.RU: "Выберите язык",
                Language.EN: "Choose language",
                Language.UK: "Виберіть мову",
            },
        ),
    )

    async def welcome(user: tg.User):
        lang = await language_store.get_user_language(user)
        await bot.send_message(user.id, f"Your language is: {lang!r}")

    @bot.message_handler(commands=["start"])
    async def my_language(message: tg.Message) -> None:
        await welcome(message.from_user)

    @bot.message_handler(commands=["language_inline"])
    async def language_selector_inline(message: tg.Message):
        await language_store.send_inline_selector(bot, user=message.from_user)

    @bot.message_handler(commands=["language_reply"])
    async def language_selector_reply_kbd(message: tg.Message):
        await language_store.send_reply_keyboard_selector(bot, user=message.from_user)

    async def on_language_change(context: LanguageChangeContext):
        await welcome(context.user)

    await language_store.setup(bot=bot, on_language_change=on_language_change)

    return BotRunner(
        bot_prefix=bot_prefix,
        bot=bot,
    )


if __name__ == "__main__":
    import os

    from dotenv import load_dotenv

    logging.basicConfig(level=logging.DEBUG)
    load_dotenv()

    async def main() -> None:
        bot_runner = await create_multilang_bot(
            token=os.environ["TOKEN"],
        )
        await bot_runner.run_polling()

    asyncio.run(main())
