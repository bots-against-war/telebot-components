import logging

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner
from telebot_components.menu.menu import MenuHandler, Menu, MenuItem
from telebot_components.redis_utils.interface import RedisInterface


def create_menu_bot(redis: RedisInterface, token: str):
    bot_prefix = "example-feedback-bot"
    bot = AsyncTeleBot(token)
    logging.basicConfig(level=logging.DEBUG)

    menu_tree = Menu(
        "main_menu",
        "✊ Главное меню ✊\n\nВыберите тип вашего запроса или сообщения:",
        [
            MenuItem(
                "прислать отчет об акции или открытом письме",
                Menu(
                    "report_menu",
                    "Выберите тип вашего отчета или акции:",
                    [
                        MenuItem("расклейка/агитация"),
                        MenuItem("открытое письмо против войны"),
                        MenuItem("образовательная забастовка 'книги вместо бомб'"),
                        MenuItem("акция 'ректор, отзови подпись'"),
                        MenuItem("другое/творчество"),
                    ],
                ),
            ),
            MenuItem(
                "присоединиться к антивоенному сопротивлению",
                Menu(
                    "join_menu",
                    "Найти единомышлен_ниц внутри вуза и создать инициативную группу:",
                    [
                        MenuItem("Мы уже создали инициативную группу в своем вузе"),
                        MenuItem("Я ищу единомышлен_ниц в своем вузе"),
                        MenuItem("Читать наши материалы по самоорганизации"),
                    ],
                ),
            ),
        ],
    )

    menu_handler = MenuHandler(bot_prefix, menu_tree)
    menu_handler.setup(bot)

    @bot.message_handler(commands=["start", "help"])
    async def start_cmd_handler(message: tg.Message):
        main_menu = menu_handler.get_menu_by_name("main_menu")
        await bot.send_message(
            message.from_user.id,
            main_menu.text,
            reply_markup=(main_menu.get_keyboard_markup()),
            parse_mode="Markdown",
        )

    return BotRunner(
        name=bot_prefix,
        bot=bot,
    )


if __name__ == "__main__":
    import asyncio
    import os

    from redis.asyncio import Redis  # type: ignore

    from telebot_components.redis_utils.emulation import RedisEmulation

    redis = RedisEmulation()
    # redis = Redis.from_url(os.environ["REDIS_URL"])
    bot_runner = create_menu_bot(
        redis=redis,
        token=os.environ["TOKEN"],
    )

    asyncio.run(bot_runner.run_polling())
