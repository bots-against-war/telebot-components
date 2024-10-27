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
    MenuMechanism,
    TerminatorContext,
    TerminatorResult,
)
from telebot_components.redis_utils.emulation import RedisEmulation
from telebot_components.stores.category import (
    Category,
    CategorySelectedContext,
    CategoryStore,
)
from telebot_components.stores.language import (
    Language,
    LanguageSelectionMenuConfig,
    LanguageStore,
)


def create_menu_bot(token: str):
    bot_prefix = "example-menu-bot"
    bot = AsyncTeleBot(token)
    logging.basicConfig(level=logging.DEBUG)
    redis = RedisEmulation()

    good = Category("good")
    bad = Category("bad")
    ugly = Category("ugly")

    async def on_category_selected(context: CategorySelectedContext):
        await bot.send_message(
            context.user.id, f"By the way, according to our records you are: {context.category.name!r}"
        )

    category_store = CategoryStore(
        bot_prefix=bot_prefix,
        redis=redis,
        categories=[good, bad, ugly],
        category_expiration_time=None,
        on_category_selected=on_category_selected,
    )

    menu_tree = Menu(
        text={
            Language.RU: "<i>Привет!</i> Какой опрос ты хочешь пройти?",
            Language.EN: "<i>Hi!</i> What survey do you want to take?",
        },
        config=MenuConfig(
            back_label={
                Language.RU: "Назад",
                Language.EN: "🔙 Back",
            },
            lock_after_termination=False,
            is_text_html=True,
        ),
        menu_items=[
            MenuItem(
                label={
                    Language.RU: "Язык программирования",
                    Language.EN: "Programming language",
                },
                submenu=Menu(
                    text={
                        Language.RU: "Пожалуйста, выберите ваш язык программирования",
                        Language.EN: "Please choose your programming language",
                    },
                    config=MenuConfig(
                        back_label={Language.RU: "<-", Language.EN: "<-"},
                        lock_after_termination=False,
                        mechanism=MenuMechanism.REPLY_KEYBOARD,
                    ),
                    menu_items=[
                        MenuItem(
                            label={
                                Language.RU: "APL",
                                Language.EN: "APL",
                            },
                            terminator="APL",
                            bound_category=good,
                        ),
                        MenuItem(
                            label={
                                Language.RU: "PROLOG",
                                Language.EN: "PROLOG",
                            },
                            terminator="PROLOG",
                        ),
                        MenuItem(
                            label={
                                Language.RU: "Haskell",
                                Language.EN: "Haskell",
                            },
                            terminator="Haskell",
                        ),
                        MenuItem(
                            label={
                                Language.RU: "C семейство",
                                Language.EN: "C family",
                            },
                            submenu=Menu(
                                {
                                    Language.RU: "Какой язык семейства C вы используете?",
                                    Language.EN: "Which C family language do you use?",
                                },
                                config=MenuConfig(back_label=None, lock_after_termination=False),
                                menu_items=[
                                    MenuItem(
                                        label={
                                            Language.RU: "Си",
                                            Language.EN: "C",
                                        },
                                        terminator="C",
                                        bound_category=good,
                                    ),
                                    MenuItem(
                                        label={
                                            Language.RU: "Си++",
                                            Language.EN: "C++",
                                        },
                                        terminator="C++",
                                    ),
                                    MenuItem(
                                        label={
                                            Language.RU: "Си шарп",
                                            Language.EN: "C#",
                                        },
                                        terminator="C#",
                                    ),
                                ],
                            ),
                        ),
                    ],
                ),
            ),
            MenuItem(
                label={
                    Language.RU: "Операционная система",
                    Language.EN: "Operating system",
                },
                submenu=Menu(
                    {
                        Language.RU: "Пожалуйста, выберите вашу операционную систему",
                        Language.EN: "Please choose your operating system",
                    },
                    [
                        MenuItem(
                            label={
                                Language.RU: "Виндоуз",
                                Language.EN: "Windows",
                            },
                            terminator="windows",
                            bound_category=ugly,
                        ),
                        MenuItem(
                            label={
                                Language.RU: "Линукс",
                                Language.EN: "Linux",
                            },
                            terminator="linux",
                            bound_category=good,
                        ),
                        MenuItem(
                            label={
                                Language.RU: "МакОС",
                                Language.EN: "MacOS",
                            },
                            terminator="mac",
                            bound_category=bad,
                        ),
                    ],
                ),
            ),
        ],
    )

    language_store = LanguageStore(
        redis=redis,
        bot_prefix=bot_prefix,
        supported_languages=[Language.RU, Language.EN],
        default_language=Language.RU,
        menu_config=LanguageSelectionMenuConfig(emojj_buttons=True, select_with_checkmark=True),
    )

    async def on_terminal_menu_option_selected(terminator_context: TerminatorContext) -> Optional[TerminatorResult]:
        await bot.send_message(terminator_context.user.id, f"You have selected: {terminator_context.terminator!r}")
        if terminator_context.terminator == "C":
            return TerminatorResult(
                menu_message_text_update={
                    Language.RU: "Сегментация памяти (ядро сброшено)",
                    Language.EN: "Segmentation fault (core dumped)",
                },
                lock_menu=True,
            )
        else:
            return None

    menu_handler = MenuHandler(
        bot_prefix=bot_prefix,
        name="example-menu",
        menu_tree=menu_tree,
        redis=redis,
        category_store=category_store,
        language_store=language_store,
    )
    menu_handler.setup(bot, on_terminal_menu_option_selected)

    @bot.message_handler(commands=["start", "help"])
    async def start_cmd_handler(message: tg.Message):
        await menu_handler.start_menu(bot, message.from_user)

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


async def main(br: BotRunner):
    print(await br.bot.get_me())
    print()
    await br.run_polling()


if __name__ == "__main__":
    import asyncio
    import os

    from dotenv import load_dotenv

    load_dotenv()

    bot_runner = create_menu_bot(
        token=os.environ["TOKEN"],
    )

    asyncio.run(main(bot_runner))
