from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Union

import pytest

from telebot_components.utils import (
    from_yaml_unsafe,
    join_paragraphs,
    telegram_message_url,
    to_yaml_unsafe,
    trim_with_ellipsis,
)


@pytest.mark.parametrize(
    "paragraphs, expected_joined",
    [
        pytest.param(["a", "b"], "a\n\nb"),
        pytest.param(["hello", "", "", ""], "hello"),
        pytest.param(["", "", "foo", ""], "foo"),
        pytest.param(["", "", "", ""], ""),
    ],
)
def test_join_paragraphs(paragraphs: list[str], expected_joined: str):
    assert join_paragraphs(paragraphs) == expected_joined


@pytest.mark.parametrize(
    "chat_id, message_id, thread_op_message_id, comment_message_id, expected_url",
    [
        pytest.param(
            -1009510656010,
            5724,
            None,
            None,
            "https://t.me/c/9510656010/5724",
            id="supergroup message link",
        ),
        pytest.param(
            "@my_channel_handle",
            1312,
            None,
            None,
            "https://t.me/my_channel_handle/1312",
            id="channel post link",
        ),
        pytest.param(
            "@my_channel_handle",
            1312,
            None,
            5763,
            "https://t.me/my_channel_handle/1312?comment=5763",
            id="channel post comment (1st way)",
        ),
        pytest.param(
            -1009510656010,
            9000,
            8440,
            None,
            "https://t.me/c/9510656010/9000?thread=8440",
            id="channel post comment (2nd way)",
        ),
    ],
)
def test_telegram_message_url(
    chat_id: Union[int, str],
    message_id: int,
    thread_op_message_id: Optional[int],
    comment_message_id: Optional[int],
    expected_url: str,
):
    assert telegram_message_url(chat_id, message_id, thread_op_message_id, comment_message_id) == expected_url


def test_telegram_message_url_mutually_exclusive_params():
    with pytest.raises(ValueError):
        telegram_message_url("@something", 1312, 555, 145)


@pytest.mark.parametrize(
    "message, target_len, expected_trimmed",
    [
        pytest.param(
            "Lorem ipsum dolor sit amet",
            9,
            "Lorem ipsum...",
        ),
        pytest.param(
            "    Lorem ipsum dolor sit",
            7,
            "Lorem ipsum...",
            id="spaces are stripped before trimming text",
        ),
        pytest.param(
            "  Lorem      ipsum dolor sit",
            14,
            "Lorem ipsum dolor...",
            id="multiple spaces between words are collapsed before trimming text",
        ),
        pytest.param("Lorem ipsum dolor sit amet", 1, "Lorem...", id="trim only on word boundaries"),
        pytest.param(
            "Lorem ipsum dolor sit amet",
            1000,
            "Lorem ipsum dolor sit amet",
            id="no ellipsis if the whole message is inside target len",
        ),
        pytest.param(
            "   Lorem    ipsum dolor sit  amet     ",
            1000,
            "   Lorem    ipsum dolor sit  amet     ",
            id="no whitesspace stripping and collapsing when no trimming is done",
        ),
    ],
)
def test_trim_with_ellipsis(message: str, target_len: int, expected_trimmed: str):
    assert trim_with_ellipsis(message, target_len) == expected_trimmed


class Color(Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"
    OTHER = {"complex": "enum value"}


@dataclass
class Container:
    @dataclass
    class Nested:
        some_other_data: bytes

    data: str
    param: int
    nested: Nested


@pytest.mark.parametrize(
    "obj",
    [
        pytest.param("hi"),
        pytest.param(420),
        pytest.param(14.30),
        pytest.param({"name": "John", "age": 35, "gender": None}, id="json-like dict"),
        pytest.param([{"name": "John", "age": 35, "gender": None}, 7, {"another": "mapping"}]),
        pytest.param(Color.RED),
        pytest.param(Color.OTHER),
        pytest.param([Color.BLUE, Color.GREEN]),
        pytest.param(Container("hello world", 1312, nested=Container.Nested(b"001110010"))),
    ],
)
def test_yaml_serialization(obj: Any):
    assert from_yaml_unsafe(to_yaml_unsafe(obj)) == obj
