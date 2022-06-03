import json
import logging
from dataclasses import asdict, is_dataclass
from typing import Any, Optional, Type

from telebot import AsyncTeleBot, types


async def callback_query_processing_error(
    bot: AsyncTeleBot,
    call: types.CallbackQuery,
    details: str,
    logger: Optional[logging.Logger] = None,
):
    if logger is not None:
        logger.exception(details)
    await bot.answer_callback_query(call.id, f"Server error: {details} :(", show_alert=True)
