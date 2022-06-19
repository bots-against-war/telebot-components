import datetime
import logging
from dataclasses import dataclass, fields
from datetime import date, tzinfo
from enum import Enum
from typing import Callable, ClassVar, Dict, Generic, Optional, Type, TypeVar

from telebot import types as tg
from telebot.callback_data import CallbackData

from telebot_components.stores.language import AnyText, MaybeLanguage, any_text_to_str

logger = logging.getLogger(__name__)

FieldValueT = TypeVar("FieldValueT")


class BadFieldValueError(Exception):
    def __init__(self, msg: AnyText):
        self.msg = msg


@dataclass
class NextFieldGetter:
    """Service class to forward-reference the next field in a form"""

    next_field_name_getter: Callable[[tg.User, Optional[FieldValueT]], Optional[str]]
    # used for startup form connectedness validation
    possible_next_field_names: list[Optional[str]]
    # filled on Form object initialization
    fields_by_name: Optional[Dict[str, "FormField"]] = None

    def get_next_field(self, user: tg.User, value: Optional[FieldValueT]) -> Optional["FormField"]:
        if self.fields_by_name is None:
            raise RuntimeError(
                "Next field getter hasn't been properly initialized, did you forget to call bind_form_fields?"
            )
        next_field_name = self.next_field_name_getter(user, value)
        if next_field_name is None:
            return None
        else:
            return self.fields_by_name[next_field_name]

    @classmethod
    def by_name(cls, name: str) -> "NextFieldGetter":
        return NextFieldGetter(lambda u, v: name, possible_next_field_names=[name])

    @classmethod
    def by_mapping(
        cls, value_to_next_field_name: Dict[Optional[FieldValueT], Optional[str]], default: Optional[str]
    ) -> "NextFieldGetter":
        possible_next_field_names = [next_field_name for v, next_field_name in value_to_next_field_name.items()]
        possible_next_field_names.append(default)
        return NextFieldGetter(
            lambda u, v: value_to_next_field_name.get(v, default), possible_next_field_names=possible_next_field_names
        )

    @classmethod
    def form_end(cls) -> "NextFieldGetter":
        return NextFieldGetter(lambda u, v: None, possible_next_field_names=[None])


@dataclass
class MessageProcessingResult(Generic[FieldValueT]):
    response_to_user: Optional[str]
    parsed_value: Optional[FieldValueT]


@dataclass
class FormField(Generic[FieldValueT]):
    name: str
    required: bool
    query_message: AnyText
    echo_result_template: Optional[AnyText]  # should contain 1 '{}' for field value
    next_field_getter: NextFieldGetter

    def process_message(self, message: tg.Message, language: MaybeLanguage) -> MessageProcessingResult[FieldValueT]:
        try:
            value = self.parse(message)
            return MessageProcessingResult(
                response_to_user=self.get_result_message(value, language),
                parsed_value=value,
            )
        except BadFieldValueError as error:
            return MessageProcessingResult(
                response_to_user=any_text_to_str(error.msg, language),
                parsed_value=None,
            )

    async def get_query_message(self, user: tg.User) -> AnyText:
        return self.query_message

    def value_to_str(self, value: FieldValueT, language: MaybeLanguage) -> str:
        return str(value)

    def get_result_message(self, value: FieldValueT, language: MaybeLanguage) -> Optional[str]:
        if self.echo_result_template is None:
            return None
        else:
            return any_text_to_str(self.echo_result_template, language).format(self.value_to_str(value, language))

    def get_reply_markup(self, language: MaybeLanguage, current_value: Optional[FieldValueT] = None) -> tg.ReplyMarkup:
        return tg.ReplyKeyboardRemove()

    # NOTE: not using abstractmethod here because of https://github.com/python/mypy/issues/5374
    def parse(self, message: tg.Message) -> FieldValueT:
        raise NotImplementedError("FormField cannot be used directly, please use concrete subclasses")

    def texts(self) -> list[AnyText]:
        """Used for build-time validation that all the fields in the form are properly localised.

        Automatically collects AnyText-typed fields using dataclass introspection. Anything else must be added
        via custom_texts() hook.
        """
        res = [self.query_message]
        if self.echo_result_template is not None:
            res.append(self.echo_result_template)
        for field in fields(self):
            if field.type == AnyText:
                res.append(getattr(self, field.name))
        res.extend(self.custom_texts())
        return res

    def custom_texts(self) -> list[AnyText]:
        """If subclasses add their own field-related texts (custom error messages,
        button captions or anything else, they must list them here)"""
        return []


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
class DateField(FormField[date]):
    timezone: tzinfo
    bad_date_format_error_msg: AnyText
    # if specified, the date is checked to be in the future
    cant_be_in_the_past_error_msg: Optional[AnyText] = None

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


@dataclass
class _EnumDefinedFieldMixin:
    EnumClass: Type[Enum]

    def custom_texts(self) -> list[AnyText]:
        return [option.value for option in self.EnumClass]

    def parse_enum(self, text: str) -> Optional[Enum]:
        for enum in self.EnumClass:
            for _, lang_text in enum.value.items():
                if lang_text == text:
                    return enum
        return None


