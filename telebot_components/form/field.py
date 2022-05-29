from __future__ import annotations

import datetime
from abc import abstractmethod
from dataclasses import InitVar, dataclass
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

from telebot import types

from telebot_components.stores.language import (
    AnyText,
    Language,
    MaybeLanguage,
    any_text_to_str,
    validate_multilang_text,
)

FieldValueType = TypeVar("FieldValueType")

ReplyKeyboard = Union[types.ReplyKeyboardMarkup, types.ReplyKeyboardRemove]


class BadFieldValueError(Exception):
    def __init__(self, msg: AnyText):
        self.msg = msg

    def localise(self, language: MaybeLanguage) -> str:
        return any_text_to_str(self.msg, language)


@dataclass
class NextFieldGetter:
    """Service class to forward-reference the next field in a form"""

    next_field_name_getter: Callable[[types.User, FieldValueType], Optional[str]]
    # filled by bind_form_fields func
    fields_by_name: Optional[Dict[str, FormField]] = None

    def get_next_field(self, user: types.User, value: FieldValueType) -> Optional[FormField]:
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
    def by_name(cls, name: str) -> NextFieldGetter:
        return NextFieldGetter(lambda u, v: name)

    @classmethod
    def by_mapping(
        cls, value_to_next_step_name: Dict[Any, Optional[str]], default: Optional[str] = None
    ) -> NextFieldGetter:
        return NextFieldGetter(lambda u, v: value_to_next_step_name.get(v, default))


def bind_form_fields(*fields: FormField):
    fields_by_name = {f.name: f for f in fields}
    for f in fields:
        if isinstance(f.next_field, NextFieldGetter):
            f.next_field.fields_by_name = fields_by_name


@dataclass
class FormField(Generic[FieldValueType]):
    name: str
    required: bool
    query_message: AnyText
    echo_result_template: Optional[AnyText]  # must contain 1 {} for field value
    # all fields are sequential by default: pass None and override get_next_field to alter this behaviour
    next_field: Union[FormField, None, NextFieldGetter]

    @final
    def get_next_field(self, user: types.User, value: FieldValueType) -> Optional[FormField]:
        if isinstance(self.next_field, FormField):
            return self.next_field
        elif isinstance(self.next_field, NextFieldGetter):
            return self.next_field.get_next_field(user, value)
        else:
            return None

    @final
    def process_message(
        self, message: types.Message, language: MaybeLanguage
    ) -> Tuple[Optional[str], Optional[FieldValueType]]:
        try:
            value = self.parse(message)
            return self.get_result_message(value, language), value
        except BadFieldValueError as e:
            return e.localise(language), None

    async def get_query_message(self, user: types.User) -> AnyText:
        return self.query_message

    def value_to_str(self, value: FieldValueType, language: MaybeLanguage) -> str:
        return str(value)

    def get_result_message(self, value: FieldValueType, language: MaybeLanguage) -> Optional[str]:
        if self.echo_result_template is None:
            return None
        return any_text_to_str(self.echo_result_template, language).format(self.value_to_str(value, language))

    def get_reply_markup(self, language: MaybeLanguage) -> ReplyKeyboard:
        return types.ReplyKeyboardRemove()

    def parse(self, message: types.Message) -> FieldValueType:
        raise NotImplementedError("FormField cannot be used directly, please use one of the concrete subclasses")


@dataclass
class PlainTextField(FormField[str]):
    def parse(self, message: types.Message) -> str:
        return message.text_content


@dataclass
class IntegerField(FormField[int]):
    not_an_integer_msg: AnyText

    def parse(self, message: types.Message) -> int:
        text = message.text_content.strip()
        try:
            return int(text)
        except Exception:
            raise BadFieldValueError(self.not_an_integer_msg)


@dataclass
class DateField(FormField[date]):
    timezone: tzinfo
    bad_date_format_msg: AnyText
    # if specified, the date is checked to be in the future
    cant_be_in_the_past_msg: Optional[AnyText] = None

    def parse(self, message: types.Message) -> date:
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
            raise BadFieldValueError(self.bad_date_format_msg)
        if self.cant_be_in_the_past_msg is not None:
            if parsed_date < today_local:
                raise BadFieldValueError(self.cant_be_in_the_past_msg)
        return parsed_date


@dataclass
class EnumField(FormField[Enum]):
    EnumClass: Type[Enum]
    invalid_enum_value_text: AnyText
    menu_row_width: int = 2

    languages: InitVar[Optional[list[Language]]] = None

    def __post_init__(self, languages: Optional[list[Language]]):
        for option in self.EnumClass:
            if languages is not None:
                validate_multilang_text(option.value, languages)
            else:
                if not isinstance(option.value, str):
                    raise ValueError(
                        f"languages not specified for EnumField - expecting every enum option to have str value"
                    )

    def value_to_str(self, value: Enum, language: MaybeLanguage) -> str:
        return any_text_to_str(value.value, language)

    def get_reply_markup(self, language: MaybeLanguage) -> types.ReplyKeyboardMarkup:
        kbd = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, row_width=self.menu_row_width)
        for option in self.EnumClass:
            kbd.add(types.KeyboardButton(any_text_to_str(option.value, language)))
        return kbd

    def parse(self, message: types.Message) -> Enum:
        for enum in self.EnumClass:
            for _, lang_text in enum.value.items():
                if lang_text == message.text:
                    return enum
        raise BadFieldValueError(self.invalid_enum_value_text)
