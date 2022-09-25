import logging
from typing import Optional

from telebot import AsyncTeleBot, types


async def callback_query_processing_error(
    bot: AsyncTeleBot,
    call: types.CallbackQuery,
    details: str,
    logger: Optional[logging.Logger] = None,
    error_level: bool = True,
):
    if logger is not None:
        if error_level:
            logger.exception(details)
        else:
            logger.info(details, exc_info=True)
    await bot.answer_callback_query(
        call.id,
        f"Server error: {details}. Please refresh menu buttons (e.g. send /start command again).",
        show_alert=True,
    )
