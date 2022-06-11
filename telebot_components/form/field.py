import datetime
from dataclasses import dataclass
from datetime import date, tzinfo
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
    final,
)

from telebot import types as tg

from telebot_components.stores.language import (
    AnyText,
    Language,
    MaybeLanguage,
    any_text_to_str,
    validate_multilang_text,
)

FieldValueT = TypeVar("FieldValueT")

ReplyKeyboard = Union[tg.ReplyKeyboardMarkup, tg.ReplyKeyboardRemove]


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
class FormField(Generic[FieldValueT]):
    name: str
    required: bool
    query_message: AnyText
    echo_result_template: Optional[AnyText]  # should contain 1 '{}' for field value
    next_field_getter: NextFieldGetter

    @final
    def process_message(
        self, message: tg.Message, language: MaybeLanguage
    ) -> Tuple[Optional[str], Optional[FieldValueT]]:
        try:
            value = self.parse(message)
            return self.get_result_message(value, language), value
        except BadFieldValueError as error:
            return any_text_to_str(error.msg, language), None

    async def get_query_message(self, user: tg.User) -> AnyText:
        return self.query_message

    def value_to_str(self, value: FieldValueT, language: MaybeLanguage) -> str:
        return str(value)

    def get_result_message(self, value: FieldValueT, language: MaybeLanguage) -> Optional[str]:
        if self.echo_result_template is None:
            return None
        return any_text_to_str(self.echo_result_template, language).format(self.value_to_str(value, language))

    def get_reply_markup(self, language: MaybeLanguage) -> ReplyKeyboard:
        return tg.ReplyKeyboardRemove()

    # NOTE: not using abstractmethod here because of https://github.com/python/mypy/issues/5374
    def parse(self, message: tg.Message) -> FieldValueT:
        raise NotImplementedError("FormField cannot be used directly, please use concrete suclasses")

    def texts(self) -> list[AnyText]:
        """Used for build-time validation that all the fields in the form are properly localised"""
        res = [self.query_message]
        if self.echo_result_template is not None:
            res.append(self.echo_result_template)
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

    def custom_texts(self) -> list[AnyText]:
        return [self.empty_text_error_msg]


@dataclass
class IntegerField(FormField[int]):
    not_an_integer_error_msg: AnyText

    def parse(self, message: tg.Message) -> int:
        text = message.text_content.strip()
        try:
            return int(text)
        except Exception:
            raise BadFieldValueError(self.not_an_integer_error_msg)

    def custom_texts(self) -> list[AnyText]:
        return [self.not_an_integer_error_msg]


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
        res = [self.bad_date_format_error_msg]
        if self.cant_be_in_the_past_error_msg is not None:
            res.append(self.cant_be_in_the_past_error_msg)
        return res


@dataclass
class EnumField(FormField[Enum]):
    EnumClass: Type[Enum]
    invalid_enum_value_error_msg: AnyText
    menu_row_width: int = 2

    def value_to_str(self, value: Enum, language: MaybeLanguage) -> str:
        return any_text_to_str(value.value, language)

    def get_reply_markup(self, language: MaybeLanguage) -> tg.ReplyKeyboardMarkup:
        kbd = tg.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=self.menu_row_width)
        for option in self.EnumClass:
            kbd.add(tg.KeyboardButton(any_text_to_str(option.value, language)))
        return kbd

    def parse(self, message: tg.Message) -> Enum:
        for enum in self.EnumClass:
            for _, lang_text in enum.value.items():
                if lang_text == message.text:
                    return enum
        raise BadFieldValueError(self.invalid_enum_value_error_msg)

    def custom_texts(self) -> list[AnyText]:
        res: list[AnyText] = [option.value for option in self.EnumClass]
        res.append(self.invalid_enum_value_error_msg)
        return res
