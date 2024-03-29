from datetime import datetime
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
from telebot_components.redis_utils.emulation import RedisEmulation
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.language import Language, LanguageStore
from tests.utils import TelegramServerMock, extract_full_kwargs


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
                                    back_label="black",
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
async def test_menu_handler_basic(example_menu: Menu, legacy_id_in_buttons: bool):
    bot = MockedAsyncTeleBot(token="")
    menu_handler = MenuHandler(
        bot_prefix="testing",
        menu_tree=example_menu,
        name="test-menu-basic",
        redis=RedisEmulation(),
    )

    example_user = tg.User(id=1312, is_bot=False, first_name="Max", last_name="Slater")
    seen_terminators: List[str] = []
    terminator_context_check_failed = False

    async def on_menu_termination(context: TerminatorContext) -> None:
        seen_terminators.append(context.terminator)
        nonlocal terminator_context_check_failed
        if not (context.bot is bot and context.user.to_dict() == example_user.to_dict()):
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

    await menu_handler.start_menu(bot, example_user)
    assert len(bot.method_calls) == 1
    assert extract_full_kwargs(bot.method_calls.pop("send_message")) == [
        {
            "chat_id": 1312,
            "text": "example menu =&lt;^_^&gt;=",  # NOTE: properly escaped for parse mode HTML
            "reply_markup": tg.InlineKeyboardMarkup(
                [
                    [tg.InlineKeyboardButton(text="picking game", callback_data="menu:55a8b659b3cd3593-1")],
                    [tg.InlineKeyboardButton(text="feedback", callback_data="terminator:55a8b659b3cd3593-1")],
                ]
            ),
            "parse_mode": "HTML",
        }
    ]
    bot.method_calls.clear()

    async def press_button(callback_data: str):
        update_json = {
            "update_id": 19283649187364,
            "callback_query": {
                "id": 40198734019872364,
                "chat_instance": "wtf is this",
                "from": example_user.to_dict(),
                "data": callback_data,
                "message": {
                    "message_id": 11111,
                    "from": example_user.to_dict(),
                    "chat": {
                        "id": 420,
                        "type": "private",
                    },
                    "date": int(datetime.now().timestamp()),
                    "text": "menu message placeholder",
                },
            },
        }
        await bot.process_new_updates([tg.Update.de_json(update_json)])  # type: ignore

    await press_button("terminator:1" if legacy_id_in_buttons else "terminator:55a8b659b3cd3593-1")
    assert seen_terminators == ["send_feedback"]
    assert not terminator_context_check_failed

    await press_button("menu:1" if legacy_id_in_buttons else "menu:55a8b659b3cd3593-1")
    assert len(bot.method_calls["edit_message_text"]) == 1
    edit_menu_message_method_call = bot.method_calls["edit_message_text"][0]
    assert (
        edit_menu_message_method_call.full_kwargs["text"] == "<b>what do you want to pick?</b>"
    )  # not escaped, true html
    assert edit_menu_message_method_call.full_kwargs["reply_markup"].to_dict() == {
        "inline_keyboard": [
            [{"text": "color", "callback_data": "menu:55a8b659b3cd3593-2"}],
            [{"text": "animal", "callback_data": "terminator:55a8b659b3cd3593-3"}],
            [{"text": "sound", "url": "https://cool-sound-picking-game.net"}],
            [{"text": "<-", "callback_data": "menu:55a8b659b3cd3593-0"}],
        ]
    }

    await press_button("menu:2" if legacy_id_in_buttons else "menu:55a8b659b3cd3593-2")
    assert len(bot.method_calls["edit_message_text"]) == 2
    edit_menu_message_method_call = bot.method_calls["edit_message_text"][1]
    assert edit_menu_message_method_call.full_kwargs["text"] == "pick color"
    assert edit_menu_message_method_call.full_kwargs["reply_markup"].to_dict() == {
        "inline_keyboard": [
            [{"text": "red", "callback_data": "terminator:55a8b659b3cd3593-5"}],
            [{"text": "green", "callback_data": "terminator:55a8b659b3cd3593-6"}],
            [{"text": "blue", "callback_data": "terminator:55a8b659b3cd3593-7"}],
            [{"text": "black", "callback_data": "menu:55a8b659b3cd3593-1"}],
        ]
    }

    await press_button("menu:1" if legacy_id_in_buttons else "menu:55a8b659b3cd3593-1")
    assert len(bot.method_calls["edit_message_text"]) == 3
    edit_menu_message_method_call = bot.method_calls["edit_message_text"][2]
    assert edit_menu_message_method_call.full_kwargs["text"] == "<b>what do you want to pick?</b>"
    assert edit_menu_message_method_call.full_kwargs["reply_markup"].to_dict() == {
        "inline_keyboard": [
            [{"text": "color", "callback_data": "menu:55a8b659b3cd3593-2"}],
            [{"text": "animal", "callback_data": "terminator:55a8b659b3cd3593-3"}],
            [{"text": "sound", "url": "https://cool-sound-picking-game.net"}],
            [{"text": "<-", "callback_data": "menu:55a8b659b3cd3593-0"}],
        ]
    }

    await press_button("terminator:3" if legacy_id_in_buttons else "terminator:55a8b659b3cd3593-3")
    assert len(bot.method_calls["edit_message_text"]) == 3
    assert seen_terminators == ["send_feedback", "pick_animal"]
    assert not terminator_context_check_failed

    # back to the main menu

    await press_button("menu:0" if legacy_id_in_buttons else "menu:55a8b659b3cd3593-0")
    assert len(bot.method_calls["edit_message_text"]) == 4
    edit_menu_message_method_call = bot.method_calls["edit_message_text"][3]
    assert edit_menu_message_method_call.full_kwargs["text"] == "example menu =&lt;^_^&gt;="
    assert edit_menu_message_method_call.full_kwargs["reply_markup"].to_dict() == {
        "inline_keyboard": [
            [{"text": "picking game", "callback_data": "menu:55a8b659b3cd3593-1"}],
            [{"text": "feedback", "callback_data": "terminator:55a8b659b3cd3593-1"}],
        ]
    }

    await press_button("menu:1" if legacy_id_in_buttons else "menu:55a8b659b3cd3593-1")
    await press_button("menu:2" if legacy_id_in_buttons else "menu:55a8b659b3cd3593-2")
    assert len(bot.method_calls["edit_message_text"]) == 6
    edit_menu_message_method_call = bot.method_calls["edit_message_text"][5]
    assert edit_menu_message_method_call.full_kwargs["text"] == "pick color"

    await press_button("terminator:6" if legacy_id_in_buttons else "terminator:55a8b659b3cd3593-6")
    assert len(bot.method_calls["edit_message_text"]) == 6
    assert len(bot.method_calls["edit_message_reply_markup"]) == 1
    edit_reply_markup_calls = bot.method_calls["edit_message_reply_markup"][0]
    assert edit_reply_markup_calls.full_kwargs["reply_markup"].to_dict() == {
        "inline_keyboard": [
            [{"text": "red", "callback_data": "inactive_button"}],
            [{"text": "✅ green", "callback_data": "inactive_button"}],
            [{"text": "blue", "callback_data": "inactive_button"}],
        ]
    }


