import logging


from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner
from telebot_components.menu.menu import MenuHandler, Menu, MenuItem, Terminators


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
                            terminator=Terminators.Agitation,
                        ),
                        MenuItem(
                            label="option 2",
                            terminator=Terminators.Letter,
                        ),
                        MenuItem(
                            label="option 3",
                            terminator=Terminators.Strike,
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
                            terminator=Terminators.Have_initiative,
                        ),
                        MenuItem(
                            label="option 2",
                            terminator=Terminators.Search_initiative,
                        ),
                        MenuItem(
                            label="option 3",
                            terminator=Terminators.Read_info,
                        ),
                    ],
                ),
            ),
        ],
    )

    menu_handler = MenuHandler(bot_prefix, menu_tree)
    menu_handler.setup(bot)

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
