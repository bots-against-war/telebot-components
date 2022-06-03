import asyncio
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from io import BytesIO
from typing import Callable, Optional, TypedDict, cast

import trello  # type: ignore
from markdownify import markdownify  # type: ignore
from telebot import AsyncTeleBot
from telebot import types as tg
from trello import TrelloClient
from typing_extensions import NotRequired

from telebot_components.constants import times
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.category import Category
from telebot_components.stores.generic import KeyValueStore
from telebot_components.utils import telegram_message_url, trim_with_ellipsis

# from bots.utils.category_store import Category
# from bots.utils.redis import redis as default_redis
# from bots.utils.storable import Storable
# from bots.utils.times import LARGE_EXPIRATION_TIME


class TrelloCardData(TypedDict):
    id: str
    category_id: int


class OriginMessageData(TypedDict):
    forwarded_message_id: int
    origin_chat_id: int


class TrelloIntegrationConfigError(RuntimeError):
    pass


@dataclass
class TrelloIntegrationConfig:
    api_key: str
    user_token: str
    organization_name: str
    board_name: str


class TrelloIntegration:
    # to execute blocking calls as async, shared by all TrelloIntegration instances
    thread_pool: Optional[ThreadPoolExecutor] = None

    def __init__(
        self,
        bot: AsyncTeleBot,
        bot_prefix: str,
        redis: RedisInterface,
        admin_chat_id: int,
        config: TrelloIntegrationConfig,
        categories: Optional[list[Category]],
    ):
        self.bot_prefix = bot_prefix
        self.admin_chat_id = admin_chat_id
        self.config = config

        # todo: stores
        self.bot = bot
        self.redis = redis

        self.trello_card_data_for_user = KeyValueStore[TrelloCardData](
            name="trello-card-data",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.MONTH,
        )
        self.origin_message_data_for_trello_card_id = KeyValueStore[OriginMessageData](
            name="origin-message-data",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.MONTH,
        )

        if not categories:
            # is no categories were supplied to the integration, is just create a "virtual" category
            # with the bot prefix as name
            self.categories = [Category(-1, name=self.bot_prefix, button_caption="not used")]
        else:
            self.categories = categories
        self.trello_client = TrelloClient(config.api_key, token=config.user_token)

        if self.thread_pool is None:
            self.thread_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="trello-integration")

        self.logger = logging.getLogger(f"{__name__}.{bot_prefix}")

    async def initialize(self):
        self.logger.info("Initializing Trello integration")
        loop = asyncio.get_event_loop()
        all_organizations = await loop.run_in_executor(self.thread_pool, self.trello_client.list_organizations)
        matching_organizations = [o for o in all_organizations if o.name == self.config.organization_name]
        if not matching_organizations:
            raise TrelloIntegrationConfigError(f"Organization not found: '{self.config.organization_name}'")
        if len(matching_organizations) > 1:
            raise TrelloIntegrationConfigError(
                f"Ambiguous organization name, several organizations found: {matching_organizations}"
            )
        self.organization = matching_organizations[0]

        all_boards = await loop.run_in_executor(self.thread_pool, self.organization.all_boards)
        matching_boards = [b for b in all_boards if b.name == self.config.board_name]
        if not matching_boards:
            raise TrelloIntegrationConfigError(
                f"Board not found in the {self.organization.name} organization: '{self.config.board_name}'"
            )
        if len(matching_boards) > 1:
            raise TrelloIntegrationConfigError(f"Ambiguous board name, several found: {matching_boards}")
        self.board = matching_boards[0]

        lists_on_board = await loop.run_in_executor(self.thread_pool, self.board.all_lists)
        lists_by_name: dict[str, trello.List] = {l.name: l for l in lists_on_board}
        self.lists_by_category_id: dict[int, trello.List] = dict()

        for category in self.categories:
            if category.name in lists_by_name:
                list_ = lists_by_name[category.name]
            else:
                self.logger.info(f"Creating new list '{category.name}' ({id = })")
                list_ = await loop.run_in_executor(self.thread_pool, self.board.add_list, category.name, "bottom")
            self.lists_by_category_id[category.id] = list_

    async def export_message(
        self, origin_message: tg.Message, forwarded_message: tg.Message, category: Optional[Category] = None
    ):
        if category is None:
            category = self.categories[0]  # guaranteed to exist

        if category.id not in self.lists_by_category_id:
            return

        loop = asyncio.get_event_loop()
        user = origin_message.from_user

        attachment_file = None
        attachment_file_content = None
        if origin_message.content_type == "text":
            card_description = safe_markdownify(origin_message.html_text, origin_message.text_content)
        else:
            if origin_message.caption:
                card_description = safe_markdownify(origin_message.html_caption, origin_message.caption)
            else:
                card_description = ""
            try:  # try downloading document to attach it
                doc_or_photo = (
                    cast(tg.Document, origin_message.document) or cast(list[tg.PhotoSize], origin_message.photo)[-1]
                )
                attachment_file = await self.bot.get_file(doc_or_photo.file_id)
                attachment_file_content = await self.bot.download_file(attachment_file.file_path)
                card_description = f"{card_description}\n📎 `{attachment_file.file_path}`"
            except Exception:
                card_description = (
                    f"{card_description}\n"
                    + f"📎 `{origin_message.content_type}` "
                    + "(тип медиа не поддерживается, см. оригинал сообщения)"
                )
        if str(self.admin_chat_id).startswith("-100"):  # i.e. this is a Telegram supergroup and allows direct msg links
            card_description += f"\n[💬]({telegram_message_url(self.admin_chat_id, forwarded_message.id)})"

        existing_trello_card_data = await self.trello_card_data_for_user.load(user.id)
        if existing_trello_card_data is None or existing_trello_card_data["category_id"] != category.id:
            new_card_reason = (
                "no card was found for the user" if existing_trello_card_data is None else "user category has changed"
            )
            self.logger.debug(f"Adding new card because {new_card_reason}")
            trello_list = self.lists_by_category_id[category.id]
            trello_card = await loop.run_in_executor(
                self.thread_pool,
                partial(
                    trello_list.add_card,
                    name="👤: " + trim_with_ellipsis(card_description, target_len=35),
                    desc=card_description,
                    position="top",
                ),
            )
            await self.trello_card_data_for_user.save(
                user.id,
                TrelloCardData(id=trello_card.id, category_id=category.id),
            )
            await self.origin_message_data_for_trello_card_id.save(
                trello_card.id,
                OriginMessageData(
                    forwarded_message_id=forwarded_message.id,
                    origin_chat_id=user.id,
                ),
            )
        else:
            self.logger.debug(f"Updating existing card {existing_trello_card_data}")
            trello_card = await loop.run_in_executor(
                self.thread_pool, self.trello_client.get_card, existing_trello_card_data["id"]
            )
            current_description = trello_card.description
            delimiter = "—" * 20
            new_description = f"{current_description}\n\n{delimiter}\n\n{card_description}"
            await loop.run_in_executor(self.thread_pool, trello_card.set_description, new_description)
            await self.trello_card_data_for_user.touch(user.id)
            await self.origin_message_data_for_trello_card_id.touch(trello_card.id)
        if attachment_file is not None and attachment_file_content is not None:
            self.logger.debug(f"Attaching file to card #{trello_card.id}: {attachment_file.file_path}")
            await loop.run_in_executor(
                self.thread_pool,
                partial(
                    trello_card.attach,
                    name=attachment_file.file_path,
                    file=BytesIO(attachment_file_content),
                ),
            )

    # polling for updates on Trello causes rate limit violations
    # TODO: configure trello webhook

    # def monitor_replies_on_trello(self, interval_sec: float):
    #     """Warning: blocking method, run in threads"""
    #     self.logger.info(f"{self.bot_prefix} monitoring Trello {self.organization.name}.{self.board.name} board")
    #     time.sleep(random.random())  # offsetting in time to avoid clashes
    #     comment_ids_seen_this_iteration = set()
    #     while True:
    #         time.sleep(interval_sec)

    #         try:
    #             last_polling_dt = self.get_last_polling_dt()
    #             self.save_last_polling_dt(datetime.utcnow())
    #             # some updates may be processed twice if made after this save is done but before card
    #             # data is fetched later in the same iteration. This should not happen too often though
    #             comment_ids_seen_prev_iteration = comment_ids_seen_this_iteration
    #             comment_ids_seen_this_iteration = set()

    #             card_ids = self.monitored_card_ids()
    #             for card_id in card_ids:
    #                 origin_message_data = self.get_card_origin_message(card_id)
    #                 if origin_message_data is None:
    #                     logger.error(f"no origin message found for card {card_id}, forgetting")
    #                     self.stop_monitoring_card(card_id)
    #                     continue
    #                 trello_card = self.trello_client.get_card(card_id)
    #                 if trello_card.closed:
    #                     logger.debug(f"card {card_id} is closed, forgetting")
    #                     self.stop_monitoring_card(card_id)
    #                     continue
    #                 for comment in trello_card.comments:
    #                     comment_id = comment["id"]
    #                     if comment_id in comment_ids_seen_prev_iteration:
    #                         continue
    #                     comment_ids_seen_this_iteration.add(comment_id)
    #                     comment_dt = datetime.fromisoformat(comment["date"].strip("Z"))
    #                     if last_polling_dt is not None and comment_dt < last_polling_dt:
    #                         continue
    #                     comment_text: str = comment["data"]["text"]
    #                     if comment_text.startswith("/reply"):
    #                         logger.debug("reply comment found!")
    #                         message_text = comment_text.replace("/reply", "").strip()
    #                         try:
    #                             comment_author = comment["memberCreator"]["fullName"]
    #                         except Exception:
    #                             comment_author = "(unknown user)"
    #                         self.bot.send_message(
    #                             self.admin_chat_id,
    #                             f"{comment_author} via [Trello card]({trello_card.short_url}):\n" + message_text,
    #                             parse_mode="Markdown",
    #                             disable_web_page_preview=True,
    #                             reply_to_message_id=origin_message_data.fwd_msg_id,
    #                         )
    #                         self.bot.send_message(origin_message_data.origin_chat_id, message_text)
    #                         if self.on_message_replied_callback is not None:
    #                             self.on_message_replied_callback(origin_message_data.fwd_msg_id)
    #         except Exception as e:
    #             logger.error(f"Error processing Trello updates: {e}")


def safe_markdownify(html_text: str, fallback_text: str) -> str:
    try:
        return markdownify(html_text)
    except Exception:
        return fallback_text + "\n⚠️ Не удалось отобразить часть текста, оригинал сообщения по ссылке."
