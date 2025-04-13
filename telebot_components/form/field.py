import asyncio
import copy
import dataclasses
import datetime
import hashlib
import inspect
import logging
import math
import re
from abc import abstractmethod
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from dataclasses import fields as dataclass_fields
from datetime import date, time, tzinfo
from enum import Enum
from hashlib import md5
from typing import (
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Collection,
    Generic,
    Iterable,
    Protocol,
    Type,
    TypeVar,
    cast,
)

from telebot import types as tg
from telebot.callback_data import CallbackData
from telebot.types import constants as tgconst

from telebot_components.form.helpers.calendar_keyboard import (
    CalendarAction,
    CalendarCallbackPayload,
    CalendarKeyboardConfig,
    SelectableDates,
    calendar_keyboard,
)
from telebot_components.form.types import FormBranchCondition
from telebot_components.language import (
    AnyText,
    Language,
    LanguageData,
    MaybeLanguage,
    any_text_to_str,
    is_any_text,
)
from telebot_components.utils import TelegramAttachment, log_errors

logger = logging.getLogger(__name__)

FieldValueT = TypeVar("FieldValueT")


class BadFieldValueError(Exception):
    def __init__(self, msg: AnyText):
        self.msg = msg


# region: config part classes


@dataclass
class NextFieldGetterContext(Generic[FieldValueT]):
    current_field: "FormField"
    current_value: FieldValueT | None
    user: tg.User
    dynamic_data: Any | None
    language: MaybeLanguage

    @property
    def current_value_id(self) -> str | None:
        return self.current_field.value_id(self.current_value) if self.current_value is not None else None


@dataclass
class NextFieldGetter(Generic[FieldValueT]):
    """Wrapper class for next field getter function, used to link form fields together"""

    next_field_name_getter: Callable[[NextFieldGetterContext], str | None | Awaitable[str | None]]
    # used for startup form connectedness validation
    possible_next_field_names: Collection[str | None]

    async def __call__(self, context: NextFieldGetterContext) -> "str | None":
        next_field_name_res = self.next_field_name_getter(context)

        if inspect.isawaitable(next_field_name_res):
            return await next_field_name_res
        else:
            return next_field_name_res  # type: ignore

    @classmethod
    def by_name(cls, name: str | None) -> "NextFieldGetter":
        return NextFieldGetter(lambda _: name, possible_next_field_names=[name])

    @classmethod
    def by_mapping(
        cls,
        value_to_next_field_name: dict[FieldValueT | None, str | None],
        default: str | None,
    ) -> "NextFieldGetter[FieldValueT]":
        return NextFieldGetter(
            lambda context: value_to_next_field_name.get(context.current_value, default),
            possible_next_field_names=(
                [next_field_name for _, next_field_name in value_to_next_field_name.items()] + [default]
            ),
        )

    @classmethod
    def form_end(cls) -> "NextFieldGetter":
        return NextFieldGetter.by_name(None)

    @classmethod
    def from_condition_list(
        cls, conditions: list[tuple[str, FormBranchCondition]], fallback: str | None
    ) -> "NextFieldGetter":
        if not conditions:
            return cls.by_name(fallback)

        def next_field_name_getter(context: NextFieldGetterContext) -> str | None:
            for next_field_name, condition in conditions:
                if (isinstance(condition, str) and context.current_value_id == condition) or (
                    callable(condition)
                    and log_errors(
                        logger,
                        errmsg=f"Error testing condition for possible next field {next_field_name!r}",
                        return_on_error=False,
                    )(condition)(context.current_value_id)
                ):
                    return next_field_name
            return fallback

        return NextFieldGetter(
            next_field_name_getter=next_field_name_getter,
            possible_next_field_names=([next_field_name for next_field_name, _ in conditions] + [fallback]),
        )


@dataclass
class FormFieldResultFormattingOpts(Generic[FieldValueT]):
    """Options specifying how to format field's result to HTML (e.g. telegram message)"""

    descr: AnyText  # used for telegram message formatting
    is_multiline: bool = False
    value_formatter: Callable[[FieldValueT, MaybeLanguage], str] | None = (
        None  # if not specified, field's default formatter is used
    )


@dataclass
class FormFieldResultExportOpts(Generic[FieldValueT]):
    """Options specifying how to format field's result to generic record"""

    column: Any  # usually an enum specifying airtable or Google Sheets column

    value_mapping: dict[FieldValueT, Any] | None = None
    unmapped_value_default: Any | None = None

    value_processor: Callable[[FieldValueT], Any] | None = None

    def __post_init__(self) -> None:
        if (self.value_mapping is not None) and (self.value_processor is not None):
            raise ValueError("Value mapping and value processor are mutually exclusive")


# endregion

# region: data transfer classes


@dataclass
class MessageProcessingContext(Generic[FieldValueT]):
    message: tg.Message
    current_value: FieldValueT | None
    language: MaybeLanguage
    dynamic_data: Any
    logger: logging.Logger


@dataclass
class MessageProcessingResult(Generic[FieldValueT]):
    response_to_user: str | None
    new_field_value: FieldValueT | None
    complete_field: bool
    ask_for_retry: bool = False
    response_reply_markup: tg.ReplyMarkup | None = None
    new_dynamic_data: Any | None = None
    updated_inline_markup: tg.InlineKeyboardMarkup | None = None
    delete_last_message: bool = False


@dataclass
class CallbackQueryProcessingContext(Generic[FieldValueT]):
    callback_payload: str
    user: tg.User
    current_value: FieldValueT | None
    language: MaybeLanguage
    dynamic_data: Any
    logger: logging.Logger


@dataclass
class CallbackQueryProcessingResult(Generic[FieldValueT]):
    response_to_user: str | None
    updated_inline_markup: tg.InlineKeyboardMarkup | None
    complete_field: bool
    new_field_value: FieldValueT | None
    new_dynamic_data: Any | None = None


# endregion

# region: form field base

