import logging


from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner
from telebot_components.menu.menu import MenuHandler, Menu, MenuItem

FIRST_TERMINATOR = "first"
SECOND_TERMINATOR = "second"
THIRD_TERMINATOR = "third"


def create_menu_bot(token: str):
    bot_prefix = "example-menu-bot"
    bot = AsyncTeleBot(token)
    logging.basicConfig(level=logging.DEBUG)

    menu_tree = Menu(
        "Main menu:",
        [
            MenuItem(
                label="option 1",
                submenu=Menu(
                    "Submenu 1:",
                    [
                        MenuItem(
                            label="option 1",
                            terminator=FIRST_TERMINATOR,
                        ),
                        MenuItem(
                            label="option 2",
                            terminator=FIRST_TERMINATOR,
                        ),
                        MenuItem(
                            label="option 3",
                            terminator=FIRST_TERMINATOR,
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
                            label="option 1",
                            terminator=SECOND_TERMINATOR,
                        ),
                        MenuItem(
                            label="option 2",
                            terminator=SECOND_TERMINATOR,
                        ),
                        MenuItem(
                            label="option 3",
                            terminator=THIRD_TERMINATOR,
                        ),
                    ],
                ),
            ),
        ],
    )

    async def on_terminal_menu_option_selected(
        bot: AsyncTeleBot, user: tg.User, menu_message: tg.Message, terminator: str
    ):
        if terminator == FIRST_TERMINATOR:
            await bot.send_message(
                user.id,
                "do what you need to do with this terminator " + FIRST_TERMINATOR,
            )
        elif terminator == SECOND_TERMINATOR:
            await bot.send_message(
                user.id,
                "do what you need to do with this terminator " + SECOND_TERMINATOR,
            )
        elif terminator == THIRD_TERMINATOR:
            await bot.send_message(
                user.id,
                "do what you need to do with this terminator " + THIRD_TERMINATOR,
            )

    menu_handler = MenuHandler(bot_prefix, menu_tree)
    menu_handler.setup(bot, on_terminal_menu_option_selected)

    @bot.message_handler(commands=["start", "help"])
    async def start_cmd_handler(message: tg.Message):
        main_menu = menu_handler.get_main_menu()
        await bot.send_message(
            message.from_user.id,
            main_menu.text,
            reply_markup=(main_menu.get_keyboard_markup()),
        )

    return BotRunner(
        name=bot_prefix,
        bot=bot,
    )


if __name__ == "__main__":
    import asyncio
    import os

    bot_runner = create_menu_bot(
        token=os.environ["TOKEN"],
    )

    asyncio.run(bot_runner.run_polling())
