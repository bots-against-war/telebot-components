import asyncio
import logging
import os

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner

from telebot_components.form.field import (
    FormFieldResultFormattingOpts,
    ListInputField,
)
from telebot_components.form.form import Form
from telebot_components.form.handler import (
    FormExitContext,
    FormHandler,
    FormHandlerConfig,
)
from telebot_components.redis_utils.emulation import RedisEmulation

form = Form(
    [
        ListInputField(
            name="food",
            required=True,
            query_message="Enter your favorite foods.",
            finish_field_button_caption="Finish selection",
            next_page_button_caption="=>",
            prev_page_button_caption="<=",
            min_len=3,
            max_len=9,
            items_per_page=5,
            max_len_reached_error_msg="You can't add more than 9 foods!",
            result_formatting_opts=FormFieldResultFormattingOpts(
                descr="Favorite food",
                is_multiline=True,
            ),
        ),
    ]
)


async def create_form_demo_bot():
    bot_prefix = "form-bot-food"
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
            cancelling_because_of_error_template="Error: {}",
            form_starting_template="Please fill out the form.",
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
            ctx.last_update.from_user.id,
            form.result_to_html(ctx.result, None),
            parse_mode="HTML",
        )

    form_handler.setup(bot, on_form_completed=on_form_completed)

    return BotRunner(bot_prefix=bot_prefix, bot=bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    async def main() -> None:
        br = await create_form_demo_bot()
        print(await br.bot.get_me())
        await br.run_polling()

    asyncio.run(main())