FormFieldT = TypeVar("FormFieldT", bound="FormField")


@dataclass
class FormField(Generic[FieldValueT]):
    name: str
    required: bool
    query_message: AnyText

    # may contain 1 '{}' for field value
    echo_result_template: AnyText | None = dataclass_field(default=None, kw_only=True)

    next_field_getter: NextFieldGetter[FieldValueT] | None = dataclass_field(default=None, kw_only=True)

    result_formatting_opts: FormFieldResultFormattingOpts | bool | None = dataclass_field(default=None, kw_only=True)
    export_opts: FormFieldResultExportOpts | None = dataclass_field(default=None, kw_only=True)

    def __post_init__(self):
        pass  # future-proof

    def get_next_field_getter(self) -> NextFieldGetter[FieldValueT]:
        if self.next_field_getter is None:
            raise RuntimeError(
                f"{self}: next field getter wasn't properly initialized; "
                + "either specify it directly or wrap the field in the Form to use sequential structure"
            )
        return self.next_field_getter

    def custom_value_type(self) -> Type | None:
        """
        Used for validation of a form's result type (see Form.validate_result_type). In trivial cases
        like PlainTextField field value type is obtained from introspection, but sometimes this is
        impossible (e.g. in MultipleSelectField), and this method is used.

        If your custom FormField subclass has a complex dynamic value type, override this method and
        return this type.
        """
        return None

    async def process_message(self, context: MessageProcessingContext) -> MessageProcessingResult[FieldValueT]:
        try:
            value = self.parse(context.message)
            return MessageProcessingResult(
                response_to_user=self.get_result_message(value, context.language),
                new_field_value=value,
                complete_field=True,
            )
        except BadFieldValueError as error:
            return MessageProcessingResult(
                response_to_user=any_text_to_str(error.msg, context.language),
                new_field_value=None,
                complete_field=False,
                ask_for_retry=True,
            )

    def parse(self, message: tg.Message) -> FieldValueT:
        """
        Simplified interface for common use-case of parsing a single value directly from the message;
        subclasses are free to leave this unimplemented, but in this case they need to
        override process_message
        """
        raise NotImplementedError("FormField cannot be used directly, please use concrete subclasses")

    async def get_query_message(self, user: tg.User, dynamic_data: Any) -> AnyText:
        return self.query_message

    def value_to_str(self, value: FieldValueT, language: MaybeLanguage) -> str:
        """Human-readable string formatting of the value"""
        return self.value_id(value)

    def value_id(self, value: FieldValueT) -> str:
        """Machine-readable string identitier for the value"""
        return str(value)

    def get_result_message(self, value: FieldValueT, language: MaybeLanguage) -> str | None:
        if self.echo_result_template is None:
            return None
        else:
            return any_text_to_str(self.echo_result_template, language).format(self.value_to_str(value, language))

    async def get_reply_markup(
        self,
        language: MaybeLanguage,
        current_value: FieldValueT | None,
        user: tg.User,
        dynamic_data: Any,
    ) -> tg.ReplyMarkup:
        return tg.ReplyKeyboardRemove()

    def texts(self) -> list[AnyText]:
        """Used for build-time validation that all the fields in the form are properly localised.

        Automatically collects AnyText-typed fields using dataclass introspection. Anything else must be added
        via custom_texts() hook.
        """
        res = [self.query_message]
        if self.echo_result_template is not None:
            res.append(self.echo_result_template)
        for field in dataclass_fields(self):
            if field.type == AnyText:
                res.append(getattr(self, field.name))
        res.extend(self.custom_texts())
        return res

    def custom_texts(self) -> list[AnyText]:
        """If subclasses add their own field-related texts (custom error messages,
        button captions or anything else, they must list them here)"""
        return []

    def with_output_opts(
        self: FormFieldT,
        formatting: FormFieldResultFormattingOpts | None = None,
        export: FormFieldResultExportOpts | None = None,
    ) -> FormFieldT:
        """Typed version of dataclasses.replace"""
        return dataclasses.replace(
            self,
            result_formatting_opts=formatting,
            export_opts=export,
        )


# endregion
# region: specific fields


@dataclass
class PlainTextField(FormField[str]):
    empty_text_error_msg: AnyText

    def parse(self, message: tg.Message) -> str:
        text = message.text_content
        if not text:
            raise BadFieldValueError(self.empty_text_error_msg)
        return text


@dataclass
class IntegerField(FormField[int]):
    not_an_integer_error_msg: AnyText

    def parse(self, message: tg.Message) -> int:
        text = message.text_content.strip()
        try:
            return int(text)
        except Exception:
            raise BadFieldValueError(self.not_an_integer_error_msg)


@dataclass
class IntegerListField(FormField[list[int]]):
    not_an_integer_list_error_msg: AnyText

    def parse(self, message: tg.Message) -> list[int]:
        try:
            text = message.text_content.strip()
            numbers = text.split()
            return [int(n) for n in numbers]
        except Exception:
            raise BadFieldValueError(self.not_an_integer_list_error_msg)

    def value_to_str(self, value: list[int], lang: MaybeLanguage) -> str:
        return ", ".join(str(i) for i in value)


@dataclass
class DateField(FormField[date]):
    timezone: tzinfo
    bad_date_format_error_msg: AnyText
    # if specified, the date is validated to be in the future
    cant_be_in_the_past_error_msg: AnyText | None = None

    def parse(self, message: tg.Message) -> date:
        date_parts = message.text_content.strip().split(".")
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_local = now_utc.astimezone(self.timezone)
        today_local = now_local.date()
        try:
            assert 1 <= len(date_parts) <= 3
            day = int(date_parts[0])
            month = int(date_parts[1]) if len(date_parts) > 1 else today_local.month
            year = int(date_parts[2]) if len(date_parts) > 2 else today_local.year
            parsed_date = date(year, month, day)
        except Exception:
            raise BadFieldValueError(self.bad_date_format_error_msg)
        if self.cant_be_in_the_past_error_msg is not None:
            if parsed_date < today_local:
                raise BadFieldValueError(self.cant_be_in_the_past_error_msg)
        return parsed_date

    def custom_texts(self) -> list[AnyText]:
        if self.cant_be_in_the_past_error_msg is not None:
            return [self.cant_be_in_the_past_error_msg]
        else:
            return []

    def value_to_str(self, value: date, lang: MaybeLanguage) -> str:
        return value.strftime("%d.%m.%Y")

    def value_id(self, value: date) -> str:
        return value.isoformat()


