from typing import List

import pytest
from telebot import types as tg
from telebot.test_util import MockedAsyncTeleBot

from telebot_components.menu.menu import (
    Menu,
    MenuConfig,
    MenuHandler,
    MenuItem,
    MenuMechanism,
    TerminatorContext,
)
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.language import Language, LanguageStore
from telebot_components.utils import TextMarkup
from tests.utils import TelegramServerMock, extract_full_kwargs, reply_markups_to_dict


@pytest.mark.parametrize(
    "submenu, terminator, link_url",
    [
        pytest.param(None, "hello", "https://google.com"),
        pytest.param(Menu("", []), "hello", None),
        pytest.param(Menu("", []), None, "https://github.com"),
        pytest.param(Menu("", []), "hello", "https://github.com"),
    ],
)
def test_menu_item_validation(submenu, terminator, link_url):
    with pytest.raises(
        ValueError,
        match="Exactly one of the arguments must be set to non-None value: submenu, terminator, or link_url",
    ):
        MenuItem(label="", submenu=submenu, terminator=terminator, link_url=link_url)


@pytest.fixture
def example_menu() -> Menu:
    return Menu(
        text="example menu =<^_^>=",
        menu_items=[
            MenuItem(
                label="picking game",
                submenu=Menu(
                    text="<b>what do you want to pick?</b>",
                    config=MenuConfig(is_text_html=True, back_label="<-", lock_after_termination=False),
                    menu_items=[
                        MenuItem(
                            label="color",
                            submenu=Menu(
                                text="pick color",
                                menu_items=[
                                    MenuItem(label="red", terminator="red"),
                                    MenuItem(label="green", terminator="green"),
                                    MenuItem(label="blue", terminator="blue"),
                                ],
                                config=MenuConfig(
                                    back_label="back",
                                    lock_after_termination=True,
                                ),
                            ),
                        ),
                        MenuItem(
                            label="animal",
                            terminator="pick_animal",
                        ),
                        MenuItem(label="sound", link_url="https://cool-sound-picking-game.net"),
                    ],
                ),
            ),
            MenuItem(
                label="feedback",
                terminator="send_feedback",
            ),
        ],
        config=MenuConfig(
            back_label="<-",
            lock_after_termination=False,
        ),
    )


