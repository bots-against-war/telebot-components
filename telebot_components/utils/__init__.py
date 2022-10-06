import asyncio
import functools
import hashlib
import io
import logging
import string
from typing import Any, Awaitable, Callable, Optional, TypeVar, Union
from weakref import WeakValueDictionary

from PIL import Image  # type: ignore
from ruamel.yaml import YAML  # type: ignore
from telebot import AsyncTeleBot
from telebot import types as tg

from telebot_components.constants.emoji import EMOJI
from telebot_components.form.field import TelegramAttachment

logger = logging.getLogger(__name__)


def telegram_message_url(
    chat_id: Union[int, str],
    message_id: int,
    thread_op_message_id: Optional[int] = None,
    comment_message_id: Optional[int] = None,
) -> str:
    """Note: there are two ways to link to a comment to a channel post:
    1. Entirely from discussion chat:
        - chat_id = discussion chat id (-100xxxxxxxxxxx)
        - message_id = comment's id
        - thread_op_message_id = id of a channel post's duplicate in discussion channel
    2. From both channel and discussion chat
        - chat_id = channel handle (@my_channel)
        - message_id = channel post's id
        - comment_message_id = comment's id **in discussion chat**
    """
    if thread_op_message_id is not None and comment_message_id is not None:
        raise ValueError("thread and comment can't be used together")
    if isinstance(chat_id, int):
        chat_id_route = str(chat_id).replace("-100", "")
        chat_id_route = f"c/{chat_id_route}"
    else:
        chat_id_route = chat_id.strip("@ ")
    message_url = f"https://t.me/{chat_id_route}/{message_id}"
    if thread_op_message_id is not None:
        message_url += f"?thread={thread_op_message_id}"
    if comment_message_id is not None:
        message_url += f"?comment={comment_message_id}"
    return message_url


def trim_with_ellipsis(message: str, target_len: int) -> str:
    if len(message) <= target_len:
        return message
    words = []
    current_len = 0
    for word in message.split():
        words.append(word)
        current_len += len(word) + 1
        if current_len > target_len:
            return " ".join(words) + "..."
    return message


def join_paragraphs(lines: list[str]) -> str:
    return "\n\n".join([l for l in lines if l])


yaml = YAML(typ="unsafe")


def to_yaml_unsafe(obj: Any) -> str:
    in_memory_stream = io.StringIO()
    yaml.dump(obj, in_memory_stream)
    return in_memory_stream.getvalue()


def from_yaml_unsafe(dump: str) -> Any:
    return yaml.load(dump)


def telegram_html_escape(string: str) -> str:
    """See https://core.telegram.org/bots/api#html-style"""
    return string.replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")


def html_link(href: str, text: str) -> str:
    return f'<a href="{href}">{text}</a>'


def markdown_link(href: str, text: str) -> str:
    return f"[{text}]({href})"


def _pretty_hash_from_alphabet(some_id: int, bot_prefix: str, length: int, alphabet: list[str]) -> str:
    """Do not use for any security-related hashing, just for user-facing anonymized signatures"""
    if len(alphabet) > 65536:
        raise ValueError(f"Alphabet has length {len(alphabet)}, exceeding the max supported value of 65536")

    try:
        abs_bytes = abs(some_id).to_bytes(64, "big")
    except OverflowError:
        raise ValueError(f"{some_id = }, which seems too large for an id...")
    sign_byte = b"+" if some_id > 0 else b"-"
    bot_bytes = bot_prefix.encode("utf-8")
    some_id_hash = hashlib.md5(sign_byte + abs_bytes + bot_bytes).digest()

    max_length = len(some_id_hash) // 2
    if length > max_length:
        raise ValueError(f"{length = }, but can't exceed {max_length}")

    res = ""
    for i in range(length):
        two_hash_bytes = some_id_hash[2 * i : 2 * (i + 1)]
        char_idx = int.from_bytes(two_hash_bytes, "little") % len(alphabet)
        res += alphabet[char_idx]
    return res


def emoji_hash(some_id: int, bot_prefix: str, length: int = 4) -> str:
    return _pretty_hash_from_alphabet(some_id, bot_prefix, length, alphabet=EMOJI)


def text_hash(some_id: int, bot_prefix: str, length: int = 6) -> str:
    return _pretty_hash_from_alphabet(
        some_id,
        bot_prefix,
        length,
        alphabet=list(string.ascii_letters + string.digits),
    )


# TODO: unused for now, move to telebot library and use to force sequential processing of
# the same-origin updates
class LockRegistry:
    def __init__(self):
        self._lock_by_key: dict[Any, asyncio.Lock] = WeakValueDictionary()

    def get_lock(self, key: Any) -> asyncio.Lock:
        maybe_lock = self._lock_by_key.get(key)
        if maybe_lock is None:
            lock = asyncio.Lock()
            self._lock_by_key[key] = lock
            return lock
        else:
            return maybe_lock


async def send_attachment(
    bot: AsyncTeleBot,
    chat_id: Union[int, str],
    attachment: TelegramAttachment,
    caption: Optional[str] = None,
    remove_metadata: bool = True,
):
    if isinstance(attachment, list) and all(isinstance(att, tg.PhotoSize) for att in attachment):
        return await bot.send_photo(chat_id, photo=attachment[0].file_id, caption=caption)
    elif isinstance(attachment, tg.Document):
        doc_to_send: Union[str, bytes]

        if (attachment.mime_type == "image/jpeg" or attachment.mime_type == "image/png") and remove_metadata:
            doc_to_send = await download_photo_document_and_remove_metadata(bot, attachment)
        else:
            doc_to_send = attachment.file_id

        return await bot.send_document(
            chat_id,
            document=doc_to_send,
            caption=caption,
            visible_file_name=attachment.file_name,
        )
    elif isinstance(attachment, tg.Video):
        return await bot.send_video(chat_id, video=attachment.file_id, caption=caption)
    elif isinstance(attachment, tg.Animation):
        return await bot.send_animation(chat_id, animation=attachment.file_id, caption=caption)
    elif isinstance(attachment, tg.Audio):
        return await bot.send_audio(chat_id, audio=attachment.file_id, caption=caption)
    else:
        raise TypeError(f"Can not send attachment of type: {type(attachment)!r}.")


async def download_photo_document_and_remove_metadata(bot: AsyncTeleBot, document: tg.Document) -> Union[bytes, str]:
    if document.mime_type != "image/jpeg" and document.mime_type != "image/png":
        logger.exception(
            f"Failed to download document and delete metadata from it. Must be jpeg/png document to delete its "
            f"metadata, but got: {document.mime_type!r}. "
        )
        return document.file_id

    try:
        file = await bot.get_file(document.file_id)
        file_content = await bot.download_file(file.file_path)

        image = Image.open(io.BytesIO(file_content))
        buf = io.BytesIO()
        image.save(buf, format=image.format)

        return buf.getvalue()

    except Exception:
        logger.exception(f"Failed to download document and delete metadata from it. Doc type: {document.mime_type}")
        return document.file_id


AsyncFunctionT = TypeVar("AsyncFunctionT", bound=Callable[..., Awaitable])


def restart_on_errors(function: AsyncFunctionT) -> AsyncFunctionT:
    """Decorator to log unexpected errors, primarily in background jobs"""

    @functools.wraps(function)
    async def decorated(*args, **kwargs):
        while True:
            try:
                return await function(*args, **kwargs)
            except Exception:
                logger.exception(f"Unexpected error in {function.__qualname__}, restarting")

    return decorated  # type: ignore
