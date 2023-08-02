import asyncio
import logging
import re
from datetime import datetime
from io import StringIO
from typing import Optional

from telebot import AsyncTeleBot

from telebot_components.utils import telegram_html_escape


class TelegramAlertsHandler(logging.Handler):
    """Sentry-like logging integration: report all error level logs to a dedicated alert Telegram channel"""

    def __init__(self, bot: AsyncTeleBot, channel_id: int, app_name: Optional[str]) -> None:
        super().__init__(level=logging.ERROR)
        self.app_name = app_name
        self.header = telegram_html_escape(self.app_name) if self.app_name else ""
        self.bot = bot
        self.channel_id = channel_id
        self._tasks: set[asyncio.Task] = set()
        self.formatter = logging.Formatter(fmt="%(name)s: %(message)s\n%(pathname)s:%(lineno)d")

    NOT_LETTERS_RE = re.compile(r"\W+")

    async def _send_error_message(self, message: str):
        try:
            await self.bot.send_message(
                self.channel_id,
                self.header + "\n<pre>" + telegram_html_escape(message) + "</pre>",
                parse_mode="HTML",
                auto_split_message=False,
            )
        except Exception:
            try:
                body = StringIO(initial_value=message)
                filename_raw = message.splitlines()[-1]
                filename = self.NOT_LETTERS_RE.sub("-", filename_raw)
                filename = filename[:40]
                filename = f"{filename}-{datetime.now().isoformat(timespec='seconds')}.txt"
                await self.bot.send_document(
                    self.channel_id,
                    document=body,
                    caption=self.header or None,
                    visible_file_name=filename,
                )
            except Exception as e:
                print(f"Error sending alert to Telegram channel: {e!r}")
                try:
                    await self.bot.send_message(
                        self.channel_id,
                        self.header + "\n\n⚠️ Failed to send alert, see application logs",
                    )
                except Exception:
                    pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self._send_error_message(self.format(record)))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception as e:
            print(f"{self.__class__.__name__}: Unable to emit message, {e!r}")


def configure_alerts(token: str, alerts_channel_id: int, app_name: Optional[str] = None):
    logging.getLogger().addHandler(
        TelegramAlertsHandler(
            bot=AsyncTeleBot(token),
            channel_id=alerts_channel_id,
            app_name=app_name,
        )
    )