@pytest.mark.parametrize(
    "legacy_id_in_buttons",
    [
        True,
        False,
    ],
)
async def test_menu_handler_basic(example_menu: Menu, legacy_id_in_buttons: bool, redis: RedisInterface):
    bot = MockedAsyncTeleBot(token="")
    menu_handler = MenuHandler(
        bot_prefix="testing",
        menu_tree=example_menu,
        name="test-menu-basic",
        redis=redis,
    )
    telegram = TelegramServerMock()

    user = tg.User(id=1312, is_bot=False, first_name="Max", last_name="Slater")
    seen_terminators: List[str] = []
    terminator_context_check_failed = False

    async def on_menu_termination(context: TerminatorContext) -> None:
        seen_terminators.append(context.terminator)
        nonlocal terminator_context_check_failed
        if not (context.bot is bot and context.user.id == user.id):
            terminator_context_check_failed = True

    menu_handler.setup(bot, on_terminal_menu_option_selected=on_menu_termination)

    main_menu = menu_handler.get_main_menu()
    assert main_menu.text == "example menu =<^_^>="
    assert main_menu.get_keyboard_markup(None).to_dict() == {
        "inline_keyboard": [
            [{"text": "picking game", "callback_data": "menu:55a8b659b3cd3593-1"}],
            [{"text": "feedback", "callback_data": "terminator:55a8b659b3cd3593-1"}],
        ]
    }

    await menu_handler.start_menu(bot, user)
    assert len(bot.method_calls) == 1
    assert extract_full_kwargs(bot.method_calls.pop("send_message")) == [
        {
            "chat_id": 1312,
            "text": "example menu =<^_^>=",
            "reply_markup": tg.InlineKeyboardMarkup(
                [
                    [tg.InlineKeyboardButton(text="picking game", callback_data="menu:55a8b659b3cd3593-1")],
                    [tg.InlineKeyboardButton(text="feedback", callback_data="terminator:55a8b659b3cd3593-1")],
                ]
            ),
            "parse_mode": None,
        }
    ]
    bot.method_calls.clear()

    # going straight to terminator
    await telegram.press_button(
        bot, user.id, "terminator:1" if legacy_id_in_buttons else "terminator:55a8b659b3cd3593-1"
    )
    assert seen_terminators == ["send_feedback"]
    assert not terminator_context_check_failed
    assert set(bot.method_calls.keys()) == {"answer_callback_query"}
    bot.method_calls.clear()

    # restarting menu to go into a nested menu
    await menu_handler.start_menu(bot, user)
    bot.method_calls.clear()
    await telegram.press_button(bot, user.id, "menu:1" if legacy_id_in_buttons else "menu:55a8b659b3cd3593-1")
    assert set(bot.method_calls.keys()) == {"answer_callback_query", "edit_message_text"}
    assert reply_markups_to_dict(extract_full_kwargs(bot.method_calls["edit_message_text"])) == [
        {
            "chat_id": 1312,
            "message_id": 11111,
            "text": "<b>what do you want to pick?</b>",
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "color", "callback_data": "menu:55a8b659b3cd3593-2"}],
                    [{"text": "animal", "callback_data": "terminator:55a8b659b3cd3593-3"}],
                    [{"text": "sound", "url": "https://cool-sound-picking-game.net"}],
                    [{"text": "<-", "callback_data": "menu:55a8b659b3cd3593-0"}],
                ]
            },
        }
    ]
    bot.method_calls.clear()

    await telegram.press_button(bot, user.id, "menu:2" if legacy_id_in_buttons else "menu:55a8b659b3cd3593-2")
    assert set(bot.method_calls.keys()) == {"answer_callback_query", "edit_message_text"}
    assert reply_markups_to_dict(extract_full_kwargs(bot.method_calls["edit_message_text"])) == [
        {
            "chat_id": 1312,
            "message_id": 11111,
            "text": "pick color",
            "parse_mode": None,
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "red", "callback_data": "terminator:55a8b659b3cd3593-5"}],
                    [{"text": "green", "callback_data": "terminator:55a8b659b3cd3593-6"}],
                    [{"text": "blue", "callback_data": "terminator:55a8b659b3cd3593-7"}],
                    [{"text": "back", "callback_data": "menu:55a8b659b3cd3593-1"}],
                ]
            },
        }
    ]
    bot.method_calls.clear()

    await telegram.press_button(bot, user.id, "menu:1" if legacy_id_in_buttons else "menu:55a8b659b3cd3593-1")
    assert set(bot.method_calls.keys()) == {"answer_callback_query", "edit_message_text"}
    assert reply_markups_to_dict(extract_full_kwargs(bot.method_calls["edit_message_text"])) == [
        {
            "chat_id": 1312,
            "message_id": 11111,
            "text": "<b>what do you want to pick?</b>",
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "color", "callback_data": "menu:55a8b659b3cd3593-2"}],
                    [{"text": "animal", "callback_data": "terminator:55a8b659b3cd3593-3"}],
                    [{"text": "sound", "url": "https://cool-sound-picking-game.net"}],
                    [{"text": "<-", "callback_data": "menu:55a8b659b3cd3593-0"}],
                ]
            },
        }
    ]
    bot.method_calls.clear()

    await telegram.press_button(
        bot, user.id, "terminator:3" if legacy_id_in_buttons else "terminator:55a8b659b3cd3593-3"
    )
    assert set(bot.method_calls.keys()) == {"answer_callback_query"}
    assert seen_terminators == ["send_feedback", "pick_animal"]
    assert not terminator_context_check_failed
    bot.method_calls.clear()

    # after terminator is triggered, the menu is not editable anymore, but buttons are still working

    await telegram.press_button(bot, user.id, "menu:0" if legacy_id_in_buttons else "menu:55a8b659b3cd3593-0")
    assert set(bot.method_calls.keys()) == {"answer_callback_query", "send_message"}
    assert reply_markups_to_dict(extract_full_kwargs(bot.method_calls["send_message"])) == [
        {
            "chat_id": 1312,
            "parse_mode": None,
            "text": "example menu =<^_^>=",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "picking game", "callback_data": "menu:55a8b659b3cd3593-1"}],
                    [{"text": "feedback", "callback_data": "terminator:55a8b659b3cd3593-1"}],
                ]
            },
        }
    ]
    bot.method_calls.clear()

    await telegram.press_button(bot, user.id, "menu:1" if legacy_id_in_buttons else "menu:55a8b659b3cd3593-1")
    await telegram.press_button(bot, user.id, "menu:2" if legacy_id_in_buttons else "menu:55a8b659b3cd3593-2")
    assert set(bot.method_calls.keys()) == {"answer_callback_query", "edit_message_text"}
    assert len(bot.method_calls["edit_message_text"]) == 2
    bot.method_calls.clear()

    # final press is locking menu after termination
    await telegram.press_button(
        bot, user.id, "terminator:6" if legacy_id_in_buttons else "terminator:55a8b659b3cd3593-6"
    )
    assert set(bot.method_calls.keys()) == {"answer_callback_query", "edit_message_reply_markup"}
    assert seen_terminators == ["send_feedback", "pick_animal", "green"]
    assert reply_markups_to_dict(extract_full_kwargs(bot.method_calls["edit_message_reply_markup"])) == [
        {
            "chat_id": 1312,
            "message_id": 11111,
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "red", "callback_data": "inactive_button"}],
                    [{"text": "✅ green", "callback_data": "inactive_button"}],
                    [{"text": "blue", "callback_data": "inactive_button"}],
                ]
            },
        }
    ]