@dataclass
class TimeField(FormField[time]):
    bad_time_format_msg: AnyText

    def parse(self, message: tg.Message) -> time:
        try:
            return time.fromisoformat(message.text_content)
        except ValueError:
            raise BadFieldValueError(self.bad_time_format_msg)

    def value_to_str(self, value: time, lang: MaybeLanguage) -> str:
        return value.isoformat(timespec="minutes")

    def value_id(self, value: time) -> str:
        return value.isoformat()


_users_uploading_media_group: set[int] = set()
_media_group_attachments_stash: dict[str, list[TelegramAttachment]] = dict()


@dataclass
class AttachmentsField(FormField[list[TelegramAttachment]]):
    attachments_expected_error_msg: AnyText
    only_one_media_message_allowed_error_msg: AnyText
    bad_attachment_type_error_msg: AnyText

    allowed_attachment_types: set[tgconst.MediaContentType] = dataclass_field(
        default_factory=lambda: {
            tgconst.MediaContentType.photo,
            tgconst.MediaContentType.document,
            tgconst.MediaContentType.video,
            tgconst.MediaContentType.animation,
            tgconst.MediaContentType.audio,
        }
    )

    def __post_init__(self):
        super().__post_init__()
        self.logger = logging.getLogger(f"{__file__}[{self.__class__.__name__}(name={self.name!r})]")

    def get_attachment(self, message: tg.Message) -> TelegramAttachment | None:
        if message.photo is not None:
            return message.photo
        elif message.document is not None:
            return message.document
        elif message.audio is not None:
            return message.audio
        elif message.animation is not None:
            return message.animation
        elif message.video is not None:
            return message.video
        else:
            return None

    def is_attachment_allowed(self, attachment: TelegramAttachment) -> bool:
        if tgconst.MediaContentType.photo in self.allowed_attachment_types:
            if isinstance(attachment, list) and all(isinstance(att, tg.PhotoSize) for att in attachment):
                return True
        if tgconst.MediaContentType.document in self.allowed_attachment_types and isinstance(attachment, tg.Document):
            return True
        if tgconst.MediaContentType.video in self.allowed_attachment_types and isinstance(attachment, tg.Video):
            return True
        if tgconst.MediaContentType.animation in self.allowed_attachment_types and isinstance(attachment, tg.Animation):
            return True
        if tgconst.MediaContentType.audio in self.allowed_attachment_types and isinstance(attachment, tg.Audio):
            return True
        else:
            return False

    def value_to_str(self, value: list[TelegramAttachment], language: MaybeLanguage) -> str:
        return f"{len(value)} attachments"

    def value_id(self, value: list[TelegramAttachment]) -> str:
        hash_ = hashlib.md5()
        for attachment in value:
            file_ids = [ps.file_id for ps in attachment] if isinstance(attachment, list) else [attachment.file_id]
            for file_id in file_ids:
                hash_.update(file_id.encode("utf-8"))
        return hash_.hexdigest()

    async def process_message(
        self,
        context: MessageProcessingContext,
    ) -> MessageProcessingResult[list[TelegramAttachment]]:
        """HACK: we want to process media group, but telegram passes them as separate messages,
        linked only with ID with no info on the total number of items, order or whatever.

        As a workaround, we use a little non-persistent cache internal to field. We store the
        first message, sleep asynchronously for some time and hope that by the time we wake up,
        all other messages in the media group have arrived and are already added to the cache
        """
        message = context.message
        language = context.language
        attachment = self.get_attachment(context.message)
        if attachment is None:
            return MessageProcessingResult(
                response_to_user=any_text_to_str(self.attachments_expected_error_msg, language),
                new_field_value=None,
                complete_field=False,
                ask_for_retry=True,
            )
        media_group_id = message.media_group_id
        self.logger.debug(f"{self.__class__.__name__} got a new media: {media_group_id = } ")

        # single-media message OR the first message in a media group
        if media_group_id is None or media_group_id not in _media_group_attachments_stash:
            if message.from_user.id in _users_uploading_media_group:
                # we're already waiting for messages in a media group
                return MessageProcessingResult(
                    response_to_user=any_text_to_str(self.only_one_media_message_allowed_error_msg, language),
                    new_field_value=None,
                    complete_field=False,
                    ask_for_retry=True,
                )
            if media_group_id is None:  # single media message
                if self.is_attachment_allowed(attachment):
                    return MessageProcessingResult(
                        response_to_user=self.get_result_message([attachment], language),
                        new_field_value=[attachment],
                        complete_field=True,
                    )
                else:
                    return MessageProcessingResult(
                        response_to_user=any_text_to_str(self.bad_attachment_type_error_msg, language),
                        new_field_value=None,
                        complete_field=False,
                        ask_for_retry=True,
                    )
            else:
                # first message in a media group — waiting for the rest of it
                try:
                    _users_uploading_media_group.add(message.from_user.id)
                    _media_group_attachments_stash[media_group_id] = [attachment]
                    self.logger.debug("First attachment in a media group, sleeping to wait for other ones")
                    await asyncio.sleep(1.0)  # waiting for other messages to come in and to be saved in the stash
                finally:
                    _users_uploading_media_group.discard(message.from_user.id)
                    final_attachments = _media_group_attachments_stash.pop(media_group_id)
                    self.logger.debug(f"Woke up, got {len(final_attachments)} attachments in a group")
                if all(self.is_attachment_allowed(att) for att in final_attachments):
                    return MessageProcessingResult(
                        response_to_user=self.get_result_message(final_attachments, language),
                        new_field_value=final_attachments,
                        complete_field=True,
                    )
                else:
                    return MessageProcessingResult(
                        response_to_user=any_text_to_str(self.bad_attachment_type_error_msg, language),
                        new_field_value=None,
                        complete_field=False,
                        ask_for_retry=True,
                    )
        # second or later message in a media group
        else:
            current_value = _media_group_attachments_stash.get(media_group_id)
            if not isinstance(current_value, list):
                self.logger.error(f"Corrupted data in stash: {_media_group_attachments_stash}")
                return MessageProcessingResult(
                    response_to_user="Something went wrong...",
                    new_field_value=None,
                    complete_field=False,
                    ask_for_retry=True,
                )
            else:
                self.logger.debug("Second-or-later attachment in a media group, adding it to stash")
                current_value.append(attachment)
                return MessageProcessingResult(
                    response_to_user=None,
                    new_field_value=None,
                    complete_field=False,
                )


