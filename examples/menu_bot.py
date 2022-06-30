import logging


from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner
from telebot_components.menu.menu import MenuHandler, Menu, MenuItem, Terminators


def create_menu_bot(token: str):
    bot_prefix = "example-feedback-bot"
    bot = AsyncTeleBot(token)
    logging.basicConfig(level=logging.DEBUG)

    menu_tree = Menu(
        "✊ Главное меню ✊\n\nВыберите тип вашего запроса или сообщения:",
        [
            MenuItem(
                label="прислать отчет об акции или открытом письме",
                submenu=Menu(
                    "Выберите тип вашего отчета или акции:",
                    [
                        MenuItem(
                            label="расклейка/агитация",
                            terminator=Terminators.Agitation,
                        ),
                        MenuItem(
                            label="открытое письмо против войны",
                            terminator=Terminators.Letter,
                        ),
                        MenuItem(
                            label="образовательная забастовка 'книги вместо бомб'",
                            terminator=Terminators.Strike,
                        ),
                    ],
                ),
            ),
            MenuItem(
                label="прислать отчет об акции или открытом письме",
                submenu=Menu(
                    "Выберите тип вашего отчета или акции:",
                    [
                        MenuItem(
                            label="расклейка/агитация",
                            terminator=Terminators.Agitation,
                        ),
                        MenuItem(
                            label="открытое письмо против войны",
                            terminator=Terminators.Letter,
                        ),
                        MenuItem(
                            label="образовательная забастовка 'книги вместо бомб'",
                            terminator=Terminators.Strike,
                        ),
                    ],
                ),
            ),
            MenuItem(
                label="присоединиться к антивоенному сопротивлению",
                submenu=Menu(
                    "Найти единомышлен_ниц внутри вуза и создать инициативную группу:",
                    [
                        MenuItem(
                            label="Мы уже создали инициативную группу в своем вузе",
                            terminator=Terminators.Have_initiative,
                        ),
                        MenuItem(
                            label="Я ищу единомышлен_ниц в своем вузе",
                            terminator=Terminators.Search_initiative,
                        ),
                        MenuItem(
                            label="Читать наши материалы по самоорганизации",
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
