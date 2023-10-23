import asyncio
import os
from enum import Enum

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner

from telebot_components.form.field import (
    DateMenuField,
    FormFieldResultFormattingOpts,
    IntegerField,
    PlainTextField,
    SingleSelectField,
)
from telebot_components.form.form import FormBranch, Form
from telebot_components.form.handler import (
    FormExitContext,
    FormHandler,
    FormHandlerConfig,
)
from telebot_components.form.helpers.calendar_keyboard import (
    CalendarKeyboardConfig,
    SelectableDates,
)
from telebot_components.redis_utils.emulation import RedisEmulation


class HaveAPet(Enum):
    HAVE = "Yes"
    DONT_HAVE = "No"
    SOON_GETTING = "No, but plan to get soon"


have_a_pet_field = SingleSelectField(
    EnumClass=HaveAPet,
    name="have_a_pet",
    required=True,
    query_message="Do you have a pet?",
    invalid_enum_value_error_msg="Please use reply keyboard to select one of the options.",
    result_formatting_opts=True,
)


pet_name = PlainTextField(
    name="pet_name",
    required=True,
    query_message="What's your pet's name?",
    empty_text_error_msg="Please enter some text",
    result_formatting_opts=FormFieldResultFormattingOpts(descr="Pet name"),
)


pet_adoption_date = DateMenuField(
    name="pet_adoption_date",
    required=True,
    query_message="When are you planning to get a pet?",
    please_use_inline_menu="Please use the calendar menu under the message to select a date.",
    calendar_keyboard_config=CalendarKeyboardConfig(selectable_dates=SelectableDates.FUTURE),
    result_formatting_opts=FormFieldResultFormattingOpts(descr="Pet adoption date"),
)

age_field = IntegerField(
    name="age",
    required=True,
    query_message="What's you age?",
    not_an_integer_error_msg="Please answer with a single number!",
)


form = Form.branching(
    [
        have_a_pet_field,
        FormBranch([pet_name], condition=HaveAPet.HAVE.name),
        FormBranch([pet_adoption_date], condition=HaveAPet.SOON_GETTING.name),
        age_field,
    ],
)

form.print_graph()


async def create_form_bot_2():
    bot_prefix = "form-bot-2"
    redis = RedisEmulation()
    form_handler = FormHandler(
        redis=redis,
        bot_prefix=bot_prefix,
        name="main",
        form=form,
        config=FormHandlerConfig(
            echo_filled_field=False,
            retry_field_msg="Please correct the value.",
            unsupported_cmd_error_template="Unsupported cmd, supported are: {}",
            cancelling_because_of_error_template="AAA Error: {}",
            form_starting_template="Fill the form.",
            can_skip_field_template="Skip with {}",
            cant_skip_field_msg="This is a mandatory field.",
        ),
    )

    bot = AsyncTeleBot(os.environ["TOKEN"])

    @bot.message_handler(commands=["start"])
    async def start_form(message: tg.Message):
        await form_handler.start(bot, message.from_user)

    async def on_form_completed(ctx: FormExitContext):
        await ctx.bot.send_message(
            ctx.last_update.from_user.id, form.result_to_html(ctx.result, None), parse_mode="HTML"
        )

    form_handler.setup(bot, on_form_completed=on_form_completed)

    return BotRunner(bot_prefix=bot_prefix, bot=bot)


if __name__ == "__main__":

    async def main() -> None:
        br = await create_form_bot_2()
        await br.run_polling()

    asyncio.run(main())