@dataclass
class _EnumDefinedFieldMixin:
    EnumClass: Type[Enum]

    def custom_texts(self) -> list[AnyText]:
        return [option.value for option in self.EnumClass]


@dataclass
class SingleSelectField(_EnumDefinedFieldMixin, FormField[Enum]):
    invalid_enum_value_error_msg: AnyText
    menu_row_width: int = 2

    def custom_value_type(self) -> Type:
        return self.EnumClass

    def match_enum(self, text: str) -> Enum | None:
        for enum in self.EnumClass:
            if isinstance(enum.value, str):
                if text == enum.value:
                    return enum
            elif isinstance(enum.value, dict):
                for _, lang_text in enum.value.items():
                    if lang_text == text:
                        return enum
        return None

    async def get_reply_markup(
        self,
        language: MaybeLanguage,
        current_value: Enum | None,
        user: tg.User,
        dynamic_data: Any,
    ) -> tg.ReplyKeyboardMarkup:
        kbd = tg.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=self.menu_row_width)
        kbd.add(*[tg.KeyboardButton(any_text_to_str(option.value, language)) for option in self.EnumClass])
        return kbd

    def parse(self, message: tg.Message) -> Enum:
        parsed_enum = self.match_enum(message.text_content)
        if parsed_enum is None:
            raise BadFieldValueError(self.invalid_enum_value_error_msg)
        else:
            return parsed_enum

    def value_to_str(self, value: Enum, lang: MaybeLanguage) -> str:
        if is_any_text(value.value):
            return any_text_to_str(value.value, lang)
        else:
            return str(value.value)

    def value_id(self, value: Enum) -> str:
        return value.name


EnumField = SingleSelectField  # backward compatibility


