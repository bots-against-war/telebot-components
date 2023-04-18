import asyncio
import logging
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from io import BytesIO
from typing import Any, Awaitable, Callable, Literal, Optional, TypedDict, Union, cast

import trello  # type: ignore
from aiohttp import web
from markdownify import markdownify  # type: ignore
from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import AuxBotEndpoint
from trello import TrelloClient

from telebot_components.constants import times
from telebot_components.feedback.integration.interface import (
    FeedbackHandlerIntegration,
    FeedbackIntegrationBackgroundContext,
    UserMessageRepliedFromIntegrationEvent,
)
from telebot_components.feedback.types import UserMessageRepliedEvent
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.category import Category
from telebot_components.stores.generic import KeyValueStore
from telebot_components.utils import (
    emoji_hash,
    html_link,
    markdown_link,
    telegram_message_url,
    trim_with_ellipsis,
)
from telebot_components.utils.strings import strip_markdown

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
class CardContent:
    description: str
    title: str
    attachment: Optional[tg.File] = None
    attachment_content: Optional[bytes] = None


@dataclass(frozen=True)
class UnansweredLabelConfig:
    name: str
    color: TrelloLabelColor


class TrelloIntegrationCredentialsError(RuntimeError):
    pass


@dataclass
class TrelloIntegrationCredentials:
    user_api_key: str  # not secret, first value in https://trello.com/app-key
    user_token: str  # secret, generated when clicking Token link on the page

    # legacy name-based board lookup
    organization_name: Optional[str] = None  # just a human-readable name
    board_name: Optional[str] = None  # again, human-readable board name

    # Trello board url has form https://trello.com/b/<board-id>/some-human-readable-name
    board_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.board_id is not None:
            if not (self.organization_name is None and self.board_name is None):
                raise ValueError(
                    "Legacy Trello board lookup config (organization and board name) "
                    "can't be used together with board id"
                )
        else:
            if self.organization_name is None or self.board_name is None:
                raise ValueError(
                    "At least one board lookup name must be specified: board id or organization and board name (legacy)"
                )