async def test_several_menus_per_bot(redis: RedisInterface) -> None:
    bot_prefix = "test-1312"
    bot = MockedAsyncTeleBot(token="")

    telegram = TelegramServerMock()

    menu_handler_1 = MenuHandler(
        bot_prefix=bot_prefix,
        menu_tree=Menu(
            text="menu 1",
            menu_items=[
                MenuItem(label="option 1", terminator="menu 1 opt 1"),
                MenuItem(label="option 2", terminator="menu 1 opt 2"),
            ],
            config=MenuConfig(back_label="<-"),
        ),
        name="menu-handler-1",
        redis=redis,
    )
    menu_handler_2 = MenuHandler(
        bot_prefix=bot_prefix,
        menu_tree=Menu(
            text="menu 2",
            menu_items=[
                MenuItem(label="option 1", terminator="menu 2 opt 1"),
                MenuItem(label="option 2", terminator="menu 2 opt 2"),
                MenuItem(label="option 3", terminator="menu 2 opt 3"),
            ],
            config=MenuConfig(back_label="<-"),
        ),
        name="menu-handler-2",
        redis=redis,
    )

    user = tg.User(id=1337, is_bot=False, first_name="Jane", last_name="Doe")
    seen_terminators: List[str] = []
    terminator_context_check_failed = False

    async def on_menu_termination(context: TerminatorContext) -> None:
        seen_terminators.append(context.terminator)
        nonlocal terminator_context_check_failed
        if not (context.bot is bot and context.user.id == user.id):
            terminator_context_check_failed = True

    menu_handler_1.setup(bot, on_menu_termination)
    menu_handler_2.setup(bot, on_menu_termination)

    # using first menu
    await menu_handler_1.start_menu(bot, user)
    assert len(bot.method_calls) == 1
    assert reply_markups_to_dict(extract_full_kwargs(bot.method_calls.pop("send_message"))) == [
        {
            "chat_id": 1337,
            "text": "menu 1",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "option 1", "callback_data": "terminator:efa8ddb0b524eb41-0"}],
                    [{"text": "option 2", "callback_data": "terminator:efa8ddb0b524eb41-1"}],
                ]
            },
            "parse_mode": None,
        }
    ]
    bot.method_calls.clear()

    # ... and second one
    await menu_handler_2.start_menu(bot, user)
    assert len(bot.method_calls) == 1
    assert extract_full_kwargs(bot.method_calls.pop("send_message")) == [
        {
            "chat_id": 1337,
            "text": "menu 2",
            "reply_markup": tg.InlineKeyboardMarkup(
                [
                    [tg.InlineKeyboardButton(text="option 1", callback_data="terminator:84c76de37c5679e0-0")],
                    [tg.InlineKeyboardButton(text="option 2", callback_data="terminator:84c76de37c5679e0-1")],
                    [tg.InlineKeyboardButton(text="option 3", callback_data="terminator:84c76de37c5679e0-2")],
                ]
            ),
            "parse_mode": None,
        }
    ]
    bot.method_calls.clear()

    await telegram.press_button(bot, user_id=user.id, callback_data="terminator:efa8ddb0b524eb41-0")
    await telegram.press_button(bot, user_id=user.id, callback_data="terminator:efa8ddb0b524eb41-1")
    await telegram.press_button(bot, user_id=user.id, callback_data="terminator:84c76de37c5679e0-0")
    await telegram.press_button(bot, user_id=user.id, callback_data="terminator:84c76de37c5679e0-1")
    await telegram.press_button(bot, user_id=user.id, callback_data="terminator:84c76de37c5679e0-2")

    assert seen_terminators == [
        "menu 1 opt 1",
        "menu 1 opt 2",
        "menu 2 opt 1",
        "menu 2 opt 2",
        "menu 2 opt 3",
    ]
    assert not terminator_context_check_failed