def lower_and_cleanup(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    s = s.strip()
    return s


@dataclass
class SearchableSingleSelectItem:
    button_label: AnyText
    spellings: list[str] = dataclass_field(default_factory=list)
    fuzzy_search_prepoc: Callable[[str], str] = lower_and_cleanup

    def matchable_texts(self) -> Iterable[str]:
        if isinstance(self.button_label, str):
            yield self.button_label
        else:
            yield from self.button_label.values()
        yield from self.spellings

    def matches(self, query: str, exact: bool) -> bool:
        if not query:
            return False
        for t in self.matchable_texts():
            if t == query or (not exact and self.fuzzy_search_prepoc(query) in self.fuzzy_search_prepoc(t)):
                return True
        return False


@dataclass
class SearchableSingleSelectField(_EnumDefinedFieldMixin, FormField[Enum]):
    no_matches_found: AnyText
    choose_from_matches: AnyText
    menu_row_width: int = 2

    @staticmethod
    def _as_item(o: Enum) -> SearchableSingleSelectItem:
        if not isinstance(o.value, SearchableSingleSelectItem):
            raise TypeError(
                "All options in searchable single select field's Enum must have "
                "SearchableSingleSelectOption instance as value"
            )
        return o.value

    def __post_init__(self) -> None:
        super().__post_init__()
        for e in self.EnumClass:
            self._as_item(e)

    def custom_value_type(self) -> Type:
        return self.EnumClass

    def custom_texts(self) -> list[AnyText]:
        return [self._as_item(e).button_label for e in self.EnumClass]

    async def get_reply_markup(
        self,
        language: MaybeLanguage,
        current_value: Enum | None,
        user: tg.User,
        dynamic_data: Any,
    ) -> tg.ReplyMarkup:
        # NOTE: no markup at the start, as in text field; later we set to "search results"
        return tg.ReplyKeyboardRemove()

    def find_matches(self, text: str, exact: bool) -> list[Enum]:
        return [e for e in self.EnumClass if self._as_item(e).matches(text, exact=exact)]

    async def process_message(self, context: MessageProcessingContext) -> MessageProcessingResult[Enum]:
        message = context.message
        language = context.language
        exact_matches = self.find_matches(message.text_content, exact=True)
        if exact_matches:
            if len(exact_matches) > 1:
                logger.warning(f"Multiple exact matches found, will select first one: {exact_matches}")
            return MessageProcessingResult(
                response_to_user=self.get_result_message(exact_matches[0], language),
                new_field_value=exact_matches[0],
                complete_field=True,
            )

        fuzzy_matches = self.find_matches(message.text_content, exact=False)
        if fuzzy_matches:
            select_match_kbd = tg.ReplyKeyboardMarkup(
                resize_keyboard=True,
                one_time_keyboard=True,
                row_width=self.menu_row_width,
            )
            for fm in fuzzy_matches:
                select_match_kbd.add(tg.KeyboardButton(any_text_to_str(self._as_item(fm).button_label, language)))

            return MessageProcessingResult(
                response_to_user=any_text_to_str(self.choose_from_matches, language),
                response_reply_markup=select_match_kbd,
                new_field_value=None,
                complete_field=False,
            )
        else:
            return MessageProcessingResult(
                response_to_user=any_text_to_str(self.no_matches_found, language),
                new_field_value=None,
                complete_field=False,
            )

    def value_to_str(self, value: Enum, lang: MaybeLanguage) -> str:
        item = self._as_item(value)
        if is_any_text(item.button_label):
            return any_text_to_str(item.button_label, lang)
        else:
            return str(item.button_label)

    def value_id(self, value: Enum) -> str:
        return value.name


INLINE_FIELD_CALLBACK_DATA = CallbackData("fieldname", "payload", prefix="inline_field")


@dataclass
class InlineFormField(FormField[FieldValueT]):
    def new_callback_data(self, payload: str) -> str:
        return INLINE_FIELD_CALLBACK_DATA.new(fieldname=self.name, payload=payload)

    async def process_callback_query(
        self, context: CallbackQueryProcessingContext[FieldValueT]
    ) -> CallbackQueryProcessingResult[FieldValueT]:
        raise NotImplementedError("InlineFormField cannot be used directly, please use concrete subclasses")


@dataclass
class StrictlyInlineFormField(InlineFormField[FieldValueT]):
    please_use_inline_menu: AnyText

    async def process_message(self, context: MessageProcessingContext) -> MessageProcessingResult[FieldValueT]:
        return MessageProcessingResult(
            response_to_user=any_text_to_str(self.please_use_inline_menu, context.language),
            new_field_value=None,
            complete_field=False,
            ask_for_retry=True,
        )


@dataclass
class MultipleSelectField(_EnumDefinedFieldMixin, StrictlyInlineFormField[set[Enum]]):
    inline_menu_row_width: int
    options_per_page: int
    finish_field_button_caption: AnyText
    next_page_button_caption: AnyText
    prev_page_button_caption: AnyText
    min_selected_to_finish: int | None = None
    max_selected_to_finish: int | None = None

    OPTION_PAYLOAD_PREFIX: ClassVar[str] = "opt"
    FINISH_FIELD_PAYLOAD: ClassVar[str] = "finish"
    TO_PAGE_PAYLOAD_PREFIX: ClassVar[str] = "topage"
    NOOP_PAYLOAD: ClassVar[str] = "noop"

    def __post_init__(self) -> None:
        super().__post_init__()
        self._option_by_hash = {self.option_hash(o): o for o in self.EnumClass}
        if len(self._option_by_hash) != len(self.EnumClass):
            raise ValueError("Duplicate options detected in the options enum!")

    def custom_value_type(self) -> Type:
        return set[self.EnumClass]  # type: ignore

    def option_hash(self, option: Enum) -> str:
        if isinstance(option.value, str):
            return md5(option.value.encode("utf-8")).hexdigest()[:8]
        elif isinstance(option.value, dict):
            # WARNING: this might be unstable due to dict order
            md5_hash = md5()
            is_hash_initialized = False
            for lang, localization in option.value.items():
                if isinstance(lang, (Language, LanguageData)) and isinstance(localization, str):
                    is_hash_initialized = True
                    md5_hash.update(localization.encode("utf-8"))
            if is_hash_initialized:
                return md5_hash.hexdigest()[:8]
        raise ValueError("Every Enum option must be either a string or a Language->str dict")

    def value_to_str(self, value: set[Enum], language: MaybeLanguage) -> str:
        selected_str = [any_text_to_str(opt.value, language) for opt in value]
        return ", ".join(sorted(selected_str))

    def value_id(self, value: set[Enum]) -> str:
        return ",".join(sorted(opt.name for opt in value))

    @property
    def total_pages(self) -> int:
        return int(math.ceil(len(self.EnumClass) / self.options_per_page))

    async def process_callback_query(
        self, context: CallbackQueryProcessingContext[set[Enum]]
    ) -> CallbackQueryProcessingResult[set[Enum]]:
        current_value = context.current_value or set()
        try:
            if context.callback_payload == self.NOOP_PAYLOAD:
                return CallbackQueryProcessingResult(
                    response_to_user=None,
                    updated_inline_markup=None,
                    complete_field=False,
                    new_field_value=current_value,
                )
            if context.callback_payload == self.FINISH_FIELD_PAYLOAD:
                return CallbackQueryProcessingResult(
                    response_to_user=self.get_result_message(current_value, context.language),
                    updated_inline_markup=None,
                    complete_field=True,
                    new_field_value=current_value,
                )
            elif context.callback_payload.startswith(self.TO_PAGE_PAYLOAD_PREFIX):
                to_page = int(context.callback_payload.removeprefix(self.TO_PAGE_PAYLOAD_PREFIX))
                return CallbackQueryProcessingResult(
                    response_to_user=None,
                    updated_inline_markup=self._get_reply_markup_for_page(
                        language=context.language,
                        current_value=current_value,
                        page=to_page,
                    ),
                    complete_field=False,
                    new_field_value=current_value,
                )
            elif context.callback_payload.startswith(self.OPTION_PAYLOAD_PREFIX):
                option_hash = context.callback_payload.removeprefix(self.OPTION_PAYLOAD_PREFIX)
                selected_option = self._option_by_hash.get(option_hash)
                if selected_option is None:
                    raise RuntimeError(
                        f"Error parsing callback payload {context.callback_payload!r} "
                        + f"as Enum value {list(self.EnumClass)}"
                    )
                new_value = copy.deepcopy(current_value)
                if selected_option in new_value:
                    new_value.remove(selected_option)
                else:
                    new_value.add(selected_option)
                page = list(self.EnumClass).index(selected_option) // self.options_per_page  # type: ignore
                return CallbackQueryProcessingResult(
                    response_to_user=None,
                    updated_inline_markup=self._get_reply_markup_for_page(
                        language=context.language,
                        current_value=new_value,
                        page=page,
                    ),
                    complete_field=False,
                    new_field_value=new_value,
                )
            else:
                raise ValueError(f"Unknown callback payload prefix: {context.callback_payload!r}!")
        except Exception as e:
            context.logger.exception("Unexpected error processing callback query")
            return CallbackQueryProcessingResult(
                response_to_user=f"Something went wrong! Details: {e}",
                updated_inline_markup=None,
                complete_field=False,
                new_field_value=current_value,
            )

    async def get_reply_markup(
        self,
        language: MaybeLanguage,
        current_value: set[Enum] | None,
        user: tg.User,
        dynamic_data: Any,
    ) -> tg.InlineKeyboardMarkup:
        return self._get_reply_markup_for_page(language=language, current_value=current_value, page=0)

    def _get_reply_markup_for_page(
        self,
        language: MaybeLanguage,
        current_value: set[Enum] | None,
        page: int,
    ) -> tg.InlineKeyboardMarkup:
        if current_value is None:
            current_value = set()
        all_options: list[Enum] = list(self.EnumClass)
        page_start_idx = page * self.options_per_page
        page_end_idx = (page + 1) * self.options_per_page
        option_buttons: list[tg.InlineKeyboardButton] = []
        for option in all_options[page_start_idx:page_end_idx]:
            option_text = any_text_to_str(option.value, language)
            if option in current_value:
                button_text = "✅ " + option_text
            else:
                button_text = "⬜ " + option_text
            option_buttons.append(
                tg.InlineKeyboardButton(
                    text=button_text,
                    callback_data=self.new_callback_data(payload=self.OPTION_PAYLOAD_PREFIX + self.option_hash(option)),
                )
            )
        keyboard = tg.InlineKeyboardMarkup()
        keyboard.add(*option_buttons, row_width=self.inline_menu_row_width)
        if self.total_pages > 1:
            noop_button = tg.InlineKeyboardButton(
                text=" ",
                callback_data=self.new_callback_data(payload=self.NOOP_PAYLOAD),
            )
            prev_page_button = tg.InlineKeyboardButton(
                text=any_text_to_str(self.prev_page_button_caption, language),
                callback_data=self.new_callback_data(payload=self.TO_PAGE_PAYLOAD_PREFIX + str(page - 1)),
            )
            next_page_button = tg.InlineKeyboardButton(
                text=any_text_to_str(self.next_page_button_caption, language),
                callback_data=self.new_callback_data(payload=self.TO_PAGE_PAYLOAD_PREFIX + str(page + 1)),
            )
            keyboard.row(
                prev_page_button if page > 0 else noop_button,
                next_page_button if page < self.total_pages - 1 else noop_button,
            )
        n_selected = len(current_value)
        if (self.min_selected_to_finish is None or n_selected >= self.min_selected_to_finish) and (
            self.max_selected_to_finish is None or n_selected <= self.max_selected_to_finish
        ):
            keyboard.row(
                tg.InlineKeyboardButton(
                    text=any_text_to_str(self.finish_field_button_caption, language),
                    callback_data=self.new_callback_data(payload=self.FINISH_FIELD_PAYLOAD),
                )
            )
        return keyboard


@dataclass
class DateMenuField(StrictlyInlineFormField[date]):
    calendar_keyboard_config: CalendarKeyboardConfig = CalendarKeyboardConfig(selectable_dates=SelectableDates.all())

    async def process_callback_query(
        self, context: CallbackQueryProcessingContext[date]
    ) -> CallbackQueryProcessingResult[date]:
        payload = CalendarCallbackPayload.load(context.callback_payload)
        if payload is None:
            raise RuntimeError(f"Failed to parse CalendarCallbackPayload from {context.callback_payload!r}")
        if payload.action is CalendarAction.NOOP:
            return CallbackQueryProcessingResult(
                response_to_user=None,
                updated_inline_markup=None,
                complete_field=False,
                new_field_value=None,
            )
        elif payload.action is CalendarAction.UPDATE:
            return CallbackQueryProcessingResult(
                response_to_user=None,
                updated_inline_markup=self._calendar_keyboard(
                    year=payload.year,
                    month=payload.month,
                    selected_value=context.current_value,
                ),
                complete_field=False,
                new_field_value=None,
            )
        elif payload.action is CalendarAction.SELECT:
            # casts are for type system, runtime validation is performed in CalendarCallbackPayload
            selected_date = date(
                cast(int, payload.year),
                cast(int, payload.month),
                cast(int, payload.day),
            )
            return CallbackQueryProcessingResult(
                response_to_user=(
                    any_text_to_str(self.echo_result_template, context.language).format(selected_date)
                    if self.echo_result_template
                    else None
                ),
                updated_inline_markup=self._calendar_keyboard(
                    selected_date.year,
                    selected_date.month,
                    selected_value=selected_date,
                ),
                complete_field=True,
                new_field_value=selected_date,
            )

    def _calendar_keyboard(
        self, year: int | None, month: int | None, selected_value: date | None
    ) -> tg.InlineKeyboardMarkup:
        return calendar_keyboard(
            year=year,
            month=month,
            new_callback_data_with_payload=self.new_callback_data,
            config=self.calendar_keyboard_config,
            selected_date=selected_value,
        )

    async def get_reply_markup(
        self,
        language: MaybeLanguage,
        current_value: date | None,
        user: tg.User,
        dynamic_data: Any,
    ) -> tg.ReplyMarkup:
        return self._calendar_keyboard(
            year=current_value.year if current_value else None,
            month=current_value.month if current_value else None,
            selected_value=current_value,
        )

    def value_to_str(self, value: date, lang: MaybeLanguage) -> str:
        return value.strftime("%d.%m.%Y")

    def value_id(self, value: date) -> str:
        return value.isoformat()


@dataclass
class DynamicOption:
    id: str
    label: AnyText


DynamicSingleSelectFieldValueT = TypeVar("DynamicSingleSelectFieldValueT", bound="DynamicOption | str")


@dataclass
class _DynamicSingleSelectFieldBase(FormField[DynamicSingleSelectFieldValueT], Generic[DynamicSingleSelectFieldValueT]):
    """
    Like SingleSelectField, but instead of defining options as Enum allows dynamic reply
    options. The options must be passed through form's dynamic_data in the format:

    dynamic_data={
        "dynamic_options": {
            "field-name-1": [DynamicOption(...), ...],
            "field-name-2": [DynamicOption(...), ...],
            ...
        }
    }
    """

    invalid_enum_value_error_msg: AnyText
    menu_row_width: int = 2
    default_options: list[DynamicOption] | None = None

    def parse_dynamic_data(self, dynamic_data: Any) -> list[DynamicOption]:
        try:
            options: list[DynamicOption] = dynamic_data["dynamic_options"][self.name]
            assert isinstance(options, list) and len(options) > 0 and all(isinstance(o, DynamicOption) for o in options)
            res: list[DynamicOption] | None = options
        except Exception:
            res = self.default_options
        if res is None:
            raise RuntimeError("Failed to parse dynamic options, and default options are not set")
        return res

    async def get_reply_markup(
        self,
        language: MaybeLanguage,
        current_value: DynamicSingleSelectFieldValueT | None,
        user: tg.User,
        dynamic_data: Any,
    ) -> tg.ReplyKeyboardMarkup:
        options = self.parse_dynamic_data(dynamic_data)
        kbd = tg.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=self.menu_row_width)
        kbd.add(*[tg.KeyboardButton(any_text_to_str(option.label, language)) for option in options])
        return kbd

    def match_option(self, options: list[DynamicOption], text: str) -> DynamicOption | None:
        for option in options:
            if isinstance(option.label, str):
                if text == option.label:
                    return option
            elif isinstance(option.label, dict):
                for _, lang_text in option.label.items():
                    if lang_text == text:
                        return option
        return None

    def custom_texts(self) -> list[AnyText]:
        return [opt.label for opt in self.default_options or []]

    def value_from_option(self, opt: DynamicOption) -> DynamicSingleSelectFieldValueT:
        raise NotImplementedError()

    async def process_message(
        self, context: MessageProcessingContext
    ) -> MessageProcessingResult[DynamicSingleSelectFieldValueT]:
        options = self.parse_dynamic_data(context.dynamic_data)
        selected_opt = self.match_option(options=options, text=context.message.text_content)
        if selected_opt is None:
            return MessageProcessingResult(
                response_to_user=any_text_to_str(self.invalid_enum_value_error_msg, context.language),
                new_field_value=None,
                complete_field=False,
                ask_for_retry=True,
            )
        value = self.value_from_option(selected_opt)
        return MessageProcessingResult(
            response_to_user=self.get_result_message(value, context.language),
            new_field_value=value,
            complete_field=True,
        )


