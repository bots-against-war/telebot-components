import html
import logging
from enum import Enum
from pprint import pformat
from typing import Any, cast

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner

from telebot_components.form.field import (
    IntegerField,
    MultipleSelectField,
    NextFieldGetter,
    PlainTextField,
    SingleSelectField,
)
from telebot_components.form.form import Form
from telebot_components.form.handler import (
    FormExitContext,
    FormHandler,
    FormHandlerConfig,
)
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.language import Language, LanguageStore

logging.basicConfig(level=logging.DEBUG)


class LoveableThing(Enum):
    CATS = "Кошки"
    DOGS = "Собаки"
    BIRDS = "Птицы"
    ICECREAM = "Мороженое"
    FRIEDS = "Друзья"


what_you_love_field = MultipleSelectField(
    name="what_you_love",
    required=True,
    query_message="Выберите, что вы любите.",
    echo_result_template="Вы выбрали: {}",
    next_field_getter=NextFieldGetter.form_end(),
    please_use_inline_menu="Пожалуйста, используте меню под сообщением.",
    EnumClass=LoveableThing,
    finish_field_button_caption="⬇️ Завершить",
)


form = Form(
    fields=[
        what_you_love_field,
    ],
    start_field=what_you_love_field,
)


def create_form_bot(redis: RedisInterface, token: str):
    bot = AsyncTeleBot(token)
    bot_prefix = "example-form-bot"

    # language_store = LanguageStore(
    #     redis,
    #     bot_prefix,
    #     supported_languages=[
    #         Language.RU,
    #         Language.EN,
    #     ],
    #     default_language=Language.RU,
    # )

    # @bot.message_handler(commands=["language"])
    # async def select_language_cmd_handler(message: tg.Message):
    #     await bot.send_message(
    #         message.from_user.id,
    #         "?",
    #         reply_markup=(await language_store.markup_for_user(message.from_user)),
    #     )

    form_handler = FormHandler[dict](
        redis=redis,
        bot_prefix=bot_prefix,
        form=form,
        config=FormHandlerConfig(
            echo_filled_field=True,
            retry_field_msg="Пожалуйста, исправьте значение.",
            cancelling_because_of_error_template="Что-то пошло не так: {}",
            form_starting_template="Пожалуйста, заполните небольшую форму! {} - отменить заполнение.",
            can_skip_field_template="{} - пропустить поле.",
            cant_skip_field_msg="Это обязательное поле, его нельзя пропустить!",
            unsupported_cmd_error_template="Неподдерживаемая команда! При заполнении формы доступны следующие команды: {}",
            cancel_cmd="/cancel",
            cancel_aliases=["/stop", "/menu"],
            skip_cmd="/skip",
        ),
        # language_store=language_store,
    )

    @bot.message_handler()
    async def default_handler(message: tg.Message):
        await form_handler.start(bot, message.from_user)

    async def on_form_cancelled(context: FormExitContext):
        # language = await language_store.get_user_language(user)
        # await bot.send_message(
        #     user.id,
        #     {Language.RU: "Заполнение формы отменено. Удачи!", Language.EN: "The form has been cancelled. Good luck!"}[
        #         language
        #     ],
        # )
        await context.bot.send_message(context.last_update.from_user.id, "Отменено!")

    async def on_form_completed(context: FormExitContext):
        form_result_dump = pformat(context.result, indent=2, width=70, sort_dicts=False)
        await bot.send_message(
            context.last_update.from_user.id,
            f"<pre>{html.escape(form_result_dump, quote=False)}</pre>",
            parse_mode="HTML",
        )

    form_handler.setup(bot, on_form_completed=on_form_completed, on_form_cancelled=on_form_cancelled)
    # language_store.setup(bot)

    return BotRunner(
        name="example-form-bot",
        bot=bot,
    )


if __name__ == "__main__":
    import asyncio
    import os

    from redis.asyncio import Redis  # type: ignore

    from telebot_components.redis_utils.emulation import RedisEmulation

    redis_url = os.environ.get("REDIS_URL")
    redis = Redis.from_url(redis_url) if redis_url is not None else RedisEmulation()

    bot_runner = create_form_bot(
        redis=redis,
        token=os.environ["TOKEN"],
    )
    asyncio.run(bot_runner.run_polling())
