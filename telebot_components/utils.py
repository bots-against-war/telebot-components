import io
from typing import Any, Optional, Union

from ruamel.yaml import YAML  # type: ignore


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