# this is a legacy backwards-compatible class storing only option ids
# newer code should use DynamicSingleSelectFieldFull
class DynamicSingleSelectField(_DynamicSingleSelectFieldBase[str]):
    def value_from_option(self, opt: DynamicOption) -> str:
        return opt.id


class DynamicSingleSelectFieldFull(_DynamicSingleSelectFieldBase[DynamicOption]):
    def value_from_option(self, opt: DynamicOption) -> DynamicOption:
        return opt

    def value_id(self, value: DynamicOption) -> str:
        return value.id

    def value_to_str(self, value: DynamicOption, language: MaybeLanguage) -> str:
        return any_text_to_str(value.label, language)


class HasLabel(Protocol):
    def label(self) -> str:
        pass


ListInputItem = TypeVar("ListInputItem", bound=HasLabel)


@dataclass
class ListInputField(InlineFormField[list[ListInputItem]], Generic[ListInputItem]):
    """
    Variable length list input with generic item type and list editing,
    akin to chip input (e.g. https://doc.wikimedia.org/codex/latest/components/demos/chip-input.html)

    Subclasses must implement parse_items method that converts a message to a list of items. Item
    can be represented by any class that has a label() method; the output of this method will be
    used on buttons.

    See StringListInputField subclass for a trivial implementation
    """

    finish_field_button_caption: AnyText
    next_page_button_caption: AnyText
    prev_page_button_caption: AnyText

    min_len: int | None = None
    max_len: int | None = None
    max_len_reached_error_msg: AnyText | None = None
    items_per_page: int = 10
    inline_menu_row_width: int = 1
    item_button_caption_prefix = "❌ "

    DELETE_ITEM_PAYLOAD_PREFIX: ClassVar[str] = "delete"
    FINISH_FIELD_PAYLOAD: ClassVar[str] = "finish"
    TO_PAGE_PAYLOAD_PREFIX: ClassVar[str] = "topage"
    NOOP_PAYLOAD: ClassVar[str] = "noop"

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.max_len is not None and self.max_len_reached_error_msg is None:
            raise ValueError("max_len_reached_error_msg must be specified, if max_len is")

    @abstractmethod
    async def parse_items(self, message: tg.Message) -> list[ListInputItem]:
        """Subclasses must override custom item parsing"""
        ...

    async def min_max_items(self, user: tg.User) -> tuple[int | None, int | None]:
        """Subclasses can override this for custom per-user limits"""
        return self.min_len, self.max_len

    async def process_message(
        self,
        context: MessageProcessingContext[list[ListInputItem]],
    ) -> MessageProcessingResult[list[ListInputItem]]:
        try:
            new_items = await self.parse_items(context.message)
        except BadFieldValueError as error:
            return MessageProcessingResult(
                response_to_user=any_text_to_str(error.msg, context.language),
                new_field_value=None,
                complete_field=False,
                ask_for_retry=True,
            )

        new_value = (context.current_value or []) + new_items
        _, max_ = await self.min_max_items(user=context.message.from_user)
        if max_ is not None and self.max_len_reached_error_msg is not None and len(new_value) > max_:
            return MessageProcessingResult(
                response_to_user=any_text_to_str(self.max_len_reached_error_msg, context.language),
                response_reply_markup=tg.ReplyKeyboardRemove(),
                new_field_value=context.current_value,
                complete_field=False,
            )
        else:
            return MessageProcessingResult(
                response_to_user=any_text_to_str(self.query_message, context.language),
                new_field_value=new_value,
                complete_field=False,
                delete_last_message=True,
            )

    async def get_reply_markup(
        self,
        language: MaybeLanguage,
        current_value: list[ListInputItem] | None,
        user: tg.User,
        dynamic_data: Any,
    ) -> tg.InlineKeyboardMarkup:
        return await self._get_reply_markup_for_page(
            language=language,
            current_value=current_value,
            user=user,
            page=0,
        )

    def _total_pages(self, current_value: list[ListInputItem]) -> int:
        return int(math.ceil(len(current_value) / self.items_per_page))

    async def _get_reply_markup_for_page(
        self,
        language: MaybeLanguage,
        current_value: list[ListInputItem] | None,
        user: tg.User,
        page: int,
    ) -> tg.InlineKeyboardMarkup:
        if current_value is None:
            current_value = list()

        page_start_idx = page * self.items_per_page
        page_end_idx = (page + 1) * self.items_per_page
        total_pages = self._total_pages(current_value)

        buttons: list[tg.InlineKeyboardButton] = []
        for idx_on_page, item in enumerate(current_value[page_start_idx:page_end_idx]):
            idx = page_start_idx + idx_on_page
            buttons.append(
                tg.InlineKeyboardButton(
                    text=self.item_button_caption_prefix + item.label(),
                    callback_data=self.new_callback_data(payload=self.DELETE_ITEM_PAYLOAD_PREFIX + str(idx)),
                )
            )
        keyboard = tg.InlineKeyboardMarkup()
        keyboard.add(*buttons, row_width=self.inline_menu_row_width)
        if total_pages > 1:
            noop_button = tg.InlineKeyboardButton(
                text=" ",
                callback_data=self.new_callback_data(payload=self.NOOP_PAYLOAD),
            )
            prev_page_button = tg.InlineKeyboardButton(
                text=any_text_to_str(self.prev_page_button_caption, language),
                callback_data=self.new_callback_data(payload=self.TO_PAGE_PAYLOAD_PREFIX + str(page - 1)),
            )
            next_page_button = tg.InlineKeyboardButton(
                text=any_text_to_str(self.next_page_button_caption, language),
                callback_data=self.new_callback_data(payload=self.TO_PAGE_PAYLOAD_PREFIX + str(page + 1)),
            )
            keyboard.row(
                prev_page_button if page > 0 else noop_button,
                next_page_button if page < total_pages - 1 else noop_button,
            )
        current_len = len(current_value)
        min_, max_ = await self.min_max_items(user)
        if (min_ is None or current_len >= min_) and (max_ is None or current_len <= max_):
            keyboard.row(
                tg.InlineKeyboardButton(
                    text=any_text_to_str(self.finish_field_button_caption, language),
                    callback_data=self.new_callback_data(payload=self.FINISH_FIELD_PAYLOAD),
                )
            )
        return keyboard

    async def process_callback_query(
        self, context: CallbackQueryProcessingContext[list[ListInputItem]]
    ) -> CallbackQueryProcessingResult[list[ListInputItem]]:
        current_value = context.current_value or []
        try:
            if context.callback_payload == self.NOOP_PAYLOAD:
                return CallbackQueryProcessingResult(
                    response_to_user=None,
                    updated_inline_markup=None,
                    complete_field=False,
                    new_field_value=current_value,
                )
            if context.callback_payload == self.FINISH_FIELD_PAYLOAD:
                return CallbackQueryProcessingResult(
                    response_to_user=self.get_result_message(current_value, context.language),
                    updated_inline_markup=None,
                    complete_field=True,
                    new_field_value=current_value,
                )
            elif context.callback_payload.startswith(self.TO_PAGE_PAYLOAD_PREFIX):
                to_page = int(context.callback_payload.removeprefix(self.TO_PAGE_PAYLOAD_PREFIX))
                return CallbackQueryProcessingResult(
                    response_to_user=None,
                    updated_inline_markup=await self._get_reply_markup_for_page(
                        language=context.language,
                        current_value=current_value,
                        user=context.user,
                        page=to_page,
                    ),
                    complete_field=False,
                    new_field_value=current_value,
                )
            elif context.callback_payload.startswith(self.DELETE_ITEM_PAYLOAD_PREFIX):
                idx_to_delete = int(context.callback_payload.removeprefix(self.DELETE_ITEM_PAYLOAD_PREFIX))
                new_value = copy.deepcopy(current_value)
                new_value.pop(idx_to_delete)
                page = idx_to_delete // self.items_per_page
                if page >= self._total_pages(new_value):
                    page -= 1
                return CallbackQueryProcessingResult(
                    response_to_user=None,
                    updated_inline_markup=await self._get_reply_markup_for_page(
                        language=context.language,
                        current_value=new_value,
                        user=context.user,
                        page=page,
                    ),
                    complete_field=False,
                    new_field_value=new_value,
                )
            else:
                raise ValueError(f"Unknown callback payload prefix: {context.callback_payload!r}!")
        except Exception as e:
            context.logger.exception("Unexpected error processing callback query")
            return CallbackQueryProcessingResult(
                response_to_user=f"Something went wrong! Details: {e}",
                updated_inline_markup=None,
                complete_field=False,
                new_field_value=current_value,
            )

    def value_to_str(self, value: list[ListInputItem], language: MaybeLanguage) -> str:
        return ", ".join(v.label() for v in value)


class str_with_label(str):
    def label(self) -> str:
        return self


class StringListInputField(ListInputField[str_with_label]):
    async def parse_items(self, message: tg.Message) -> list[str_with_label]:
        return [str_with_label(s) for s in message.text_content.split()]
