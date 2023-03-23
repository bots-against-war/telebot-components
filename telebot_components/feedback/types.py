from dataclasses import dataclass
from typing import Optional

from telebot import AsyncTeleBot


@dataclass
class UserMessageRepliedEvent:
    """Service type holding information about user message being replied"""

    bot: AsyncTeleBot  # passed around for ease of use
    origin_chat_id: int
    reply_text: str  # may contain Telegram-compatible HTML markup
    reply_has_attachments: bool
    reply_author: Optional[str]
    reply_link: Optional[str]
    main_admin_chat_message_id: int