@dataclass
class SingleSelectField(_EnumDefinedFieldMixin, FormField[Enum]):
    invalid_enum_value_error_msg: AnyText
    menu_row_width: int = 2

    def value_to_str(self, value: Enum, language: MaybeLanguage) -> str:
        return any_text_to_str(value.value, language)

    def get_reply_markup(
        self, language: MaybeLanguage, current_value: Optional[FieldValueT] = None
    ) -> tg.ReplyKeyboardMarkup:
        kbd = tg.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=self.menu_row_width)
        for option in self.EnumClass:
            kbd.add(tg.KeyboardButton(any_text_to_str(option.value, language)))
        return kbd

    def parse(self, message: tg.Message) -> Enum:
        parsed_enum = self.parse_enum(message.text_content)
        if parsed_enum is None:
            raise BadFieldValueError(self.invalid_enum_value_error_msg)
        else:
            return parsed_enum


EnumField = SingleSelectField  # backward compatibility


INLINE_FIELD_CALLBACK_DATA = CallbackData("fieldname", "payload", prefix="inline-form")


@dataclass
class CallbackProcessingResult(Generic[FieldValueT]):
    response_to_user: Optional[str]
    update_inline_markup: bool
    complete_field: bool
    new_field_value: FieldValueT


@dataclass
class InlineFormField(FormField[FieldValueT]):
    def new_callback_data(self, payload: str) -> str:
        return INLINE_FIELD_CALLBACK_DATA.new(fieldname=self.name, payload=payload)

    def process_callback_query(
        self, callback_payload: str, current_value: Optional[set[Enum]], language: MaybeLanguage
    ) -> CallbackProcessingResult[FieldValueT]:
        raise NotImplementedError("InlineFormField cannot be used directly, please use concrete subclasses")


@dataclass
class StrictlyInlineFormField(InlineFormField[FieldValueT]):
    please_use_inline_menu: AnyText

    def process_message(self, message: tg.Message, language: MaybeLanguage) -> MessageProcessingResult[FieldValueT]:
        return MessageProcessingResult(any_text_to_str(self.please_use_inline_menu, language), None)


@dataclass
class MultipleSelectField(_EnumDefinedFieldMixin, StrictlyInlineFormField[set[Enum]]):
    finish_field_button_caption: AnyText

    OPTION_PAYLOAD_PREFIX: ClassVar[str] = "opt"
    FINISH_FIELD_PAYLOAD: ClassVar[str] = "finish"

    def value_to_str(self, value: set[Enum], language: MaybeLanguage) -> str:
        return ", ".join([any_text_to_str(opt.value, language) for opt in value])

    def process_callback_query(
        self, callback_payload: str, current_value: Optional[set[Enum]], language: MaybeLanguage
    ) -> CallbackProcessingResult[set[Enum]]:
        if current_value is None:
            current_value = set()
        if callback_payload == self.FINISH_FIELD_PAYLOAD:
            return CallbackProcessingResult(
                response_to_user=self.get_result_message(current_value, language),
                update_inline_markup=False,
                complete_field=True,
                new_field_value=current_value,
            )
        elif callback_payload.startswith(self.OPTION_PAYLOAD_PREFIX):
            selected_option = self.parse_enum(callback_payload.removeprefix(self.OPTION_PAYLOAD_PREFIX))
            if selected_option is None:
                logger.error(
                    f"Error parsing callback payload {callback_payload!r} as Enum value {list(self.EnumClass)}"
                )
                return CallbackProcessingResult(
                    response_to_user="Something went wrong, we're on it!",
                    update_inline_markup=True,
                    complete_field=False,
                    new_field_value=current_value,
                )
            new_value: set[Enum] = current_value.copy()
            if selected_option in new_value:
                new_value.remove(selected_option)
            else:
                new_value.add(selected_option)
            return CallbackProcessingResult(
                response_to_user=None,
                update_inline_markup=True,
                complete_field=False,
                new_field_value=new_value,
            )
        else:
            logger.error(f"Error parsing callback payload {callback_payload!r}, unknown prefix!")
            return CallbackProcessingResult(
                response_to_user="Something went wrong, we're on it!",
                update_inline_markup=True,
                complete_field=False,
                new_field_value=current_value,
            )

    def get_reply_markup(
        self, language: MaybeLanguage, current_value: Optional[set[Enum]] = None
    ) -> tg.InlineKeyboardMarkup:
        if current_value is None:
            current_value = set()
        keyboard = tg.InlineKeyboardMarkup(row_width=1)
        for option in self.EnumClass:
            button_text = any_text_to_str(option.value, language)
            if option in current_value:
                button_text_marked = "☑️ " + button_text
            else:
                button_text_marked = "⬜ " + button_text
            keyboard.add(
                tg.InlineKeyboardButton(
                    text=button_text_marked,
                    callback_data=self.new_callback_data(payload=self.OPTION_PAYLOAD_PREFIX + button_text),
                )
            )
        keyboard.add(
            tg.InlineKeyboardButton(
                text=any_text_to_str(self.finish_field_button_caption, language),
                callback_data=self.FINISH_FIELD_PAYLOAD,
            )
        )
        return keyboard