async def test_several_menus_per_bot(redis: RedisInterface) -> None:
    bot_prefix = "test-1312"
    redis = RedisEmulation()
    bot = MockedAsyncTeleBot(token="")

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

    example_user = tg.User(id=1337, is_bot=False, first_name="Jane", last_name="Doe")
    seen_terminators: List[str] = []
    terminator_context_check_failed = False

    async def on_menu_termination(context: TerminatorContext) -> None:
        seen_terminators.append(context.terminator)
        nonlocal terminator_context_check_failed
        if not (context.bot is bot and context.user.to_dict() == example_user.to_dict()):
            terminator_context_check_failed = True

    menu_handler_1.setup(bot, on_menu_termination)
    menu_handler_2.setup(bot, on_menu_termination)

    async def press_button(callback_data: str):
        update_json = {
            "update_id": 19283649187364,
            "callback_query": {
                "id": 40198734019872364,
                "chat_instance": "wtf is this",
                "from": example_user.to_dict(),
                "data": callback_data,
                "message": {
                    "message_id": 11111,
                    "from": example_user.to_dict(),
                    "chat": {
                        "id": 420,
                        "type": "private",
                    },
                    "date": int(datetime.now().timestamp()),
                    "text": "menu message placeholder",
                },
            },
        }
        await bot.process_new_updates([tg.Update.de_json(update_json)])  # type: ignore

    # using first menu
    await menu_handler_1.start_menu(bot, example_user)
    assert len(bot.method_calls) == 1
    assert extract_full_kwargs(bot.method_calls.pop("send_message")) == [
        {
            "chat_id": 1337,
            "text": "menu 1",
            "reply_markup": tg.InlineKeyboardMarkup(
                [
                    [tg.InlineKeyboardButton(text="option 1", callback_data="terminator:efa8ddb0b524eb41-0")],
                    [tg.InlineKeyboardButton(text="option 2", callback_data="terminator:efa8ddb0b524eb41-1")],
                ]
            ),
            "parse_mode": "HTML",
        }
    ]
    bot.method_calls.clear()

    # ... and second one
    await menu_handler_2.start_menu(bot, example_user)
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
            "parse_mode": "HTML",
        }
    ]
    bot.method_calls.clear()

    await press_button("terminator:efa8ddb0b524eb41-0")
    await press_button("terminator:efa8ddb0b524eb41-1")
    await press_button("terminator:84c76de37c5679e0-0")
    await press_button("terminator:84c76de37c5679e0-1")
    await press_button("terminator:84c76de37c5679e0-2")

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
        redis=RedisEmulation(),
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
        "parse_mode": "HTML",
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
        "parse_mode": "HTML",
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
        redis=RedisEmulation(),
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
        "parse_mode": "HTML",
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
        "parse_mode": "HTML",
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
        "parse_mode": "HTML",
    }
    assert reply_markup.to_dict() == {
        "keyboard": [[{"text": "red pill"}], [{"text": "blue pill"}]],
        "one_time_keyboard": True,
        "resize_keyboard": True,
    }
    bot.method_calls.clear()