class TrelloIntegration(FeedbackHandlerIntegration):
    # to execute blocking calls as async, shared by all TrelloIntegration instances
    thread_pool: Optional[ThreadPoolExecutor] = None

    STORED_WEBHOOK_DATA_KEY = "const-stored-key"
    STORED_WEBHOOK_SECRET_KEY = "const-generated-webhook-secret"
    TRELLO_LABEL_NEW_MESSAGE = "trello-label-new-message"

    # https://community.atlassian.com/t5/Trello-questions/Is-there-a-text-limit-in-the-quot-Description-quot-field-of/qaq-p/838401#M6890
    # actually, the limit is 16384, but we set it a bit lower to leave some space for the "next card" link
    MAX_CARD_DESCRIPTION_LENGTH = 16200

    def __init__(
        self,
        redis: RedisInterface,
        bot_prefix: str,
        credentials: TrelloIntegrationCredentials,
        reply_with_card_comments: bool,
        unanswered_label: bool = True,
        unanswered_label_config: UnansweredLabelConfig = UnansweredLabelConfig(name="Not answered", color="pink"),
        categories: Optional[list[Category]] = None,
        # used to sign trello cards with anonymized user id hash (unique for a user and a bot prefix)
        #                        user id, bot prefix
        user_id_hash_func: Callable[[int, str], str] = emoji_hash,
        # if set to False, exported messages do not have a backlink to the admin chat
        admin_chat_backlink: bool = True,
    ):
        self._bot: Optional[AsyncTeleBot] = None
        self.redis = redis
        self.bot_prefix = bot_prefix
        self.credentials = credentials
        self.unanswered_label = unanswered_label
        self.reply_with_card_comments = reply_with_card_comments
        self.unanswered_label_config = unanswered_label_config
        self.user_id_hash_func = user_id_hash_func
        self.admin_chat_backlink = admin_chat_backlink

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
            self.implicit_categories = True
            self.categories = [Category(name=self.bot_prefix, button_caption="not used")]
        else:
            self.implicit_categories = False
            self.categories = categories
        self.trello_client = TrelloClient(self.credentials.user_api_key, token=self.credentials.user_token)

        if self.thread_pool is None:
            self.thread_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="trello-integration")

        self.logger = logging.getLogger(f"{__name__}[{bot_prefix}]")

        self.initialized = False

    def name(self) -> str:
        return "Trello"

    @property
    def bot(self) -> AsyncTeleBot:
        if self._bot is None:
            raise RuntimeError("TrelloIntegration was not properly initialized: self._bot is not set")
        return self._bot

    def help_message_section(self) -> str:
        trello_help = "🗂️ <i>Интеграция с Trello</i>\n"
        trello_help += (
            f'· Помимо чата сообщения выгружаются на {html_link(self.board.url, "доску Trello")} '
            + "в списки: "
            + ", ".join(f"<b>{lst.name}</b>" for lst in self.lists_by_category_name.values())
            + "\n"
            "· В карточки переносятся присланные в бот фотографии и документы. Для каждого сообщения доступна "
            + "обратная ссылка на этот чат."
        )
        if self.reply_with_card_comments:
            trello_help += (
                "\n"
                + "· Через комментарии к карточке можно ответить на сообщение в боте: "
                + "комментарий «/reply текст ответа» значит, что бот отправит «текст ответа» в чат с "
                + "пользователь_ницей, а также напишет уведомление сюда."
            )
        return trello_help

    async def load_trello_webhook_data(self) -> Optional[TrelloWebhookData]:
        return await self.trello_webhook_data_store.load(self.STORED_WEBHOOK_DATA_KEY)

    async def save_trello_webhook_data(self, webhook: TrelloWebhookData) -> bool:
        return await self.trello_webhook_data_store.save(self.STORED_WEBHOOK_DATA_KEY, webhook)

    async def setup(self, bot: AsyncTeleBot) -> None:
        self.logger.info("Setting up Trello integration")
        self._bot = bot
        loop = asyncio.get_running_loop()

        if self.credentials.board_id is not None:
            self.board = await loop.run_in_executor(
                self.thread_pool, self.trello_client.get_board, self.credentials.board_id
            )
        else:
            self.logger.info("Looking up target board by organization and board names")
            if self.credentials.organization_name is None or self.credentials.board_name is None:
                raise ValueError("Invalid credentials")
            all_organizations = await loop.run_in_executor(self.thread_pool, self.trello_client.list_organizations)
            matching_organizations = [o for o in all_organizations if o.name == self.credentials.organization_name]
            if not matching_organizations:
                raise TrelloIntegrationCredentialsError(
                    f"Organization not found: '{self.credentials.organization_name}'"
                )
            if len(matching_organizations) > 1:
                raise TrelloIntegrationCredentialsError(
                    f"Ambiguous organization name, several organizations found: {matching_organizations}"
                )
            organization = matching_organizations[0]

            all_boards = await loop.run_in_executor(self.thread_pool, organization.all_boards)
            matching_boards = [b for b in all_boards if b.name == self.credentials.board_name]
            if not matching_boards:
                raise TrelloIntegrationCredentialsError(
                    f"Board not found in the {organization.name!r} organization: {self.credentials.board_name!r}"
                )
            if len(matching_boards) > 1:
                raise TrelloIntegrationCredentialsError(f"Ambiguous board name, several found: {matching_boards}")
            self.board = matching_boards[0]

        lists_on_board = await loop.run_in_executor(self.thread_pool, self.board.all_lists)
        lists_by_name: dict[str, trello.List] = {lst.name: lst for lst in lists_on_board}
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

    async def background_job(self, context: FeedbackIntegrationBackgroundContext) -> Any:
        self.ensure_initialized()
        if context.server_listening is not None:
            await context.server_listening
        self.logger.info("Server is listening, setting up Trello webhook now")
        loop = asyncio.get_running_loop()
        trello_webhook_data = await self.load_trello_webhook_data()
        if trello_webhook_data is not None:
            if (
                self.reply_with_card_comments
                and trello_webhook_data["id_model"] == self.board.id
                and trello_webhook_data["base_url"] == context.base_url
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
                if context.server_listening is None or context.base_url is None:
                    raise RuntimeError(
                        "Error initializing Trello integration: reply_with_card_comments option is set to True, "
                        + "but HTTP server related data is not passed to initializing background job"
                    )
                webhook_path = await self.webhook_path()
                self.logger.info(f"Creating and storing new trello webhook at {webhook_path}")
                created_webhook = await loop.run_in_executor(
                    self.thread_pool,
                    partial(
                        self.trello_client.create_hook,
                        callback_url=context.base_url + webhook_path,
                        id_model=self.board.id,
                        desc="telebot_components trello integration webhook",
                    ),
                )
                await self.save_trello_webhook_data(
                    TrelloWebhookData(
                        id=created_webhook.id,
                        id_model=self.board.id,
                        base_url=context.base_url,
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
            origin_message_data = await self.origin_message_data_for_trello_card_id.load(commented_card_id)
            if origin_message_data is None:
                return web.Response()
            user_id = origin_message_data["origin_chat_id"]
            await self.bot.send_message(chat_id=user_id, text=reply_text)  # the actual answer to the user
            description = self._add_admin_reply_prefix(reply_text)
            trello_card = await self._append_card_content(
                card_id=commented_card_id,
                content=CardContent(
                    description=description,
                    title=self._title_with_user_hash(user_id, description),
                ),
                user_id=user_id,
                old_trello_card_callback=self.remove_unanswered_label,
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

            if self.message_replied_callback is not None:
                await self.message_replied_callback(
                    UserMessageRepliedFromIntegrationEvent(
                        bot=self.bot,
                        origin_chat_id=origin_message_data["origin_chat_id"],
                        reply_author=webhook_action["memberCreator"]["fullName"],
                        reply_text=reply_text,
                        reply_has_attachments=False,  # Trello integration does not support sending attachments yet
                        reply_link=trello_card_url(commented_card_data["shortLink"]),
                        integration=self,
                        main_admin_chat_message_id=origin_message_data["forwarded_message_id"],
                    )
                )
        except Exception:
            self.logger.exception("Error processing Trello webhook event")
        finally:
            return web.Response()

    async def webhook_probe_handler(self, request: web.Request) -> web.Response:
        self.logger.info(f"Webhook probing request from Trello: {request}")
        return web.Response()

    async def aux_endpoints(self) -> list[AuxBotEndpoint]:
        return [
            AuxBotEndpoint(
                method="POST",
                route=await self.webhook_path(),
                handler=self.webhook_event_handler,
            ),
            AuxBotEndpoint(
                method="HEAD",
                route=await self.webhook_path(),
                handler=self.webhook_probe_handler,
            ),
        ]

    get_webhook_endpoints = aux_endpoints  # backwards compatibility

    async def _card_content_from_message(
        self, message: tg.Message, user_id: int, include_attachments: bool
    ) -> CardContent:
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
                    media: Union[tg.Document, tg.PhotoSize, tg.Video, tg.Audio]
                    if message.document is not None:
                        media = message.document
                    elif message.photo is not None:
                        media = message.photo[-1]
                    elif message.video is not None:
                        media = message.video
                    elif message.audio is not None:
                        media = message.audio
                    else:
                        raise RuntimeError("Media type is unsupported")
                    attachment = await self.bot.get_file(media.file_id)
                    attachment_content = await self.bot.download_file(attachment.file_path)
                    description = f"{description}\n📎 `{attachment.file_path}`"
                except Exception:
                    description = (
                        f"{description}\n📎 `{message.content_type}` "
                        + "(вложение не поддерживается, см. оригинал сообщения)"
                    )
            else:
                description = f"{description}\n📎 `{message.content_type}` (см. оригинал сообщения)"
        return CardContent(
            description=description,
            title=self._title_with_user_hash(user_id, description),
            attachment=attachment,
            attachment_content=attachment_content,
        )

    def _add_admin_reply_link(self, description: str, reply_link: str) -> str:
        return f"{description}\n" + markdown_link(reply_link, "💬")

    async def _append_card_content(
        self,
        card_id: str,
        content: CardContent,
        user_id: int,
        old_trello_card_callback: Optional[Callable[[trello.Card], Awaitable[Any]]] = None,
    ) -> trello.Card:
        """
        Fetch an existing card description, add an appendix and update the description. Returns Card object.

        On card overflow, create a new card with a description appendix, adding "previous card" and "next card" links.
        In this case returns Card object for the new card. If some additional processing is needed on the old card,
        old_trello_card_callback can be used.
        """
        loop = asyncio.get_running_loop()
        self.logger.debug(f"Updating existing card {card_id = }")
        trello_card = await loop.run_in_executor(self.thread_pool, self.trello_client.get_card, card_id)

        new_description = concatenate_card_text_sections(trello_card.description, content.description)
        if len(new_description) < self.MAX_CARD_DESCRIPTION_LENGTH:
            await loop.run_in_executor(
                self.thread_pool,
                trello_card.set_description,
                new_description,
            )
            await self.trello_card_data_for_user.touch(user_id)
            await self.origin_message_data_for_trello_card_id.touch(trello_card.id)
            return trello_card
        else:
            self.logger.info("Seems like the card is overflown, trying to create a new one")
            old_trello_card = trello_card
            new_trello_card = await loop.run_in_executor(
                self.thread_pool,
                partial(
                    old_trello_card.trello_list.add_card,
                    name=content.title,
                    desc=concatenate_card_text_sections(
                        markdown_link(old_trello_card.short_url, "⬅️ previous card ⬅️"),
                        content.description,
                    ),
                    position="top",
                ),
            )
            try:
                await loop.run_in_executor(
                    self.thread_pool,
                    old_trello_card.set_description,
                    concatenate_card_text_sections(
                        old_trello_card.description,
                        markdown_link(new_trello_card.short_url, "➡️ next card ➡️"),
                    ),
                )
            except Exception:
                self.logger.info("Can't add forwardlink to the old card, ignoring", exc_info=True)

            old_trello_card_data = await self.trello_card_data_for_user.load(user_id)
            if old_trello_card_data is not None:
                await self.trello_card_data_for_user.save(
                    user_id,
                    TrelloCardData(
                        id=new_trello_card.id,
                        category_name=old_trello_card_data["category_name"],
                    ),
                )
            old_trello_card_origin_message = await self.origin_message_data_for_trello_card_id.load(old_trello_card.id)
            if old_trello_card_origin_message is not None:
                await self.origin_message_data_for_trello_card_id.save(
                    new_trello_card.id, old_trello_card_origin_message
                )
            if old_trello_card_callback is not None:
                await old_trello_card_callback(old_trello_card)
            return new_trello_card

    async def handle_user_message(
        self,
        admin_chat_message: tg.Message,
        user: tg.User,
        user_message: Optional[tg.Message],
        category: Optional[Category],
        bot: AsyncTeleBot,
    ) -> None:
        self.ensure_initialized()
        if category is None:
            if self.implicit_categories:
                category = self.categories[0]  # guaranteed to exist
            else:
                # if the categories are explicit, but the message to export has none,
                # it means that the category has not been selected by the user and we
                # have no place to put their message
                return

        if category.name not in self.lists_by_category_name:
            return

        loop = asyncio.get_running_loop()

        card_content = await self._card_content_from_message(admin_chat_message, user.id, include_attachments=True)
        self.logger.debug(f"Card content: {card_content}")
        if self.admin_chat_backlink and str(admin_chat_message.chat.id).startswith("-100"):
            card_content.description = self._add_admin_reply_link(
                card_content.description,
                reply_link=telegram_message_url(admin_chat_message.chat.id, admin_chat_message.id),
            )
        card_content.description = "👤: " + card_content.description

        existing_trello_card_data = await self.trello_card_data_for_user.load(user.id)

        trello_card: Optional[trello.Card] = None
        if existing_trello_card_data is not None and existing_trello_card_data["category_name"] == category.name:
            try:
                trello_card = await self._append_card_content(
                    card_id=existing_trello_card_data["id"],
                    content=card_content,
                    user_id=user.id,
                )
            except Exception:
                self.logger.exception("Unexpected error appending card content, will try creating a new one")
                new_card_reason = "error occured appending to existing card"
        elif existing_trello_card_data is None:
            new_card_reason = "no saved card data found for the user"
        else:
            new_card_reason = "user category has changed"

        if trello_card is None:
            self.logger.info(f"Creating a new card ({new_card_reason})")
            trello_list = self.lists_by_category_name[category.name]
            trello_card = await loop.run_in_executor(
                self.thread_pool,
                partial(
                    trello_list.add_card,
                    name=card_content.title,
                    desc=card_content.description,
                    position="top",
                ),
            )
            trello_card = cast(trello.Card, trello_card)

            # storing the newly created card as the "active" card for the user
            await self.trello_card_data_for_user.save(
                user.id,
                TrelloCardData(id=trello_card.id, category_name=category.name),
            )

        # storing the last user message exported to the card as the origin for the card (will be replied to)
        await self.origin_message_data_for_trello_card_id.save(
            trello_card.id,
            OriginMessageData(
                forwarded_message_id=admin_chat_message.id,
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

    def _add_admin_reply_prefix(self, description: str) -> str:
        return "🤖: " + description

    async def handle_user_message_replied_elsewhere(self, event: UserMessageRepliedEvent) -> None:
        trello_card_data = await self.trello_card_data_for_user.load(event.origin_chat_id)
        if trello_card_data is None:
            self.logger.info("Not exporting admin message as it responds to a cardless user")
            return
        description = safe_markdownify(event.reply_text, event.reply_text)
        if event.reply_has_attachments:
            description = f"{description}\n📎 `(см. оригинал сообщения)"
        if self.admin_chat_backlink and event.reply_link is not None:
            description = self._add_admin_reply_link(description, reply_link=event.reply_link)
        description = self._add_admin_reply_prefix(description)
        try:
            trello_card = await self._append_card_content(
                card_id=trello_card_data["id"],
                content=CardContent(
                    description=description,
                    title=self._title_with_user_hash(event.origin_chat_id, description),
                    attachment=None,
                    attachment_content=None,
                ),
                user_id=event.origin_chat_id,
                old_trello_card_callback=self.remove_unanswered_label,
            )
            await self.remove_unanswered_label(trello_card)
        except Exception:
            self.logger.exception(f"Error exporting user message replied event, ignoring: {event}")

    def _title_with_user_hash(self, user_id: int, description: str) -> str:
        title = description
        title = strip_markdown(title)
        title = title.strip()
        title = re.sub(r"\s+", " ", title)
        title = trim_with_ellipsis(title, target_len=40)
        user_hash_prefix = self.user_id_hash_func(user_id, self.bot_prefix)
        return f"{user_hash_prefix}: {title}"

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
            self.logger.info("Unanswered label not found in storage, generating new one")
            return await self.create_unanswered_label()

        existing_labels_in_trello = self.board.get_labels(limit=100)
        existing_label_in_trello = [x for x in existing_labels_in_trello if x.id == existing_label_in_redis["id"]]
        if not existing_label_in_trello:
            await self.trello_label_data.drop(self.TRELLO_LABEL_NEW_MESSAGE)
            return await self.create_unanswered_label()

        return existing_label_in_trello[0]

    async def append_unanswered_label(self, trello_card: trello.Card) -> None:
        if not self.unanswered_label:
            return
        loop = asyncio.get_running_loop()
        label = await self.ensure_unanswered_label()
        self.logger.debug(f"Append label {label.id} to card {trello_card.id}")
        try:
            await loop.run_in_executor(
                self.thread_pool,
                trello_card.add_label,
                label,
            )
        except trello.ResourceUnavailable:
            self.logger.debug(f"Error appending label {label.id} to card {trello_card.id}, ignoring", exc_info=True)

    async def remove_unanswered_label(self, trello_card: trello.Card) -> None:
        if not self.unanswered_label:
            return
        loop = asyncio.get_running_loop()
        label = await self.ensure_unanswered_label()
        self.logger.debug(f"Remove label {label.id} from card {trello_card.id}")
        try:
            await loop.run_in_executor(
                self.thread_pool,
                trello_card.remove_label,
                label,
            )
        except trello.ResourceUnavailable:
            self.logger.debug(f"Error removing label {label.id} from card {trello_card.id}, ignoring", exc_info=True)


def safe_markdownify(html_text: str, fallback_text: str) -> str:
    try:
        md: str = markdownify(html_text)
        md = md.replace("#", r"\#")
        return md
    except Exception:
        return fallback_text + "\n⚠️ Не удалось отобразить часть текста, оригинал сообщения по ссылке."


def concatenate_card_text_sections(first: str, second: str) -> str:
    if not first:
        return second
    elif not second:
        return first
    else:
        delimiter = "—" * 20
        return f"{first}\n\n{delimiter}\n\n{second}"


def trello_card_url(short_link: str) -> str:
    return "https://trello.com/c/" + short_link
