import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from itertools import chain
from typing import Any, Coroutine, Optional, Union

import pytest

from telebot_components.utils import (
    LockRegistry,
    emoji_hash,
    from_yaml_unsafe,
    join_paragraphs,
    telegram_message_url,
    text_hash,
    to_yaml_unsafe,
    trim_with_ellipsis,
)
from telebot_components.utils.strings import html_link, mask, remove_command_prefix


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


async def test_lock_registry():
    @dataclass
    class Event:
        is_start: bool
        idx: int
        timestamp: float

    shared_state: dict[str, list[Event]] = defaultdict(list)
    lock_registry = LockRegistry()

    async def worker(key: str, idx: int) -> None:
        async with lock_registry.get_lock(key):
            shared_state[key].append(Event(is_start=True, idx=idx, timestamp=time.time()))
            await asyncio.sleep(0.01)
            shared_state[key].append(Event(is_start=False, idx=idx, timestamp=time.time()))

    worker_coros: list[Coroutine[None, None, None]] = []
    for key_int in range(1000):
        for idx in range(10):
            worker_coros.append(worker(str(key_int), idx))

    await asyncio.gather(*worker_coros)

    for key in shared_state.keys():
        events = shared_state[key]
        # checking that the lock forces concurrent execution, with each index starting and finishing
        assert [e.idx for e in events] == list(chain.from_iterable([idx, idx] for idx in range(10)))
        assert [e.is_start for e in events] == list(chain.from_iterable([True, False] for _ in range(10)))

    # checking that lock registry doesn't hold references for unused keys anymore
    assert len(lock_registry._lock_by_key) == 0


def test_alphabet_hash():
    assert emoji_hash(123456789, "hello-world") == "🤸👐🃏🦯"
    assert emoji_hash(10000, "hello-world") == "⚒🌑💄🧃"
    assert emoji_hash(-10000, "hello-world") == "🔵🦠🕔🌂"

    assert emoji_hash(1, "hello-world") == "⬛🈲🧅🛖"
    assert emoji_hash(1, "hello-world2") == "🥕✔🔩🧐"

    assert text_hash(1312, "foo") == "tbape0"
    assert text_hash(817269837164, "foo") == "hGSS2k"


@pytest.mark.parametrize(
    "original, open_ratio, expected_masked",
    [
        pytest.param("hello world", 0.3, "hel********"),
        pytest.param("hello world", 0.5, "hello******"),
        pytest.param("abcdefg", 0, "*******"),
        pytest.param("abcdefg", 1, "abcdefg"),
        pytest.param("abcdefg", 1000, "abcdefg"),
        pytest.param("abcdefg", -1000, "*******"),
    ],
)
def test_mask_string(original: str, open_ratio: float, expected_masked: str):
    assert mask(original, open_ratio) == expected_masked


@pytest.mark.parametrize(
    "original, expected",
    [
        pytest.param("hi", "hi"),
        pytest.param("/command", ""),
        pytest.param("/command    ", ""),
        pytest.param("/command payload", "payload"),
        pytest.param("/command@bot_username", ""),
        pytest.param("/command@bot_username payload", "payload"),
        pytest.param("/command@bot_username        payload        ", "payload"),
    ],
)
def test_remove_command_prefix(original: str, expected: str):
    assert remove_command_prefix(original) == expected


@pytest.mark.parametrize(
    "href, text, expected",
    [
        pytest.param("", "", '<a href=""></a>'),
        pytest.param("", "hello", '<a href="">hello</a>'),
        pytest.param("google.com", "world", '<a href="google.com">world</a>'),
    ],
)
def test_html_link(href: str, text: str, expected: str):
    assert html_link(href, text) == expected
