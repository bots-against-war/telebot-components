import asyncio
import logging
import math
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import (
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Optional,
    Protocol,
    TypedDict,
    cast,
)

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.api import ApiHTTPException
from telebot.formatting import hbold
from telebot.runner import AuxBotEndpoint
from telebot.types import constants as tg_constants
from telebot.types.service import FilterFunc
from telebot.util import extract_arguments

from telebot_components.constants import times
from telebot_components.feedback.anti_spam import (
    AntiSpam,
    AntiSpamConfig,
    AntiSpamInterface,
    AntiSpamStatus,
)
from telebot_components.feedback.integration.interface import (
    FeedbackHandlerIntegration,
    FeedbackIntegrationBackgroundContext,
    UserMessageRepliedFromIntegrationEvent,
)
from telebot_components.feedback.integration.trello import TrelloIntegration
from telebot_components.feedback.types import UserMessageRepliedEvent
from telebot_components.form.field import TelegramAttachment
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.banned_users import BannedUsersStore
from telebot_components.stores.category import Category, CategoryStore
from telebot_components.stores.generic import (
    KeyFlagStore,
    KeyListStore,
    KeySetStore,
    KeyValueStore,
)
from telebot_components.stores.language import (
    AnyText,
    Language,
    LanguageStore,
    any_text_to_str,
    vaildate_singlelang_text,
)
from telebot_components.utils import (
    emoji_hash,
    html_link,
    send_attachment,
    telegram_html_escape,
    telegram_message_url,
)


@dataclass
class ServiceMessages:
    # messages to user (may be localized, if used with LanguageStore), please keep in sync with user_facing property
    # e.g. "Спасибо за сообщение, переслано!"
    forwarded_to_admin_ok: Optional[AnyText] = None
    # e.g. "Пожалуйста, сначала выберите тему сообщения, и затем пришлите его заново"
    you_must_select_category: Optional[AnyText] = None
    # e.g. "⚠️ Пожалуйста, не присылайте больше {} сообщений в течение {}!"
    throttling_template: Optional[AnyText] = None

    # messages in admin chat (not localised!)
    # e.g. "Скопировано в чат с пользователь_ницей!"
    copied_to_user_ok: Optional[str] = None
    # e.g. "Невозможно удалить сообщение."
    can_not_delete_message: Optional[str] = None
    # e.g. "Сообщение успешно удалено!"
    deleted_message_ok: Optional[str] = None

    @property
    def user_facing(self) -> list[Optional[AnyText]]:
        return [self.forwarded_to_admin_ok, self.you_must_select_category, self.throttling_template]

    def throttling(self, anti_spam: AntiSpamConfig, language: Optional[Language]) -> str:
        if self.throttling_template is None:
            raise RuntimeError("throttling_template is not set, please use validate_config method on startup")
        return any_text_to_str(self.throttling_template, language).format(
            anti_spam.throttle_after_messages, anti_spam.throttle_duration
        )


class HashtagMessageData(TypedDict):
    """Can't just store message ids as ints because we need to update hashatag messages sometimes!"""

    message_id: int
    hashtags: list[str]  # NOTE: '#' prefixes are not stored here, only hashtag body


class CopiedMessageToUserData(TypedDict):
    origin_chat_id: int
    sent_message_id: int


class AdminChatActionCallback(Protocol):
    async def __call__(self, admin_message: tg.Message, forwarded_message: tg.Message, origin_chat_id: int) -> None:
        ...


@dataclass
class AdminChatAction:
    command: str
    callback: AdminChatActionCallback
    delete_everything_related_to_user_after: bool = False


@dataclass
class FeedbackConfig:
    # if False, message log is sent to PM with the admin that has invoked the '/log' cmd
    message_log_to_admin_chat: bool

    # if True, user's messages are not forwarded until they select a category
    force_category_selection: bool

    # if True, hashtag messages is sent before the actual forwarded message to the admin chat
    hashtags_in_admin_chat: bool

    # if hashtags_in_admin_chat is True, this specifies how often to send hashtag message
    hashtag_message_rarer_than: Optional[timedelta]

    # e.g. 'new' to mark all unanswered messages with '#new'; specify None to disable unanswered
    unanswered_hashtag: Optional[str]

    # when users send a lot of messages, they can grow tired of constant confirmations
    # this parameter allows to limit confirmations to user to one per a specified time
    confirm_forwarded_to_admin_rarer_than: Optional[timedelta] = None

    # custom filters and hooks
    custom_user_message_filter: Optional[Callable[[tg.Message], Coroutine[None, None, bool]]] = None
    before_forwarding: Optional[Callable[[tg.User], Coroutine[None, None, Optional[tg.Message]]]] = None
    after_forwarding: Optional[Callable[[tg.User], Coroutine[None, None, Optional[tg.Message]]]] = None

    # appended to admin chat help under "Other" section; Supports HTML markup
    admin_chat_help_extra: Optional[str] = None

    # if True, user messages are not forwarded but copied to admin chat without any back
    # link to the user account; before the message, user id hash is sent for identification
    full_user_anonymization: bool = False
    # function used to generate user id hash for a particular bot; if full_user_anonymization is False,
    # it is ignored
    user_id_hash_func: Callable[[int, str], str] = emoji_hash

    # how many messages to forward in one go on /log command
    message_log_page_size: int = 30


