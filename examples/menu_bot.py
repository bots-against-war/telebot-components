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
    TerminatorResult,
)
from telebot_components.redis_utils.emulation import RedisEmulation
from telebot_components.stores.category import (
    Category,
    CategorySelectedContext,
    CategoryStore,
)


def create_menu_bot(token: str):
    bot_prefix = "example-menu-bot"
    bot = AsyncTeleBot(token)
    logging.basicConfig(level=logging.INFO)

    good = Category("good")
    bad = Category("bad")
    ugly = Category("ugly")

    async def on_category_selected(context: CategorySelectedContext):
        await bot.send_message(
            context.user.id, f"By the way, according to our records you are: {context.category.name!r}"
        )

    category_store = CategoryStore(
        bot_prefix=bot_prefix,
        redis=RedisEmulation(),
        categories=[good, bad, ugly],
        category_expiration_time=None,
        on_category_selected=on_category_selected,
    )

    menu_tree = Menu(
        text="Hi! What survey do you want to take?",
        config=MenuConfig(
            back_label="<-",
            lock_after_termination=False,
        ),
        menu_items=[
            MenuItem(
                label="Programming language",
                submenu=Menu(
                    "Please choose your programming language",
                    [
                        MenuItem(
                            label="APL",
                            terminator="APL",
                            bound_category=good,
                        ),
                        MenuItem(
                            label="PROLOG",
                            terminator="PROLOG",
                        ),
                        MenuItem(
                            label="Haskell",
                            terminator="Haskell",
                        ),
                        MenuItem(
                            label="C family",
                            submenu=Menu(
                                "Which C family language do you use?",
                                menu_items=[
                                    MenuItem(label="C", terminator="C", bound_category=good),
                                    MenuItem(label="C++", terminator="C++"),
                                    MenuItem(label="C#", terminator="C#"),
                                ],
                            ),
                        ),
                    ],
                ),
            ),
            MenuItem(
                label="Operating system",
                submenu=Menu(
                    "Please choose your operating system",
                    [
                        MenuItem(
                            label="Windows",
                            terminator="windows",
                            bound_category=ugly,
                        ),
                        MenuItem(
                            label="Linux",
                            terminator="linux",
                            bound_category=good,
                        ),
                        MenuItem(
                            label="MacOS",
                            terminator="mac",
                            bound_category=bad,
                        ),
                    ],
                ),
            ),
        ],
    )

    async def on_terminal_menu_option_selected(terminator_context: TerminatorContext) -> Optional[TerminatorResult]:
        await bot.send_message(terminator_context.user.id, f"You have selected: {terminator_context.terminator!r}")
        if terminator_context.terminator == "C":
            return TerminatorResult(
                menu_message_text_update="Segmentation fault (core dumped)",
                lock_menu=True,
            )
        else:
            return None

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

    @bot.message_handler(commands=["whoami"])
    async def whoami_handler(message: tg.Message):
        category = await category_store.get_user_category(message.from_user)
        await bot.send_message(
            message.from_user.id,
            f"You are: {category.name}" if category is not None else "We don't yet know who you are :(",
        )

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
