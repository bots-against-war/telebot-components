import html
import json
from enum import Enum
import logging
from pprint import pformat, pprint
from typing import Type

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner

from telebot_components.form.field import (
    EnumField,
    IntegerField,
    NextFieldGetter,
    PlainTextField,
)
from telebot_components.form.form import Form
from telebot_components.form.handler import FormHandler, FormHandlerConfig
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.language import (
    Language,
    LanguageStore,
)


logging.basicConfig(level=logging.INFO)


name_field = PlainTextField(
    name="name",
    required=True,
    query_message={
        Language.RU: "Введите ваше имя.",
        Language.EN: "Enter your name.",
    },
    echo_result_template=None,  # no result echoing
    empty_text_error_msg={
        Language.RU: "Имя не может быть пустым.",
        Language.EN: "The name cannot be empty",
    },
    next_field_getter=NextFieldGetter.by_name("age"),
)


def after_age_field(u: tg.User, v: int) -> str:
    if v < 16:
        return "favorite_subject"
    elif v < 18:
        return "has_finished_school"
    else:
        return "university_program"


age_field = IntegerField(
    name="age",
    required=False,
    query_message={
        Language.RU: "Введите ваш возраст.",
        Language.EN: "Enter your age.",
    },
    echo_result_template={
        Language.RU: "Ваш возраст: {}.",
        Language.EN: "You age: {}.",
    },
    not_an_integer_error_msg={
        Language.RU: "Пожалуйста, введите ваш возраст одним числом.",
        Language.EN: "Please enter your age as a single number.",
    },
    next_field_getter=NextFieldGetter(
        next_field_name_getter=after_age_field,
        possible_next_field_names=["favorite_subject", "has_finished_school", "university_program"],
    ),
)


class SchoolSubject(Enum):
    MATH = {
        Language.RU: "Математика",
        Language.EN: "Math",
    }
    SCIENCE = {
        Language.RU: "Физика / химия",
        Language.EN: "Science",
    }
    LANGUAGE = {
        Language.RU: "Языки",
        Language.EN: "Language",
    }
    OTHER = {
        Language.RU: "Другое",
        Language.EN: "Other",
    }


favorite_subject_field = EnumField(
    name="favorite_subject",
    required=True,
    query_message={
        Language.RU: "Выберите ваш любимый предмет в школе.",
        Language.EN: "Choose your favorite school subject.",
    },
    echo_result_template=None,
    EnumClass=SchoolSubject,
    invalid_enum_value_error_msg={
        Language.RU: "Пожалуйста, используйте меню.",
        Language.EN: "Please use menu.",
    },
    next_field_getter=NextFieldGetter.form_end(),
)


class YesNo(Enum):
    YES = {Language.RU: "Да", Language.EN: "Yes"}
    NO = {Language.RU: "Нет", Language.EN: "No"}


has_finished_school_field = EnumField(
    name="has_finished_school",
    required=True,
    query_message={
        Language.RU: "Закончили ли вы школу?",
        Language.EN: "Have you finished school yet?",
    },
    echo_result_template=None,
    EnumClass=YesNo,
    invalid_enum_value_error_msg={
        Language.RU: "Пожалуйста, используйте меню.",
        Language.EN: "Please use menu.",
    },
    next_field_getter=NextFieldGetter.by_mapping({YesNo.NO: "favorite_subject"}, default="university_program"),
)


university_program_field = PlainTextField(
    name="university_program",
    required=True,
    query_message={
        Language.RU: "Введите название факультета и учебной программы.",
        Language.EN: "Enter your faculty and major.",
    },
    echo_result_template=None,
    next_field_getter=NextFieldGetter.form_end(),
    empty_text_error_msg={
        Language.RU: "Поле не может быть пустым.",
        Language.EN: "This field cannot be empty",
    },
)


form = Form(
    fields=[
        name_field,
        age_field,
        favorite_subject_field,
        has_finished_school_field,
        university_program_field,
    ],
    start_field=name_field,
)


