
from dataclasses import dataclass
from typing import Optional

from telebot import AsyncTeleBot


@dataclass
class UserMessageRepliedEvent:
    """Service type with info about user message """

    bot: AsyncTeleBot  # passed around for ease of use
    origin_chat_id: int
    reply_text: str
    reply_has_attachments: bool
    reply_author: Optional[str]
    reply_link: Optional[str]