async def test_menu_handler_with_language_store(redis: RedisInterface):
    bot = MockedAsyncTeleBot(token="")
    example_user = tg.User(id=161, is_bot=False, first_name="Piotr", last_name="Kropotkin", language_code="ru")

    menu = Menu(
        text={
            Language.RU: "заголовок",
            Language.EN: "header",
        },
        config=MenuConfig(
            back_label={Language.RU: "назад", Language.EN: "back"}, lock_after_termination=False, is_text_html=False
        ),
        menu_items=[
            MenuItem(label={Language.RU: "один", Language.EN: "one"}, terminator="1"),
            MenuItem(label={Language.RU: "два", Language.EN: "two"}, terminator="2"),
        ],
    )
    language_store = LanguageStore(
        redis=redis,
        bot_prefix="1111test",
        supported_languages=[Language.RU, Language.EN],
        default_language=Language.EN,
    )

    menu_handler = MenuHandler(
        bot_prefix="testing",
        menu_tree=menu,
        language_store=language_store,
        name="test-menu-with-langs",
        redis=redis,
    )

    async def on_menu_termination(context: TerminatorContext) -> None:
        pass

    menu_handler.setup(bot, on_terminal_menu_option_selected=on_menu_termination)

    await menu_handler.start_menu(bot, example_user)
    assert len(bot.method_calls) == 1
    all_send_message_kw = extract_full_kwargs(bot.method_calls["send_message"])
    assert len(all_send_message_kw) == 1
    send_message_kw = all_send_message_kw[0]
    reply_markup = send_message_kw.pop("reply_markup")
    assert send_message_kw == {
        "chat_id": 161,
        "text": "заголовок",
        "parse_mode": None,
    }
    assert reply_markup.to_dict() == {
        "inline_keyboard": [
            [{"text": "один", "callback_data": "terminator:52744b22f922b564-0"}],
            [{"text": "два", "callback_data": "terminator:52744b22f922b564-1"}],
        ]
    }
    bot.method_calls.clear()

    await language_store.set_user_language(example_user, Language.EN)

    await menu_handler.start_menu(bot, example_user)
    assert len(bot.method_calls) == 1
    all_send_message_kw = extract_full_kwargs(bot.method_calls["send_message"])
    assert len(all_send_message_kw) == 1
    send_message_kw = all_send_message_kw[0]
    reply_markup = send_message_kw.pop("reply_markup")
    assert send_message_kw == {
        "chat_id": 161,
        "text": "header",
        "parse_mode": None,
    }
    assert reply_markup.to_dict() == {
        "inline_keyboard": [
            [{"text": "one", "callback_data": "terminator:52744b22f922b564-0"}],
            [{"text": "two", "callback_data": "terminator:52744b22f922b564-1"}],
        ]
    }
    bot.method_calls.clear()


