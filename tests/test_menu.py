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
    TerminatorContext,
)


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
        text="example menu",
        menu_items=[
            MenuItem(
                label="picking game",
                submenu=Menu(
                    text="what do you want to pick?",
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


async def test_menu_handler_basic(example_menu: Menu):
    bot = MockedAsyncTeleBot(token="")
    menu_handler = MenuHandler(
        bot_prefix="testing",
        menu_tree=example_menu,
    )

    example_user = tg.User(id=1312, is_bot=False, first_name="Max", last_name="Slater")
    language = await menu_handler.get_maybe_language(example_user)

    seen_terminators: List[str] = []
    terminator_context_check_failed = False

    async def on_menu_termination(context: TerminatorContext) -> None:
        seen_terminators.append(context.terminator)
        nonlocal terminator_context_check_failed
        if not (context.bot is bot and context.user.to_dict() == example_user.to_dict()):
            terminator_context_check_failed = True

    menu_handler.setup(bot, on_terminal_menu_option_selected=on_menu_termination)

    main_menu = menu_handler.get_main_menu()
    assert main_menu.text == "example menu"
    assert main_menu.get_keyboard_markup(language).to_dict() == {
        "inline_keyboard": [
            [{"text": "picking game", "callback_data": "menu:1"}],
            [{"text": "feedback", "callback_data": "terminator:1"}],
        ]
    }

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

    await press_button("terminator:1")
    assert seen_terminators == ["send_feedback"]
    assert not terminator_context_check_failed

    await press_button("menu:1")
    assert len(bot.method_calls["edit_message_text"]) == 1
    edit_menu_message_method_call = bot.method_calls["edit_message_text"][0]
    assert edit_menu_message_method_call.full_kwargs["text"] == "what do you want to pick?"
    assert edit_menu_message_method_call.full_kwargs["reply_markup"].to_dict() == {
        "inline_keyboard": [
            [{"text": "color", "callback_data": "menu:2"}],
            [{"text": "animal", "callback_data": "terminator:3"}],
            [{"text": "sound", "url": "https://cool-sound-picking-game.net"}],
            [{"text": "<-", "callback_data": "menu:0"}],
        ]
    }

    await press_button("menu:2")
    assert len(bot.method_calls["edit_message_text"]) == 2
    edit_menu_message_method_call = bot.method_calls["edit_message_text"][1]
    assert edit_menu_message_method_call.full_kwargs["text"] == "pick color"
    assert edit_menu_message_method_call.full_kwargs["reply_markup"].to_dict() == {
        "inline_keyboard": [
            [{"text": "red", "callback_data": "terminator:5"}],
            [{"text": "green", "callback_data": "terminator:6"}],
            [{"text": "blue", "callback_data": "terminator:7"}],
            [{"text": "black", "callback_data": "menu:1"}],
        ]
    }

    await press_button("menu:1")
    assert len(bot.method_calls["edit_message_text"]) == 3
    edit_menu_message_method_call = bot.method_calls["edit_message_text"][2]
    assert edit_menu_message_method_call.full_kwargs["text"] == "what do you want to pick?"
    assert edit_menu_message_method_call.full_kwargs["reply_markup"].to_dict() == {
        "inline_keyboard": [
            [{"text": "color", "callback_data": "menu:2"}],
            [{"text": "animal", "callback_data": "terminator:3"}],
            [{"text": "sound", "url": "https://cool-sound-picking-game.net"}],
            [{"text": "<-", "callback_data": "menu:0"}],
        ]
    }

    await press_button("terminator:3")
    assert len(bot.method_calls["edit_message_text"]) == 3
    assert seen_terminators == ["send_feedback", "pick_animal"]
    assert not terminator_context_check_failed

    # back to the main menu

    await press_button("menu:0")
    assert len(bot.method_calls["edit_message_text"]) == 4
    edit_menu_message_method_call = bot.method_calls["edit_message_text"][3]
    assert edit_menu_message_method_call.full_kwargs["text"] == "example menu"
    assert edit_menu_message_method_call.full_kwargs["reply_markup"].to_dict() == {
        "inline_keyboard": [
            [{"text": "picking game", "callback_data": "menu:1"}],
            [{"text": "feedback", "callback_data": "terminator:1"}],
        ]
    }

    await press_button("menu:1")
    await press_button("menu:2")
    assert len(bot.method_calls["edit_message_text"]) == 6
    edit_menu_message_method_call = bot.method_calls["edit_message_text"][5]
    assert edit_menu_message_method_call.full_kwargs["text"] == "pick color"

    await press_button("terminator:6")
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