DUMMY_EXPIRATION_TIME = timedelta(seconds=1312)  # for stores unused based on runtime settings


class FeedbackHandler:
    """
    A class incapsulating the following workflow:
     - people write messages to the bot
     - the bot forwards/copies messages to admin chat
     - admins reply
     - the bot copies messages back to the user
    """

    CONST_KEY = "const"

    def __init__(
        self,
        admin_chat_id: int,
        redis: RedisInterface,
        bot_prefix: str,
        config: FeedbackConfig,
        anti_spam: AntiSpamInterface,
        service_messages: ServiceMessages,
        banned_users_store: Optional[BannedUsersStore] = None,
        language_store: Optional[LanguageStore] = None,
        category_store: Optional[CategoryStore] = None,
        trello_integration: Optional[TrelloIntegration] = None,
        integrations: Optional[list[FeedbackHandlerIntegration]] = None,
        admin_chat_response_actions: Optional[list[AdminChatAction]] = None,
        # specific feedback handler name in case there are several of them;
        # the default (empty string) makes it backwards compatible
        name: str = "",
    ) -> None:
        self.name = name
        bot_prefix = bot_prefix + name
        self.bot_prefix = bot_prefix
        self.logger = logging.getLogger(f"{__name__}[{self.bot_prefix}]")

        self.admin_chat_id = admin_chat_id
        self.config = config

        self.anti_spam = anti_spam
        self.banned_users_store = banned_users_store
        self.language_store = language_store
        self.category_store = category_store

        integrations_ = integrations or []
        if trello_integration is not None:
            self.logger.warning("'trello_integration' argument is deprecated, please use 'integrations' instead")
            integrations_.append(trello_integration)
        self.integrations = integrations_

        self.service_messages = service_messages
        self.validate_service_messages()

        if admin_chat_response_actions is None:
            self.admin_chat_response_actions = []
        else:
            self.admin_chat_response_actions = admin_chat_response_actions

        if self.banned_users_store is not None:
            self.admin_chat_response_actions.append(
                AdminChatAction(
                    command="/ban",
                    callback=self._ban_admin_chat_action,
                    delete_everything_related_to_user_after=True,
                )
            )
        self.admin_chat_response_action_by_command = {aca.command: aca for aca in self.admin_chat_response_actions}

        # === stores used by the handler ===

        # forwarded message in admin chat -> origin chat id (user id)
        self.origin_chat_id_store = KeyValueStore[int](
            name="origin-chat-for-msg",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.YEAR,
        )
        # origin chat id -> set of message ids in admin chat related to user
        # NOTE: stores not only forwarded message ids but also service messages
        # associated with this user (hashtags, custom pre- and post-forwarding messages)
        self.user_related_messages_store = KeySetStore[int](
            name="msgs-from-user",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.MONTH,
        )
        # origin chat id -> list of messages from or to the user
        self.message_log_store = KeyListStore[int](
            name="message-log-with",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.MONTH,
        )
        # [optional] user id -> flag if the "forwarded to admin" confirmation has recently been
        # sent to them
        self.recently_sent_confirmation_flag_store = KeyFlagStore(
            name="recently-sent-confirmation-to",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=self.config.confirm_forwarded_to_admin_rarer_than or DUMMY_EXPIRATION_TIME,
        )
        # [optional] hashtag-related stores
        # user id -> hashtag message data; used to avoid sending hashtags too frequently
        self.recent_hashtag_message_for_user_store = KeyValueStore[HashtagMessageData](
            name="recent-hashtag-message-for",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=self.config.hashtag_message_rarer_than or DUMMY_EXPIRATION_TIME,
        )
        # forwarded msg id -> hashtag message id; used to update hashtag message on forwarded msg action
        self.hashtag_message_for_forwarded_message_store = KeyValueStore[HashtagMessageData](
            name="hashtag-msg-for-fwd",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.MONTH,
        )

        # copied to user ok msg id/admin response msg -> origin chat id (user id) + sent message id;
        # used to undo sent message if needed
        self.copied_to_user_data_store = KeyValueStore[CopiedMessageToUserData](
            name="copied-to-user-ok",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.FIVE_MINUTES,
        )

        self.last_sent_user_id_hash_store = KeyValueStore[str](
            name="last-sent-user-id-hash",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=timedelta(hours=12),
            loader=str,
            dumper=str,
        )

    def validate_service_messages(self):
        if self.config.force_category_selection and self.service_messages.you_must_select_category is None:
            raise ValueError("force_category_selection is True, you_must_select_category message must be set")

        for message in self.service_messages.user_facing:
            if message is None:
                continue
            if self.language_store is None:
                vaildate_singlelang_text(message)
            else:
                self.language_store.validate_multilang(message)

    async def _ban_admin_chat_action(
        self, admin_message: tg.Message, forwarded_message: tg.Message, origin_chat_id: int
    ):
        if self.banned_users_store is not None:
            await self.banned_users_store.ban_user(origin_chat_id)

    async def save_message_from_user(self, author: tg.User, forwarded_message_id: int):
        origin_chat_id = author.id
        await self.origin_chat_id_store.save(forwarded_message_id, origin_chat_id)
        await self.user_related_messages_store.add(origin_chat_id, forwarded_message_id, reset_ttl=True)
        await self.message_log_store.push(origin_chat_id, forwarded_message_id, reset_ttl=True)

    def _admin_help_message(self) -> str:
        paragraphs = [
            "<b>Справка-памятка для админского чата</b>",
            "<i>Сообщение сгенерировано автоматически по команде /help</i>",
        ]
        copies_or_forwards = "пересылает" if not self.config.full_user_anonymization else "копирует"
        paragraphs.append(
            "💬 <i>Основное</i>\n"
            + f"· В этот чат бот {copies_or_forwards} все сообщения (кроме специальных случаев вроде /команд), которые ему "
            + "пишут в личку.\n"
            + (
                (
                    "· Перед скопированным сообщением бот указывает анонимизированный идентификатор пользователь_ницы, "
                    + f"например такой: «{self.config.user_id_hash_func(random.randint(1, 1000), self.bot_prefix)}»\n"
                )
                if self.config.full_user_anonymization
                else ""
            )
            + "· Если в этом чате ответить на сообщение, бот скопирует ответ в чат с пользователь_ницей.\n"
            + "· Чтобы отменить отправку сообщения пользователь_нице - отправьте реплай с командой /undo на ваше "
            + "сообщение или на подтверждение отправки бота (доступно в течение 5 минут)"
        )
        if self.category_store is not None:
            categories_help = (
                "📊 <i>Категории сообщений</i>\n"
                + "· Каждо_й пользователь_нице предлагается выбрать одну из категорий: "
                + ", ".join(
                    [f"<b>{c.name}</b> (# {c.hashtag})" for c in self.category_store.categories if not c.hidden]
                )
                + "\n"
            )
            if self.config.force_category_selection:
                categories_help += "· Выбор категории обязателен для пользователь_ниц."
            else:
                categories_help += "· Выбор категории необязателен, боту можно написать и без него."

            paragraphs.append(categories_help)

        security_help = (
            "🛡️ <i>Защита и безопасность</i>\n"
            + "· Бот никак не выдаёт, кто отвечает пользователь_нице из этого чата. Насколько возможно судить, "
            + "никакого способа взломать бота нет. Однако всё, что вы отвечаете через бота, сразу пересылается человеку "
            + "на другом конце, и отменить отправку можно лишь в течении первых 5 минут, поэтому будьте внимательны!"
        )
        if isinstance(self.anti_spam, AntiSpam):
            security_help += (
                "\n"
                + "· Бот автоматически ограничивает число сообщений, присылаемых ему в единицу времени. "
                + f"Конфигурация на данный момент: не больше {self.anti_spam.config.throttle_after_messages} сообщений за "
                + f"{self.anti_spam.config.throttle_duration}. При необходимости её можно изменять."
            )
        if self.banned_users_store is not None:
            security_help += (
                "\n· Если ответить на пересланное сообщение командой /ban, "
                + "пользователь_ница будет заблокирован_а, а все сообщения от них в чате — удалены"
            )
        paragraphs.append(security_help)

        paragraphs.append(
            "📋 <i>История сообщений</i>\n"
            + "· Через бота может быть неудобно вести несколько длительных переписок — все они мешаются в одном чате.\n"
            + "· Если ответить на пересланное сообщение командой /log, бот перешлёт историю переписки с "
            + "пользователь_ницей "
            + (
                "в этот чат. Можно настроить бота так, чтобы бот пересылал историю не сюда, а "
                + "в диалог с администратор_кой, которая её запросила."
                if self.config.message_log_to_admin_chat
                else "вам в личку (для этого вы должны хотя бы раз что-то ему написать). Можно настроить бота так, "
                + "чтобы чтобы бот пересылал историю сообщений не в личку, а прямо в этот чат."
            )
            + f"\n· По умолчанию бот пересылает первые {self.config.message_log_page_size} сообщений, дальше можно листать "
            + "по страницам: «/log 2», «/log 3», и так далее"
        )

        integration_help_messages = [integration.help_message_section() for integration in self.integrations]
        paragraphs.extend([m for m in integration_help_messages if m])

        if self.config.admin_chat_help_extra:
            paragraphs.append("🪄 <i>Другое</i>\n" + self.config.admin_chat_help_extra)
        return "\n\n".join(paragraphs)

    async def _user_message_filter(self, message: tg.Message) -> bool:
        if self.config.custom_user_message_filter is not None:
            return await self.config.custom_user_message_filter(message)
        else:
            return True

    async def _handle_user_message(
        self,
        bot: AsyncTeleBot,
        user: tg.User,
        # message forwarder returns ids of messages in admin chat and a Message object with the message contents
        message_forwarder: Callable[[], Coroutine[None, None, tuple[list[int], tg.Message]]],
        user_replier: Callable[[str, Optional[tg.ReplyMarkup]], Coroutine[None, None, Any]],
        export_to_integrations: bool = True,
    ) -> Optional[int]:
        if self.banned_users_store is not None and await self.banned_users_store.is_banned(user.id):
            return None
        anti_spam_status = await self.anti_spam.status(user)
        if anti_spam_status is AntiSpamStatus.SOFT_BAN:
            return None

        if self.language_store is not None:
            language = await self.language_store.get_user_language(user)
        else:
            language = None
        if anti_spam_status is AntiSpamStatus.THROTTLING:
            anti_spam = cast(AntiSpam, self.anti_spam)  # only real AntiSpam can return this status
            await user_replier(self.service_messages.throttling(anti_spam.config, language), None)
            return None

        hashtag_msg_data: Optional[HashtagMessageData] = None
        category: Optional[Category] = None
        if self.config.hashtags_in_admin_chat:
            category_hashtag = None  # sentinel
            if self.category_store is not None:
                category = await self.category_store.get_user_category(user)
                if category is None:
                    if self.config.force_category_selection:
                        # see validate_service_messages
                        you_must_select_category = cast(AnyText, self.service_messages.you_must_select_category)
                        await user_replier(
                            any_text_to_str(you_must_select_category, language),
                            await self.category_store.markup_for_user(user),
                        )
                        return None
                else:
                    category_hashtag = category.hashtag

            # see runtime check for the hashtags_in_admin_chat flag and creation of the store
            hashtag_msg_data = await self.recent_hashtag_message_for_user_store.load(user.id)
            if hashtag_msg_data is None or (
                category_hashtag is not None and category_hashtag not in hashtag_msg_data["hashtags"]
            ):
                # sending a new hashtag message
                if self.config.unanswered_hashtag is not None:
                    hashtags = [self.config.unanswered_hashtag]
                else:
                    hashtags = []
                if category_hashtag is not None:
                    hashtags.append(category_hashtag)
                # TODO: multiple categories per user support
                hashtag_msg = await bot.send_message(self.admin_chat_id, _join_hashtags(hashtags))
                await self.user_related_messages_store.add(user.id, hashtag_msg.id, reset_ttl=False)
                hashtag_msg_data = HashtagMessageData(message_id=hashtag_msg.id, hashtags=hashtags)
                await self.recent_hashtag_message_for_user_store.save(user.id, hashtag_msg_data)

        preforwarded_msg = None
        if self.config.before_forwarding is not None:
            preforwarded_msg = await self.config.before_forwarding(user)
            if isinstance(preforwarded_msg, tg.Message):
                await self.save_message_from_user(user, preforwarded_msg.id)

        admin_chat_forwarded_msg_ids, user_content_message = await message_forwarder()
        for admin_chat_forwarded_msg_id in admin_chat_forwarded_msg_ids:
            await self.save_message_from_user(user, admin_chat_forwarded_msg_id)
        postforwarded_msg = None
        if self.config.after_forwarding is not None:
            postforwarded_msg = await self.config.after_forwarding(user)
            if isinstance(postforwarded_msg, tg.Message):
                await self.save_message_from_user(user, postforwarded_msg.id)

        if self.config.hashtags_in_admin_chat and hashtag_msg_data is not None:
            for admin_chat_forwarded_msg_id in admin_chat_forwarded_msg_ids:
                await self.hashtag_message_for_forwarded_message_store.save(
                    admin_chat_forwarded_msg_id, hashtag_msg_data
                )

        if self.service_messages.forwarded_to_admin_ok is not None and (
            self.config.confirm_forwarded_to_admin_rarer_than is None
            or not await self.recently_sent_confirmation_flag_store.is_flag_set(user.id)
        ):
            await user_replier(
                any_text_to_str(self.service_messages.forwarded_to_admin_ok, language),
                None,
            )
            if self.config.confirm_forwarded_to_admin_rarer_than is not None:
                await self.recently_sent_confirmation_flag_store.set_flag(user.id)

        # HACK: we are assuming that the list of forwarded message ids contains "service" ids first and the main message last,
        # so here we use (and return) the last one
        if not admin_chat_forwarded_msg_ids:
            return None
        main_admin_chat_forwarded_msg_id = admin_chat_forwarded_msg_ids[-1]
        if export_to_integrations:
            if category is None and self.category_store is not None:
                # NOTE: category may already be loaded, if so -- reusing the value here
                category = await self.category_store.get_user_category(user)

            # HACK: the code above treats user and content message as separate entities for legacy reasons, but
            #       integrations do not; for simpler interface, they expect to receive a single Message object
            #       with all the info to facilitate that, here we shamelessly patch the message objects to hide
            #       all the mess
            message = user_content_message
            message.from_user = user
            if preforwarded_msg is not None:
                message.text = "[pre]: " + preforwarded_msg.text_content + "\n\n" + message.text_content
            if postforwarded_msg is not None:
                message.text = message.text_content + "\n\n" + "[post]: " + postforwarded_msg.text_content

            await asyncio.gather(
                *[
                    integration.handle_user_message(
                        message=message,
                        admin_chat_message_id=main_admin_chat_forwarded_msg_id,
                        category=category,
                        bot=bot,
                    )
                    for integration in self.integrations
                ]
            )
        return main_admin_chat_forwarded_msg_id

    async def _send_user_id_hash_message(self, bot: AsyncTeleBot, user_id: int) -> Optional[int]:
        user_id_hash = self.config.user_id_hash_func(user_id, self.bot_prefix)
        last_sent_user_id_hash = await self.last_sent_user_id_hash_store.load(self.CONST_KEY)
        if last_sent_user_id_hash is None or last_sent_user_id_hash != user_id_hash:
            user_id_hash_msg = await bot.send_message(self.admin_chat_id, user_id_hash)
            await self.last_sent_user_id_hash_store.save(self.CONST_KEY, user_id_hash)
            return user_id_hash_msg.id
        else:
            return None

    async def emulate_user_message(
        self,
        bot: AsyncTeleBot,
        user: tg.User,
        text: str,
        attachment: Optional[TelegramAttachment] = None,
        no_response: bool = False,
        export_to_trello: bool = True,
        remove_exif_data: bool = True,
        send_user_id_hash_message: bool = False,
        **send_message_kwargs,
    ) -> Optional[int]:
        """Sometimes we want FeedbackHandler to act like the user has sent us a message, but without actually
        a message there (they might have pressed a button or interacted with the bot in some other way). This
        method can be used in such cases.

        If the message has been successfully sent to the admin chat, this method returns its id.
        """

        async def message_forwarder() -> tuple[list[int], tg.Message]:
            if send_user_id_hash_message:
                admin_chat_message_ids = [await self._send_user_id_hash_message(bot, user.id)]
            else:
                admin_chat_message_ids = []
            if attachment is None:
                sent_msg = await bot.send_message(self.admin_chat_id, text=text, **send_message_kwargs)
            else:
                sent_msg = await send_attachment(bot, self.admin_chat_id, attachment, text, remove_exif_data)
            admin_chat_message_ids.append(sent_msg.id)
            return [mid for mid in admin_chat_message_ids if mid is not None], sent_msg

        async def user_replier(text: str, reply_markup: Optional[tg.ReplyMarkup]) -> Optional[tg.Message]:
            if no_response:
                return None
            else:
                return await bot.send_message(user.id, text=text, reply_markup=reply_markup)

        return await self._handle_user_message(
            bot=bot,
            user=user,
            message_forwarder=message_forwarder,
            user_replier=user_replier,
            export_to_integrations=export_to_trello,
        )

    async def setup(self, bot: AsyncTeleBot):
        @bot.message_handler(
            func=cast(FilterFunc, self._user_message_filter),
            chat_types=[tg_constants.ChatType.private],
            content_types=list(tg_constants.MediaContentType),
            priority=-100,  # lower priority to process the rest of the handlers first
        )
        async def user_to_bot(message: tg.Message):
            async def message_forwarder() -> tuple[list[int], tg.Message]:
                if self.config.full_user_anonymization:
                    user_id_msg_id = await self._send_user_id_hash_message(bot, message.from_user.id)
                    copied_message_id = await bot.copy_message(
                        chat_id=self.admin_chat_id,
                        from_chat_id=message.chat.id,
                        message_id=message.id,
                    )
                    maybe_message_ids = [user_id_msg_id, copied_message_id.message_id]
                    return [mid for mid in maybe_message_ids if mid is not None], message
                else:
                    forwarded_message = await bot.forward_message(
                        self.admin_chat_id, from_chat_id=message.chat.id, message_id=message.id
                    )
                    return [forwarded_message.id], forwarded_message

            async def user_replier(text: str, reply_markup: Optional[tg.ReplyMarkup]) -> tg.Message:
                return await bot.reply_to(message, text, reply_markup=reply_markup)

            await self._handle_user_message(
                bot=bot,
                user=message.from_user,
                message_forwarder=message_forwarder,
                user_replier=user_replier,
            )

        await self.setup_admin_chat_handlers(bot)
        for integration in self.integrations:
            await integration.setup(bot)

    async def aux_endpoints(self) -> list[AuxBotEndpoint]:
        return sum(await i.aux_endpoints() for i in self.integrations) or []

    def background_jobs(
        self,
        base_url: Optional[str],
        server_listening_future: Optional[asyncio.Future[None]],
    ) -> list[Coroutine[None, None, None]]:
        return [
            i.background_job(FeedbackIntegrationBackgroundContext(base_url, server_listening_future))
            for i in self.integrations
        ]

    async def _remove_unanswered_hashtag(self, bot: AsyncTeleBot, message_id: int):
        if self.hashtag_message_for_forwarded_message_store is None:
            return
        hashtag_message_data = await self.hashtag_message_for_forwarded_message_store.load(message_id)
        if hashtag_message_data is None:
            return
        if self.config.unanswered_hashtag is None:
            return
        if self.config.unanswered_hashtag not in hashtag_message_data["hashtags"]:
            return
        hashtag_message_data["hashtags"].remove(self.config.unanswered_hashtag)
        try:
            if hashtag_message_data["hashtags"]:
                await bot.edit_message_text(
                    message_id=hashtag_message_data["message_id"],
                    chat_id=self.admin_chat_id,
                    text=_join_hashtags(hashtag_message_data["hashtags"]),
                )
            else:
                await bot.delete_message(chat_id=self.admin_chat_id, message_id=hashtag_message_data["message_id"])
        except Exception as e:
            # when replying on a message in a group that has already been responded to,
            # telegram API returns and error if there's nothing to change
            self.logger.info(f"Error updating hashtag message: {e}")
            pass
        finally:
            await self.hashtag_message_for_forwarded_message_store.save(message_id, hashtag_message_data)

    async def message_replied_from_integration_callback(
        self,
        event: UserMessageRepliedFromIntegrationEvent,
        *,
        notify_integrations: bool = True,
    ) -> None:
        self.logger.debug(f"Message replied from integration: {event!r}")
        if self.config.hashtags_in_admin_chat:
            await self._remove_unanswered_hashtag(event.bot, event.main_admin_chat_message_id)

        integration_name = telegram_html_escape(event.integration.name())
        cloned_reply_message = await event.bot.send_message(
            chat_id=self.admin_chat_id,
            reply_to_message_id=event.main_admin_chat_message_id,
            text=(
                "💬 "
                + hbold(telegram_html_escape(event.reply_author or "<unknown admin>"), escape=False)
                + " via "
                + (html_link(event.reply_link, integration_name) if event.reply_link else integration_name)
                + (("\n\n" + event.reply_text) if event.reply_text else "")
                + ("\n\n📎 attachment" if event.reply_has_attachments else "")
            ),
            parse_mode="HTML",
        )

        await self.message_log_store.push(event.origin_chat_id, cloned_reply_message.id)

        if notify_integrations:
            # do not notify integration about its own replies
            integrations_to_notify = [i for i in self.integrations if not i is event.integration]
            self.logger.debug(f"Notifying integrations: {[i.name() for i in integrations_to_notify]}")
            await asyncio.gather(
                *[integration.handle_user_message_replied_elsewhere(event) for integration in integrations_to_notify]
            )
        else:
            self.logger.debug(f"Will not notify integrations")

    async def setup_admin_chat_handlers(
        self,
        bot: AsyncTeleBot,
        on_admin_reply_to_bot: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        @bot.message_handler(chat_id=[self.admin_chat_id], commands=["help"])
        async def admin_chat_help(message: tg.Message):
            await bot.reply_to(
                message,
                self._admin_help_message(),
                disable_web_page_preview=True,
                disable_notification=True,
                parse_mode="HTML",
            )

        for integration in self.integrations:
            integration.register_message_replied_callback(self.message_replied_from_integration_callback)

        @bot.message_handler(
            chat_id=[self.admin_chat_id],
            is_reply=True,
            commands=["undo"],
        )
        async def admin_undo_forwarded_message(message: tg.Message):
            replied_to_message = message.reply_to_message
            if replied_to_message is None:
                return
            copied_message_data = await self.copied_to_user_data_store.load(replied_to_message.id)
            if copied_message_data is None:
                if self.service_messages.can_not_delete_message is not None:
                    await bot.reply_to(message, self.service_messages.can_not_delete_message)
                return
            origin_chat_id = copied_message_data["origin_chat_id"]
            sent_message_id = copied_message_data["sent_message_id"]
            try:
                await bot.delete_message(origin_chat_id, sent_message_id)
                if self.service_messages.deleted_message_ok is not None:
                    await bot.reply_to(message, self.service_messages.deleted_message_ok)
                await self.copied_to_user_data_store.drop(replied_to_message.id)
            except Exception as e:
                self.logger.exception("error deleting message")
                if self.service_messages.can_not_delete_message is not None:
                    await bot.reply_to(message, self.service_messages.can_not_delete_message)

        @bot.message_handler(
            chat_id=[self.admin_chat_id],
            is_reply=True,
            content_types=list(tg_constants.MediaContentType),
        )
        async def admin_to_bot(message: tg.Message):
            try:
                forwarded_msg = message.reply_to_message
                if forwarded_msg is None:
                    return
                origin_chat_id = await self.origin_chat_id_store.load(forwarded_msg.id)
                if origin_chat_id is None:
                    return

                if message.text is not None and message.text.startswith("/"):
                    # admin chat commands
                    if message.text in self.admin_chat_response_action_by_command:
                        admin_chat_action = self.admin_chat_response_action_by_command[message.text]
                        await admin_chat_action.callback(message, forwarded_msg, origin_chat_id)
                        if admin_chat_action.delete_everything_related_to_user_after:
                            user_related_message_ids = await self.user_related_messages_store.all(origin_chat_id)
                            user_related_message_ids.add(message.id)
                            for message_id in user_related_message_ids:
                                try:
                                    await bot.delete_message(self.admin_chat_id, message_id)
                                    await self.origin_chat_id_store.drop(message_id)
                                except Exception:
                                    pass
                            await self.user_related_messages_store.drop(origin_chat_id)
                            await self.message_log_store.drop(origin_chat_id)
                    elif message.text_content.startswith("/log"):
                        try:
                            page_str = extract_arguments(message.text_content) or "1"
                            page = int(page_str)
                            if page > 0:
                                page -= 1  # one based to zero based
                        except Exception:
                            await bot.reply_to(
                                message, f"Bad command, expected format is '/log' or '/log <page number>'"
                            )
                            return
                        log_message_ids = await self.message_log_store.all(origin_chat_id)
                        total_pages = int(math.ceil(len(log_message_ids) / self.config.message_log_page_size))
                        if page < 0:
                            page = page % total_pages  # wrapping so that -1 = last, -2 = second to last, etc
                        start_idx = self.config.message_log_page_size * page
                        end_idx = self.config.message_log_page_size * (page + 1)
                        log_message_ids_page = log_message_ids[start_idx:end_idx]
                        self.logger.info(
                            f"Forwarding log page {page} / {total_pages} (from {message.text_content!r}) received for origin chat id "
                            + f"{origin_chat_id}, total messages: {len(log_message_ids)}, on current page: {len(log_message_ids_page)}"
                        )
                        if not log_message_ids_page:
                            if page == 0:
                                await bot.reply_to(message, "Message log with this user is not available :(")
                            else:
                                await bot.reply_to(
                                    message,
                                    f"Only {len(log_message_ids)} messages are available in log, not enough messages for page {page}",
                                )
                            return
                        log_destination_chat_id = (
                            self.admin_chat_id if self.config.message_log_to_admin_chat else message.from_user.id
                        )
                        await bot.send_message(
                            chat_id=log_destination_chat_id,
                            text=f"📜 Log page {page + 1} / {total_pages}",
                        )
                        for message_id in log_message_ids_page:
                            try:
                                log_message = await bot.forward_message(
                                    chat_id=log_destination_chat_id,
                                    from_chat_id=self.admin_chat_id,
                                    message_id=message_id,
                                )
                                if self.config.message_log_to_admin_chat:
                                    # to be able to reply to them as to normal forwarded messages...
                                    await self.origin_chat_id_store.save(log_message.id, origin_chat_id)
                                    # ... and to delete them in case of user ban
                                    await self.user_related_messages_store.add(origin_chat_id, log_message.id)
                                await asyncio.sleep(0.5)  # soft rate limit prevention
                            except Exception:
                                self.logger.exception(
                                    "Error sending message as part of /log command, continuing; "
                                    + f"{page = }; {total_pages = }"
                                )
                        await bot.send_message(
                            chat_id=log_destination_chat_id,
                            text=(
                                f"⬆️ Log page {page + 1} / {total_pages}"
                                + (f"\nNext: <code>/log {page + 2}</code>" if page + 1 < total_pages else "")
                            ),
                            parse_mode="HTML",
                        )
                    else:
                        available_commands = list(self.admin_chat_response_action_by_command.keys()) + ["/log"]
                        await bot.reply_to(
                            message,
                            f"Invalid admin chat command: {message.text}; available commands are: "
                            + ", ".join(available_commands),
                        )
                else:
                    # actual response to the user
                    try:
                        copied_message_id = await bot.copy_message(
                            chat_id=origin_chat_id, from_chat_id=self.admin_chat_id, message_id=message.id
                        )
                    except ApiHTTPException as e:
                        # this is normal and most likely means that user has blocked the bot
                        self.logger.info(f"Error copying message to user chat. {e!r}")
                        await bot.reply_to(message, str(e))
                        return
                    await self.message_log_store.push(origin_chat_id, message.id)

                    await self.copied_to_user_data_store.save(
                        message.id,
                        CopiedMessageToUserData(
                            origin_chat_id=origin_chat_id, sent_message_id=int(copied_message_id.message_id)
                        ),
                    )
                    if self.service_messages.copied_to_user_ok is not None:
                        copied_to_user_ok_message = await bot.reply_to(message, self.service_messages.copied_to_user_ok)
                        await self.copied_to_user_data_store.save(
                            copied_to_user_ok_message.id,
                            CopiedMessageToUserData(
                                origin_chat_id=origin_chat_id, sent_message_id=int(copied_message_id.message_id)
                            ),
                        )

                    if self.config.hashtags_in_admin_chat:
                        await self._remove_unanswered_hashtag(bot, forwarded_msg.id)
                    has_attachments = message.content_type != "text"
                    await asyncio.gather(
                        *[
                            integration.handle_user_message_replied_elsewhere(
                                UserMessageRepliedEvent(
                                    bot=bot,
                                    origin_chat_id=origin_chat_id,
                                    reply_text=(
                                        (message.html_text if not has_attachments else message.html_caption) or ""
                                    ),
                                    reply_has_attachments=has_attachments,
                                    reply_author=message.from_user.first_name,
                                    reply_link=telegram_message_url(self.admin_chat_id, message.id),
                                    main_admin_chat_message_id=forwarded_msg.id,
                                )
                            )
                            for integration in self.integrations
                        ]
                    )
            except Exception as e:
                await bot.reply_to(message, f"Something went wrong! {e}")
                self.logger.exception(f"Unexpected error while replying to forwarded msg")


def _join_hashtags(hashtags: list[str]) -> str:
    return " ".join(["#" + h for h in hashtags])
