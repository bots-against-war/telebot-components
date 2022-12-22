import logging
from typing import Optional

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner

from telebot_components.menu.menu import (
    Menu,
    MenuConfig,
    MenuHandler,
    MenuItem,
    TerminatorContext,
)
from telebot_components.redis_utils.emulation import RedisEmulation
from telebot_components.stores.category import (
    Category,
    CategorySelectedContext,
    CategoryStore,
)

FIRST_TERMINATOR = "first"
SECOND_TERMINATOR = "second"
THIRD_TERMINATOR = "third"


def create_menu_bot(token: str):
    bot_prefix = "example-menu-bot"
    bot = AsyncTeleBot(token)
    logging.basicConfig(level=logging.INFO)

    red = Category("red")
    green = Category("green")
    blue = Category("blue")

    async def on_category_selected(context: CategorySelectedContext):
        await bot.send_message(
            context.user.id, f"by the way, you have now selected the category: {context.category.name!r}"
        )

    category_store = CategoryStore(
        bot_prefix=bot_prefix,
        redis=RedisEmulation(),
        categories=[red, green, blue],
        category_expiration_time=None,
        on_category_selected=on_category_selected,
    )

    menu_tree = Menu(
        text="Main menu:",
        config=MenuConfig(
            back_label="<-",
            lock_after_termination=False,
        ),
        menu_items=[
            MenuItem(
                label="option 1",
                submenu=Menu(
                    "Submenu 1:",
                    [
                        MenuItem(
                            label="suboption 1.1",
                            terminator=FIRST_TERMINATOR,
                        ),
                        MenuItem(
                            label="suboption 1.2 (❤)",
                            terminator=SECOND_TERMINATOR,
                            bound_category=red,
                        ),
                        MenuItem(
                            label="suboption 1.3",
                            terminator=THIRD_TERMINATOR,
                        ),
                    ],
                ),
            ),
            MenuItem(
                label="option 2",
                submenu=Menu(
                    "Submenu 2:",
                    [
                        MenuItem(
                            label="suboption 2.1 (💙)",
                            terminator=FIRST_TERMINATOR,
                            bound_category=blue,
                        ),
                        MenuItem(
                            label="suboption 2.2",
                            terminator=SECOND_TERMINATOR,
                        ),
                        MenuItem(
                            label="suboption 2.3 (💚)",
                            terminator=THIRD_TERMINATOR,
                            bound_category=green,
                        ),
                    ],
                ),
            ),
        ],
    )

    async def on_terminal_menu_option_selected(terminator_context: TerminatorContext) -> None:
        if terminator_context.terminator == FIRST_TERMINATOR:
            await bot.send_message(
                terminator_context.user.id,
                "do what you need to do with this terminator " + FIRST_TERMINATOR,
            )
        elif terminator_context.terminator == SECOND_TERMINATOR:
            await bot.send_message(
                terminator_context.user.id,
                "do what you need to do with this terminator " + SECOND_TERMINATOR,
            )
        elif terminator_context.terminator == THIRD_TERMINATOR:
            await bot.send_message(
                terminator_context.user.id,
                "do what you need to do with this terminator " + THIRD_TERMINATOR,
            )

    menu_handler = MenuHandler(bot_prefix, menu_tree, category_store=category_store)
    menu_handler.setup(bot, on_terminal_menu_option_selected)

    @bot.message_handler(commands=["start", "help"])
    async def start_cmd_handler(message: tg.Message):
        main_menu = menu_handler.get_main_menu()
        await bot.send_message(
            message.from_user.id,
            main_menu.text,
            reply_markup=(main_menu.get_keyboard_markup()),
        )

    category_store.setup(bot)

    return BotRunner(
        bot_prefix=bot_prefix,
        bot=bot,
    )


if __name__ == "__main__":
    import asyncio
    import os

    from dotenv import load_dotenv

    load_dotenv()

    bot_runner = create_menu_bot(
        token=os.environ["TOKEN"],
    )

    asyncio.run(bot_runner.run_polling())