form.print_graph()
# ┌─────────────────────┐
# │        name         │
# └─────────────────────┘
#            |
#            V
# ┌─────────────────────┐
# │         age         │
# │                     │─────┐
# │                     │──┐  |
# └─────────────────────┘  |  |
#            |             |  |
#            V             |  |
# ┌─────────────────────┐  |  |
# │ has_finished_school │  |  |
# │                     │──|─┐|
# └─────────────────────┘  | ||
#            |             | ||
#            V             | ||
# ┌─────────────────────┐  | ||
# │                     │<─┘ ||
# │ university_program  │    ||
# │                     │───┐||
# └─────────────────────┘   |||
#                           |||
#                           |||
# ┌─────────────────────┐   |||
# │                     │<──|┘|
# │                     │<──|─┘
# │  favorite_subject   │   |
# └─────────────────────┘   |
#            |              |
#            V              |
# ┌─────────────────────┐   |
# │                     │<──┘
# │         END         │
# └─────────────────────┘


def create_form_bot(BotClass: Type[AsyncTeleBot], redis: RedisInterface, token: str):
    bot = BotClass(token)
    bot_prefix = "example-form-bot"

    language_store = LanguageStore(
        bot_prefix, redis, supported_languages=[Language.RU, Language.EN], default_language=Language.RU
    )

    @bot.message_handler(commands=["language"])
    async def select_language_cmd_handler(message: tg.Message):
        await bot.send_message(
            message.from_user.id,
            "?",
            reply_markup=(await language_store.markup_for_user(message.from_user)),
        )

    form_handler = FormHandler(
        form,
        config=FormHandlerConfig(
            echo_filled_field=True,
            retry_field_msg={
                Language.RU: "Пожалуйста, исправьте значение.",
                Language.EN: "Please enter valid value.",
            },
            cancelling_because_of_error_template={
                Language.RU: "Что-то пошло не так: {}",
                Language.EN: "Something went wrong: {}",
            },
            form_starting_template={
                Language.RU: "Пожалуйста, заполните небольшую форму! {} - отменить заполнение.",
                Language.EN: "Please fill out a simple form! {} to cancel.",
            },
            can_skip_field_template={
                Language.RU: "{} - пропустить поле.",
                Language.EN: "{} to skip.",
            },
            cant_skip_field_msg={
                Language.RU: "Это обязательное поле, его нельзя пропустить!",
                Language.EN: "This is a required field that can't be skipped!",
            },
            unsupported_cmd_error_template={
                Language.RU: "Неподдерживаемая команда! При заполнении формы доступны следующие команды: {}",
                Language.EN: "The command is not supported! When filling out the form the available commands are: {}",
            },
            cancel_cmd="/cancel",
            cancel_aliases=["/stop", "/menu"],
            skip_cmd="/skip",
        ),
        language_store=language_store,
    )

    @bot.message_handler()
    async def default_handler(message: tg.Message):
        await form_handler.start(bot, message.from_user)

    async def on_form_cancelled(bot: AsyncTeleBot, last_message: tg.Message, user: tg.User, result: dict):
        language = await language_store.get_user_language(user)
        await bot.send_message(
            user.id,
            {Language.RU: "Заполнение формы отменено. Удачи!", Language.EN: "The form has been cancelled. Good luck!"}[
                language
            ],
        )

    async def on_form_completed(bot: AsyncTeleBot, last_message: tg.Message, user: tg.User, result: dict):
        form_result_dump = pformat(result, indent=2, width=70, sort_dicts=False)
        await bot.send_message(
            user.id,
            f"<pre>{html.escape(form_result_dump, quote=False)}</pre>",
            parse_mode="HTML",
        )

    form_handler.setup(bot, on_form_completed=on_form_completed, on_form_cancelled=on_form_cancelled)
    language_store.setup(bot)

    return BotRunner(
        name="example-form-bot",
        bot=bot,
    )


if __name__ == "__main__":
    import os

    from telebot_components.redis_utils.emulation import RedisEmulation

    bot_runner = create_form_bot(
        AsyncTeleBot,
        redis=RedisEmulation(),
        token=os.environ["TOKEN"],
    )
    bot_runner.run_polling()
