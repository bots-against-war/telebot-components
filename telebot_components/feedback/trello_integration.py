import asyncio
import json
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from io import BytesIO
from typing import Callable, Coroutine, Literal, Optional, TypedDict, Union, cast

import trello  # type: ignore
from aiohttp import web
from markdownify import markdownify  # type: ignore
from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import AuxBotEndpoint
from trello import TrelloClient

from telebot_components.constants import times
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.category import Category
from telebot_components.stores.generic import KeyValueStore
from telebot_components.utils import (
    telegram_html_escape,
    telegram_message_url,
    trim_with_ellipsis,
)

TrelloLabelColor = Literal["yellow", "purple", "blue", "red", "green", "orange", "black", "sky", "pink", "lime"]


class TrelloCardData(TypedDict):
    id: str
    category_name: str


class OriginMessageData(TypedDict):
    forwarded_message_id: int
    origin_chat_id: int


class TrelloWebhookData(TypedDict):
    id: str
    id_model: str
    base_url: str
    path: str


class TrelloLabelData(TypedDict):
    id: str
    name: str
    color: TrelloLabelColor
    id_board: str


@dataclass
class ExportedCardContent:
    description: str
    attachment: Optional[tg.File]
    attachment_content: Optional[bytes]


@dataclass
class MessageRepliedFromTrelloContext:
    forwarded_user_message_id: int
    reply_message_id: int
    origin_chat_id: int


@dataclass
class UnansweredLabelConfig:
    name: str
    color: TrelloLabelColor


OnMessageRepliedFromTrello = Callable[[MessageRepliedFromTrelloContext], Coroutine[None, None, None]]


class TrelloIntegrationCredentialsError(RuntimeError):
    pass


@dataclass
class TrelloIntegrationCredentials:
    user_api_key: str  # not secret, first value in https://trello.com/app-key
    user_token: str  # secret, generated when clicking Token link on the page
    organization_name: str  # just human-readable name
    board_name: str  # again, human-readable board name


