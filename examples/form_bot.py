import html
import logging
from enum import Enum
from pprint import pformat
from typing import Any, cast

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner
from telebot.types import constants as tgconst

from telebot_components.form.field import (
    AttachmentsField,
    CalendarKeyboardConfig,
    DateMenuField,
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
    next_field_getter=NextFieldGetter.by_name("some-date"),
)


date_field = DateMenuField(
    name="some-date",
    required=True,
    query_message={
        Language.RU: "Выберите дату.",
        Language.EN: "Please select the preferred date.",
    },
    echo_result_template={
        Language.RU: "Выбранная дата: {}.",
        Language.EN: "You shose the following date: {}.",
    },
    next_field_getter=NextFieldGetter.by_name("age"),
    please_use_inline_menu={
        Language.RU: "Пожалуйста, используйте меню.",
        Language.EN: "Please use inline menu.",
    },
    calendar_keyboard_config=CalendarKeyboardConfig(
        prev_month_button="⬅️",
        next_month_button="➡️",
        future_only=False,
    ),
)


def after_age_field(u: tg.User, v: Any) -> str:
    if isinstance(v, int):
        if v < 16:
            return "favorite_subject"
        elif v < 18:
            return "has_finished_school"
        else:
            return "university_program"
    else:
        return "has_finished_school"


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
        Language.RU: "Возраст должен быть указан одним числом.",
        Language.EN: "Age must be specified as a single number.",
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
    BIOLOGY = {
        Language.RU: "Биология",
        Language.EN: "Biology",
    }
    LAW = {
        Language.RU: "Право",
        Language.EN: "Law",
    }
    PE = {
        Language.RU: "Физкультура",
        Language.EN: "Physical Education",
    }
    OTHER = {
        Language.RU: "Другое",
        Language.EN: "Other",
    }


favorite_subject_field = MultipleSelectField(
    name="favorite_subject",
    required=True,
    query_message={
        Language.RU: "Выберите ваши любимые предметы в школе.",
        Language.EN: "Choose your favorite school subjects.",
    },
    echo_result_template=None,
    EnumClass=SchoolSubject,
    please_use_inline_menu={
        Language.RU: "Пожалуйста, используйте меню под сообщением.",
        Language.EN: "Please use inline menu.",
    },
    finish_field_button_caption={
        Language.RU: "Завершить выбор",
        Language.EN: "Finish selection",
    },
    next_page_button_caption={
        Language.RU: "след.",
        Language.EN: "next",
    },
    prev_page_button_caption={
        Language.RU: "пред.",
        Language.EN: "prev",
    },
    inline_menu_row_width=2,
    options_per_page=6,
    next_field_getter=NextFieldGetter.by_name("photos"),
    min_selected_to_finish=2,
    max_selected_to_finish=5,
)


class YesNo(Enum):
    YES = {Language.RU: "Да", Language.EN: "Yes"}
    NO = {Language.RU: "Нет", Language.EN: "No"}


has_finished_school_field = SingleSelectField(
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
    next_field_getter=NextFieldGetter.by_name("photos"),
    empty_text_error_msg={
        Language.RU: "Поле не может быть пустым.",
        Language.EN: "This field cannot be empty",
    },
)


photos_field = AttachmentsField(
    name="photos",
    required=True,
    query_message={Language.RU: "Прикрепите фотографии.", Language.EN: "Please attach photos."},
    echo_result_template=None,
    attachments_expected_error_msg={
        Language.RU: "В этом поле нужно прикрепить фотографии.",
        Language.EN: "This field requires you to attach some photos.",
    },
    only_one_media_message_allowed_error_msg={
        Language.RU: "Можно приложить не более 10 фото.",
        Language.EN: "You can't attach more than 10 photos.",
    },
    bad_attachment_type_error_msg={
        Language.RU: "Пожалуйста, прикрепляйте только фотографии с компрессией, не в в виде документов.",
        Language.EN: "Please attach only photos with compression, not as documents.",
    },
    allowed_attachment_types={tgconst.MediaContentType.photo},
    next_field_getter=NextFieldGetter.form_end(),
)


form = Form(
    fields=[
        name_field,
        date_field,
        age_field,
        favorite_subject_field,
        has_finished_school_field,
        university_program_field,
        photos_field,
    ],
    start_field=name_field,
)


def create_form_bot(redis: RedisInterface, token: str):
    bot = AsyncTeleBot(token)
    bot_prefix = "example-form-bot"

    language_store = LanguageStore(
        redis, bot_prefix, supported_languages=[Language.RU, Language.EN], default_language=Language.RU
    )

    @bot.message_handler(commands=["language"])
    async def select_language_cmd_handler(message: tg.Message):
        await bot.send_message(
            message.from_user.id,
            "?",
            reply_markup=(await language_store.markup_for_user(message.from_user)),
        )

    form_handler = FormHandler[dict](
        redis=redis,
        bot_prefix=bot_prefix,
        name="example",
        form=form,
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
            keep_existing_field_value_template={
                Language.RU: "Текущее значение: {}; {} - оставить его.",
                Language.EN: "Current value: {}; use {} to keep it.",
            },
            keep_cmd="/keep",
        ),
        language_store=language_store,
    )

    @bot.message_handler()
    async def default_handler(message: tg.Message):
        await form_handler.start(
            bot,
            message.from_user,
            # these are used as "current values" or "defaults" and can be kept with /keep cmd
            initial_form_result={
                "university_program": "Mathematics",
                "has_finished_school": YesNo.YES,
                "favorite_subject": {SchoolSubject.SCIENCE, SchoolSubject.PE},
            },
        )

    async def on_form_cancelled(context: FormExitContext[dict]):
        user = context.last_update.from_user
        language = await language_store.get_user_language(user)
        await bot.send_message(
            user.id,
            {Language.RU: "Заполнение формы отменено. Удачи!", Language.EN: "The form has been cancelled. Good luck!"}[
                language
            ],
        )

    async def on_form_completed(context: FormExitContext[dict]):
        result_to_dump = context.result.copy()
        result_to_dump.pop("photos")
        form_result_dump = pformat(result_to_dump, indent=2, width=70, sort_dicts=False)
        await bot.send_message(
            context.last_update.from_user.id,
            f"<pre>{html.escape(form_result_dump, quote=False)}</pre>",
            parse_mode="HTML",
        )
        photos: list[list[tg.PhotoSize]] = context.result["photos"]
        for photo in photos:
            await bot.send_photo(
                context.last_update.from_user.id, photo=photo[0].file_id, caption=photo[0].file_unique_id
            )

    form_handler.setup(bot, on_form_completed=on_form_completed, on_form_cancelled=on_form_cancelled)
    language_store.setup(bot)

    return BotRunner(
        bot_prefix="example-form-bot",
        bot=bot,
    )


if __name__ == "__main__":
    import asyncio
    import os

    from dotenv import load_dotenv
    from redis.asyncio import Redis  # type: ignore

    from telebot_components.redis_utils.emulation import RedisEmulation

    load_dotenv()

    redis_url = os.environ.get("REDIS_URL")
    redis = Redis.from_url(redis_url) if redis_url is not None else RedisEmulation()

    bot_runner = create_form_bot(
        redis=redis,
        token=os.environ["TOKEN"],
    )

    async def main():
        print(await bot_runner.bot.get_me())
        await bot_runner.run_polling()

    asyncio.run(main())