async def test_menu_handler_with_reply_buttons(redis: RedisInterface):
    bot = MockedAsyncTeleBot(token="1234567")

    catch_all_received_messages: list[tg.Message] = []

    @bot.message_handler()
    async def catch_all_handler(message: tg.Message):
        catch_all_received_messages.append(message)

    example_user = tg.User(id=161, is_bot=False, first_name="Ivan", last_name="Ivanov")

    telegram = TelegramServerMock()

    menu = Menu(
        text="menu",
        config=MenuConfig(back_label="<-", mechanism=MenuMechanism.REPLY_KEYBOARD),
        menu_items=[
            MenuItem(
                label="one",
                submenu=Menu(
                    text="submenu one",
                    menu_items=[
                        MenuItem(label="1 sub 1", terminator="1.1"),
                        MenuItem(label="1 sub 2", terminator="1.2"),
                    ],
                ),
            ),
            MenuItem(
                label="no-escape",
                submenu=Menu(
                    text="no escape submenu",
                    config=MenuConfig(back_label=None, mechanism=MenuMechanism.REPLY_KEYBOARD),
                    menu_items=[
                        MenuItem(label="red pill", terminator="red"),
                        MenuItem(label="blue pill", terminator="blue"),
                    ],
                ),
            ),
        ],
    )

    menu_handler = MenuHandler(
        bot_prefix="testing",
        menu_tree=menu,
        name="test-menu-on-reply-buttons",
        redis=redis,
    )

    async def on_menu_termination(context: TerminatorContext) -> None:
        pass

    menu_handler.setup(bot, on_terminal_menu_option_selected=on_menu_termination)

    await menu_handler.start_menu(bot, example_user)
    assert len(bot.method_calls) == 1
    all_send_message_kw = extract_full_kwargs(bot.method_calls["send_message"])
    assert len(all_send_message_kw) == 1
    send_message_kw = all_send_message_kw[0]
    reply_markup = send_message_kw.pop("reply_markup")
    assert send_message_kw == {
        "chat_id": 161,
        "text": "menu",
        "parse_mode": None,
    }
    assert reply_markup.to_dict() == {
        "keyboard": [[{"text": "one"}], [{"text": "no-escape"}]],
        "one_time_keyboard": True,
        "resize_keyboard": True,
    }
    bot.method_calls.clear()

    await telegram.send_message_to_bot(bot, user_id=example_user.id, text="one")
    assert len(bot.method_calls) == 1
    all_send_message_kw = extract_full_kwargs(bot.method_calls["send_message"])
    assert len(all_send_message_kw) == 1
    send_message_kw = all_send_message_kw[0]
    reply_markup = send_message_kw.pop("reply_markup")
    assert send_message_kw == {
        "chat_id": 161,
        "text": "submenu one",
        "parse_mode": None,
    }
    assert reply_markup.to_dict() == {
        "keyboard": [[{"text": "1 sub 1"}], [{"text": "1 sub 2"}], [{"text": "<-"}]],
        "one_time_keyboard": True,
        "resize_keyboard": True,
    }
    bot.method_calls.clear()

    await telegram.send_message_to_bot(
        bot, user_id=example_user.id, text="some random other message not for menu handler"
    )
    assert len(catch_all_received_messages) == 1

    # back to main menu
    await telegram.send_message_to_bot(bot, user_id=example_user.id, text="<-")
    bot.method_calls.clear()

    await telegram.send_message_to_bot(bot, user_id=example_user.id, text="no-escape")
    assert len(bot.method_calls) == 1
    all_send_message_kw = extract_full_kwargs(bot.method_calls["send_message"])
    assert len(all_send_message_kw) == 1
    send_message_kw = all_send_message_kw[0]
    reply_markup = send_message_kw.pop("reply_markup")
    assert send_message_kw == {
        "chat_id": 161,
        "text": "no escape submenu",
        "parse_mode": None,
    }
    assert reply_markup.to_dict() == {
        "keyboard": [[{"text": "red pill"}], [{"text": "blue pill"}]],
        "one_time_keyboard": True,
        "resize_keyboard": True,
    }
    bot.method_calls.clear()