class TrelloIntegration:
    # to execute blocking calls as async, shared by all TrelloIntegration instances
    thread_pool: Optional[ThreadPoolExecutor] = None

    def __init__(
        self,
        bot: AsyncTeleBot,
        redis: RedisInterface,
        bot_prefix: str,
        admin_chat_id: int,
        credentials: TrelloIntegrationCredentials,
        reply_with_card_comments: bool,
        unanswered_label: bool = True,
        unanswered_label_config: UnansweredLabelConfig = UnansweredLabelConfig(name="Not answered", color="pink"),
        categories: Optional[list[Category]] = None,
    ):
        self.bot = bot
        self.redis = redis
        self.bot_prefix = bot_prefix
        self.admin_chat_id = admin_chat_id
        self.credentials = credentials
        self.unanswered_label = unanswered_label
        self.reply_with_card_comments = reply_with_card_comments
        self.unanswered_label_config = unanswered_label_config

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
        self.trello_webhook_data_store = KeyValueStore[TrelloWebhookData](
            name="webhook",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=None,
        )
        # generated exactly once on first initialization of the integration,
        # regardless of whether it is used or not
        self.webhook_secret_store = KeyValueStore[str](
            name="webhook-secret",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=None,
        )

        self.trello_label_data = KeyValueStore[TrelloLabelData](
            name="trello-label-data",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=None,
        )

        if not categories:
            # is no categories were supplied to the integration, is just create a "virtual" category
            # with the bot prefix as name
            self.categories = [Category(name=self.bot_prefix, button_caption="not used")]
        else:
            self.categories = categories
        self.trello_client = TrelloClient(self.credentials.user_api_key, token=self.credentials.user_token)

        if self.thread_pool is None:
            self.thread_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="trello-integration")

        self.logger = logging.getLogger(f"{__name__}.{bot_prefix}")

        self.on_message_replied_from_trello: Optional[OnMessageRepliedFromTrello] = None
        self.initialized = False

    def set_on_message_replied_from_trello(self, new: OnMessageRepliedFromTrello):
        self.on_message_replied_from_trello = new

    STORED_WEBHOOK_DATA_KEY = "const-stored-key"
    STORED_WEBHOOK_SECRET_KEY = "const-generated-webhook-secret"
    TRELLO_LABEL_NEW_MESSAGE = "trello-label-new-message"

    async def load_trello_webhook_data(self) -> Optional[TrelloWebhookData]:
        return await self.trello_webhook_data_store.load(self.STORED_WEBHOOK_DATA_KEY)

    async def save_trello_webhook_data(self, webhook: TrelloWebhookData) -> bool:
        return await self.trello_webhook_data_store.save(self.STORED_WEBHOOK_DATA_KEY, webhook)

    async def initialize(self):
        """Must be run on bot runner setup"""
        self.logger.info("Initializing Trello integration")
        loop = asyncio.get_running_loop()
        all_organizations = await loop.run_in_executor(self.thread_pool, self.trello_client.list_organizations)
        matching_organizations = [o for o in all_organizations if o.name == self.credentials.organization_name]
        if not matching_organizations:
            raise TrelloIntegrationCredentialsError(f"Organization not found: '{self.credentials.organization_name}'")
        if len(matching_organizations) > 1:
            raise TrelloIntegrationCredentialsError(
                f"Ambiguous organization name, several organizations found: {matching_organizations}"
            )
        self.organization = matching_organizations[0]

        all_boards = await loop.run_in_executor(self.thread_pool, self.organization.all_boards)
        matching_boards = [b for b in all_boards if b.name == self.credentials.board_name]
        if not matching_boards:
            raise TrelloIntegrationCredentialsError(
                f"Board not found in the {self.organization.name} organization: '{self.credentials.board_name}'"
            )
        if len(matching_boards) > 1:
            raise TrelloIntegrationCredentialsError(f"Ambiguous board name, several found: {matching_boards}")
        self.board = matching_boards[0]

        lists_on_board = await loop.run_in_executor(self.thread_pool, self.board.all_lists)
        lists_by_name: dict[str, trello.List] = {l.name: l for l in lists_on_board}
        self.lists_by_category_name: dict[str, trello.List] = dict()

        for category in self.categories:
            if category.name in lists_by_name:
                list_ = lists_by_name[category.name]
            else:
                self.logger.info(f"Creating new list '{category.name}'")
                list_ = await loop.run_in_executor(self.thread_pool, self.board.add_list, category.name, "bottom")
            self.lists_by_category_name[category.name] = list_

        if not await self.webhook_secret_store.exists(self.STORED_WEBHOOK_SECRET_KEY):
            self.logger.info("Webhook secret not found, generating new one")
            await self.webhook_secret_store.save(self.STORED_WEBHOOK_SECRET_KEY, secrets.token_urlsafe(32))

        if self.unanswered_label:
            await self.ensure_unanswered_label()

        self.initialized = True

    def ensure_initialized(self):
        if not self.initialized:
            raise RuntimeError(
                "TrelloIntegration hasn't been initialized! "
                + "Have you forgot to 'await trello_integration.initialize()' while creating your BotRunner?"
            )

    async def webhook_path(self) -> str:
        self.ensure_initialized()
        webhook_secret = await self.webhook_secret_store.load(self.STORED_WEBHOOK_SECRET_KEY)
        if webhook_secret is None:
            raise RuntimeError("Something went wrong with initialization: webhook secret is not set")
        return f"/trello-webhook/{self.bot_prefix}/{webhook_secret}"

    async def initialize_webhook(self, base_url: str, server_listening_future: asyncio.Future):
        """Must be  as a background job in parallel with webhook app setup"""
        self.ensure_initialized()
        await server_listening_future
        self.logger.info("Server is listening, setting up Trello webhook now")
        loop = asyncio.get_running_loop()
        trello_webhook_data = await self.load_trello_webhook_data()
        if trello_webhook_data is not None:
            if (
                self.reply_with_card_comments
                and trello_webhook_data["id_model"] == self.board.id
                and trello_webhook_data["base_url"] == cast(str, base_url)
            ):
                self.logger.info("Stored Trello webhook data seem fine, running with it")
            else:
                if self.reply_with_card_comments:
                    self.logger.info("Stored Trello webhook data do not match current base URL or model id; recreating")
                else:
                    self.logger.info("Found stored Trello webhook data but replying with comments is off, deleting it")
                try:
                    # in py-trello this implemented as WebHook object method
                    # https://github.com/sarumont/py-trello/blob/c711809a076aee3fc04784e26e6b0b9688229ebd/trello/webhook.py#L18
                    self.trello_client.fetch_json("/webhooks/" + trello_webhook_data["id"], http_method="DELETE")
                except Exception as e:
                    self.logger.info(f"Error deleting existing webhook, assuming it's already deleted: {e}")
                trello_webhook_data = None

        if trello_webhook_data is None:
            if self.reply_with_card_comments:
                webhook_path = await self.webhook_path()
                self.logger.info(f"Creating and storing new trello webhook at {webhook_path}")
                created_webhook = await loop.run_in_executor(
                    self.thread_pool,
                    partial(
                        self.trello_client.create_hook,
                        callback_url=cast(str, base_url) + webhook_path,
                        id_model=self.board.id,
                        desc="telebot_components trello integration webhook",
                    ),
                )
                await self.save_trello_webhook_data(
                    TrelloWebhookData(
                        id=created_webhook.id,
                        id_model=self.board.id,
                        base_url=cast(str, base_url),
                        path=webhook_path,
                    )
                )
        self.initialized = True

    async def webhook_event_handler(self, request: web.Request) -> web.Response:
        try:
            req_json: dict = await request.json()
            webhook_action: dict = req_json["action"]
            if webhook_action["type"] != "commentCard":
                return web.Response()
            webhook_action_data: dict = webhook_action["data"]
            comment_text: str = webhook_action_data["text"]
            if not comment_text.startswith("/reply"):
                return web.Response()
            reply_text = comment_text.removeprefix("/reply").strip()
            commented_card_data = webhook_action_data["card"]
            commented_card_id: str = commented_card_data["id"]
            commented_card_link: str = "https://trello.com/c/" + commented_card_data["shortLink"]
            origin_message_data = await self.origin_message_data_for_trello_card_id.load(commented_card_id)
            if origin_message_data is None:
                return web.Response()
            comment_author: str = webhook_action["memberCreator"]["fullName"]
            reply_message = await self.bot.send_message(
                chat_id=self.admin_chat_id,
                reply_to_message_id=origin_message_data["forwarded_message_id"],
                text=(
                    f'<i>{telegram_html_escape(comment_author)} via <a href="{commented_card_link}">Trello</a></i>\n'
                    + telegram_html_escape(reply_text)
                ),
                parse_mode="HTML",
            )
            await self.bot.send_message(
                chat_id=origin_message_data["origin_chat_id"],
                text=reply_text,
            )
            trello_card = await self.append_card_description(
                card_id=commented_card_id,
                description_appendix=self.add_admin_reply_prefix(reply_text),
                user_id=origin_message_data["origin_chat_id"],
            )
            await self.remove_unanswered_label(trello_card)
            loop = asyncio.get_running_loop()

            def delete_reply_comment():
                try:
                    self.trello_client.fetch_json(
                        f"/cards/{commented_card_id}/actions/{webhook_action['id']}/comments",
                        http_method="DELETE",
                    )
                except Exception as e:
                    self.logger.info(f"Error deleting trello comment: {e}")

            await loop.run_in_executor(self.thread_pool, delete_reply_comment)

            if self.on_message_replied_from_trello is not None:
                await self.on_message_replied_from_trello(
                    MessageRepliedFromTrelloContext(
                        forwarded_user_message_id=origin_message_data["forwarded_message_id"],
                        reply_message_id=reply_message.id,
                        origin_chat_id=origin_message_data["origin_chat_id"],
                    )
                )
        except Exception:
            self.logger.exception("Error processing Trello webhook event")
        return web.Response()

    async def webhook_probe_handler(self, request: web.Request) -> web.Response:
        self.logger.info(f"Webhook probing request from Trello received: {request}")
        return web.Response()

    async def get_webhook_endpoints(self) -> list[AuxBotEndpoint]:
        return [
            AuxBotEndpoint(
                method="POST",
                route=await self.webhook_path(),
                handler=self.webhook_event_handler,
            ),
            AuxBotEndpoint(
                method="HEAD",  # type: ignore
                route=await self.webhook_path(),
                handler=self.webhook_probe_handler,
            ),
        ]

    async def card_content_from_message(self, message: tg.Message, include_attachments: bool) -> ExportedCardContent:
        attachment = None
        attachment_content = None
        if message.content_type == "text":
            description = safe_markdownify(message.html_text, message.text_content)
        else:
            if message.caption:
                description = safe_markdownify(message.html_caption, message.caption)
            else:
                description = ""
            if include_attachments:
                try:  # try downloading document to attach it
                    doc_or_photo: Union[tg.Document, tg.PhotoSize]
                    if message.document is not None:
                        doc_or_photo = message.document
                    elif message.photo is not None:
                        doc_or_photo = message.photo[-1]
                    else:
                        raise RuntimeError("Unsupported")
                    attachment = await self.bot.get_file(doc_or_photo.file_id)
                    attachment_content = await self.bot.download_file(attachment.file_path)
                    description = f"{description}\n📎 `{attachment.file_path}`"
                except Exception:
                    description = (
                        f"{description}\n📎 `{message.content_type}` "
                        + "(вложение не поддерживается, см. оригинал сообщения)"
                    )
            else:
                description = f"{description}\n📎 `{message.content_type}` (см. оригинал сообщения)"
        return ExportedCardContent(
            description=description,
            attachment=attachment,
            attachment_content=attachment_content,
        )

    def add_backlink_if_possible(self, description: str, to_message: tg.Message) -> str:
        # i.e. this is a Telegram supergroup and allows direct msg links
        if str(self.admin_chat_id).startswith("-100"):
            return f"{description}\n[💬]({telegram_message_url(self.admin_chat_id, to_message.id)})"
        else:
            return description

    async def append_card_description(self, card_id: str, description_appendix: str, user_id: int) -> trello.Card:
        loop = asyncio.get_running_loop()
        self.logger.debug(f"Updating existing card {card_id = }")
        trello_card = await loop.run_in_executor(self.thread_pool, self.trello_client.get_card, card_id)
        delimiter = "—" * 20
        updated_card_description = f"{trello_card.description}\n\n{delimiter}\n\n{description_appendix}"
        await loop.run_in_executor(
            self.thread_pool,
            trello_card.set_description,
            updated_card_description,
        )
        await self.trello_card_data_for_user.touch(user_id)
        await self.origin_message_data_for_trello_card_id.touch(trello_card.id)
        return trello_card

    async def export_user_message(
        self,
        user: tg.User,
        forwarded_message: tg.Message,
        category: Optional[Category] = None,
        postprocess_card_description: Callable[[str], str] = lambda s: s,
    ):
        self.ensure_initialized()
        if category is None:
            category = self.categories[0]  # guaranteed to exist

        if category.name not in self.lists_by_category_name:
            return

        loop = asyncio.get_running_loop()

        card_content = await self.card_content_from_message(forwarded_message, include_attachments=True)
        self.logger.debug(f"Card content: {card_content}")
        card_content.description = postprocess_card_description(card_content.description)
        card_content.description = self.add_backlink_if_possible(card_content.description, to_message=forwarded_message)
        card_content.description = "👤: " + card_content.description

        existing_trello_card_data = await self.trello_card_data_for_user.load(user.id)

        trello_card: Optional[trello.Card] = None
        if existing_trello_card_data is not None and existing_trello_card_data["category_name"] == category.name:
            try:
                trello_card = await self.append_card_description(
                    card_id=existing_trello_card_data["id"],
                    description_appendix=card_content.description,
                    user_id=user.id,
                )
            except Exception as e:
                self.logger.info(f"Error appending existing card description, will create new one: {e}")
                new_card_reason = "error occured appending to existing card"
        elif existing_trello_card_data is None:
            new_card_reason = "no saved card data found for the user"
        else:
            new_card_reason = "user category has changed"
        if trello_card is None:
            self.logger.info(f"Adding new card ({new_card_reason})")
            trello_list = self.lists_by_category_name[category.name]
            trello_card = await loop.run_in_executor(
                self.thread_pool,
                partial(
                    trello_list.add_card,
                    name=trim_with_ellipsis(card_content.description, target_len=35),
                    desc=card_content.description,
                    position="top",
                ),
            )
            trello_card = cast(trello.Card, trello_card)
            await self.trello_card_data_for_user.save(
                user.id,
                TrelloCardData(id=trello_card.id, category_name=category.name),
            )
        # storing the last user message in the card as origin
        await self.origin_message_data_for_trello_card_id.save(
            trello_card.id,
            OriginMessageData(
                forwarded_message_id=forwarded_message.id,
                origin_chat_id=user.id,
            ),
        )
        if card_content.attachment is not None and card_content.attachment_content is not None:
            self.logger.debug(f"Attaching file to card #{trello_card.id}: {card_content.attachment.file_path}")
            await loop.run_in_executor(
                self.thread_pool,
                partial(
                    trello_card.attach,
                    name=card_content.attachment.file_path,
                    file=BytesIO(card_content.attachment_content),
                ),
            )
        await self.append_unanswered_label(trello_card)

    def add_admin_reply_prefix(self, description: str) -> str:
        return "🤖: " + description

    async def export_admin_message(self, message: tg.Message, to_user_id: int) -> None:
        trello_card_data = await self.trello_card_data_for_user.load(to_user_id)
        if trello_card_data is None:
            self.logger.info("Not exporting admin message responding to cardless user")
            return
        card_content = await self.card_content_from_message(message, include_attachments=False)
        description = self.add_backlink_if_possible(card_content.description, to_message=message)
        description = self.add_admin_reply_prefix(description)
        try:
            trello_card = await self.append_card_description(
                card_id=trello_card_data["id"],
                description_appendix=description,
                user_id=to_user_id,
            )
            await self.remove_unanswered_label(trello_card)
        except Exception as e:
            self.logger.info(f"Error exporting admin message #{message}, ignoring: {e}")

    async def create_unanswered_label(self) -> trello.Label:
        loop = asyncio.get_running_loop()
        self.logger.info(f"Adding label {self.unanswered_label_config.name} to board {self.board.id}")
        trello_label = await loop.run_in_executor(
            self.thread_pool,
            partial(
                self.board.add_label,
                name=self.unanswered_label_config.name,
                color=self.unanswered_label_config.color,
            ),
        )
        trello_label = cast(trello.Label, trello_label)
        await self.trello_label_data.save(
            self.TRELLO_LABEL_NEW_MESSAGE,
            TrelloLabelData(
                id=trello_label.id, name=trello_label.name, color=trello_label.color, id_board=self.board.id
            ),
        )

        return trello_label

    async def ensure_unanswered_label(self) -> trello.Label:
        existing_label_in_redis = await self.trello_label_data.load(self.TRELLO_LABEL_NEW_MESSAGE)
        if not existing_label_in_redis:
            self.logger.info("Unread label in redis not found, generating new one")
            return await self.create_unanswered_label()

        existing_labels_in_trello = self.board.get_labels(limit=100)
        existing_label_in_trello = [x for x in existing_labels_in_trello if x.id == existing_label_in_redis["id"]]
        if not existing_label_in_trello:
            await self.trello_label_data.drop(self.TRELLO_LABEL_NEW_MESSAGE)
            return await self.create_unanswered_label()

        return existing_label_in_trello[0]

    async def append_unanswered_label(self, trello_card: trello.Card):
        if not self.unanswered_label:
            return True
        loop = asyncio.get_running_loop()
        label = await self.ensure_unanswered_label()
        self.logger.debug(f"Append label {label.id} to card {trello_card.id}")
        await loop.run_in_executor(
            self.thread_pool,
            trello_card.add_label,
            label,
        )

    async def remove_unanswered_label(self, trello_card: trello.Card):
        if not self.unanswered_label:
            return True
        loop = asyncio.get_running_loop()
        label = await self.ensure_unanswered_label()
        self.logger.debug(f"Remove label {label.id} from card {trello_card.id}")
        await loop.run_in_executor(
            self.thread_pool,
            trello_card.remove_label,
            label,
        )


def safe_markdownify(html_text: str, fallback_text: str) -> str:
    try:
        md: str = markdownify(html_text)
        md = md.replace("#", "\#")
        return md
    except Exception:
        return fallback_text + "\n⚠️ Не удалось отобразить часть текста, оригинал сообщения по ссылке."