async def test_menu_with_different_mechanisms(redis: RedisInterface):
    bot = MockedAsyncTeleBot(token="token")
    user = tg.User(id=1234, is_bot=False, first_name="Andy", last_name="Bernard")
    telegram = TelegramServerMock()

    menu = Menu(
        text="top level",
        config=MenuConfig(back_label="back", mechanism=MenuMechanism.REPLY_KEYBOARD),
        menu_items=[
            MenuItem(
                label="one",
                submenu=Menu(
                    text="submenu one",
                    menu_items=[
                        MenuItem(label="1 sub 1", terminator="1.1"),
                        MenuItem(label="1 sub 2", terminator="1.2"),
                    ],
                    config=MenuConfig(back_label="back", mechanism=MenuMechanism.INLINE_BUTTONS),
                ),
            ),
        ],
    )

    menu_handler = MenuHandler(
        bot_prefix="test-1-2-1-2",
        menu_tree=menu,
        name="test-menu-on-reply-buttons",
        redis=redis,
    )

    terminators_reached: list[str] = []

    async def on_menu_termination(context: TerminatorContext) -> None:
        terminators_reached.append(context.terminator)

    menu_handler.setup(bot, on_terminal_menu_option_selected=on_menu_termination)

    await menu_handler.start_menu(bot, user)
    assert len(bot.method_calls) == 1
    assert reply_markups_to_dict(extract_full_kwargs(bot.method_calls["send_message"])) == [
        {
            "chat_id": 1234,
            "text": "top level",
            "parse_mode": None,
            "reply_markup": {
                "keyboard": [[{"text": "one"}]],
                "one_time_keyboard": True,
                "resize_keyboard": True,
            },
        }
    ]
    bot.method_calls.clear()

    await telegram.send_message_to_bot(bot, user_id=user.id, text="one")
    assert len(bot.method_calls) == 1
    assert reply_markups_to_dict(extract_full_kwargs(bot.method_calls["send_message"])) == [
        {
            "chat_id": 1234,
            "text": "submenu one",
            "parse_mode": None,
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "1 sub 1", "callback_data": "terminator:6b36666a13608c19-1"}],
                    [{"text": "1 sub 2", "callback_data": "terminator:6b36666a13608c19-2"}],
                    [{"text": "back", "callback_data": "menu:6b36666a13608c19-0"}],
                ],
            },
        }
    ]
    bot.method_calls.clear()

    # back to main menu
    await telegram.press_button(bot, user.id, "menu:6b36666a13608c19-0")
    assert set(bot.method_calls.keys()) == {"send_message", "answer_callback_query"}
    assert reply_markups_to_dict(extract_full_kwargs(bot.method_calls["send_message"])) == [
        {
            "chat_id": 1234,
            "text": "top level",
            "parse_mode": None,
            "reply_markup": {
                "keyboard": [[{"text": "one"}]],
                "one_time_keyboard": True,
                "resize_keyboard": True,
            },
        }
    ]
    bot.method_calls.clear()


@pytest.mark.parametrize(
    "markup, is_text_html, expected_parse_mode",
    [
        pytest.param(TextMarkup.NONE, False, None),
        pytest.param(TextMarkup.HTML, False, "HTML"),
        pytest.param(TextMarkup.MARKDOWN, False, "MarkdownV2"),
        pytest.param(TextMarkup.NONE, True, "HTML"),
    ],
)
async def test_text_markups(
    redis: RedisInterface, markup: TextMarkup, is_text_html: bool, expected_parse_mode: str | None
):
    bot = MockedAsyncTeleBot(token="token")
    user = tg.User(id=1234, is_bot=False, first_name="Andy", last_name="Bernard")
    telegram = TelegramServerMock()

    menu = Menu(
        text="menu",
        config=MenuConfig(
            back_label=None,
            mechanism=MenuMechanism.INLINE_BUTTONS,
            text_markup=markup,
            is_text_html=is_text_html,
        ),
        menu_items=[
            MenuItem(
                label="option",
                submenu=Menu(
                    text="submenu",
                    menu_items=[
                        MenuItem(label="fin", terminator="fin"),
                    ],
                    config=MenuConfig(
                        back_label=None,
                        mechanism=MenuMechanism.INLINE_BUTTONS,
                        text_markup=markup,
                        is_text_html=is_text_html,
                    ),
                ),
            ),
        ],
    )

    menu_handler = MenuHandler(
        bot_prefix="test-1-2-1-2",
        menu_tree=menu,
        name="testing",
        redis=redis,
    )

    async def on_menu_termination(context: TerminatorContext) -> None:
        pass

    menu_handler.setup(bot, on_terminal_menu_option_selected=on_menu_termination)

    await menu_handler.start_menu(bot, user)
    assert len(bot.method_calls) == 1
    assert reply_markups_to_dict(extract_full_kwargs(bot.method_calls["send_message"])) == [
        {
            "chat_id": 1234,
            "text": "menu",
            "parse_mode": expected_parse_mode,
            "reply_markup": {"inline_keyboard": [[{"text": "option", "callback_data": "menu:ae2b1fca515949e5-1"}]]},
        }
    ]
    bot.method_calls.clear()

    await telegram.press_button(bot, user.id, "menu:ae2b1fca515949e5-1")
    assert set(bot.method_calls.keys()) == {"edit_message_text", "answer_callback_query"}
    assert reply_markups_to_dict(extract_full_kwargs(bot.method_calls["edit_message_text"])) == [
        {
            "chat_id": 1234,
            "message_id": 11111,
            "text": "submenu",
            "parse_mode": expected_parse_mode,
            "reply_markup": {
                "inline_keyboard": [[{"callback_data": "terminator:ae2b1fca515949e5-1", "text": "fin"}]],
            },
        }
    ]
    bot.method_calls.clear()
