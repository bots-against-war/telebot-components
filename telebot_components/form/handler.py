from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum, auto
from typing import Any, Generic, MutableMapping, Optional, Protocol, TypeVar, cast

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.types import constants

from telebot_components.form.field import FormField, ReplyKeyboard
from telebot_components.stores.language import (
    AnyText,
    LanguageStore,
    MaybeLanguage,
    any_text_to_str,
)
from telebot_components.utils import join_paragraphs

FormResultT = TypeVar("FormResultT", bound=MutableMapping[str, Any], contravariant=True)


@dataclass
class FormConfig:
    echo_filled_field: bool
    retry_field_msg: AnyText
    cancelling_because_of_error_template: AnyText
    form_starting_template: AnyText
    can_skip_field_template: AnyText
    cant_skip_field_msg: AnyText
    cancel_cmd: str = "/cancel"
    cancel_aliases: list[str] = dataclass_field(default_factory=list)
    skip_cmd: str = "/skip"

    @property
    def cancel_cmds(self) -> list[str]:
        return [self.cancel_cmd] + self.cancel_aliases

    def can_skip_field_msg(self, language: MaybeLanguage) -> str:
        return any_text_to_str(self.can_skip_field_template, language).format(self.skip_cmd)

    def form_starting_msg(self, language: MaybeLanguage) -> str:
        return any_text_to_str(self.form_starting_template, language).format(", ".join(self.cancel_cmds))


class _FormAction(Enum):
    COMPLETED = auto()
    CANCELLED = auto()
    KEEP_GOING = auto()


@dataclass
class _UserResponse:
    message_html: str
    reply_keyboard: ReplyKeyboard = tg.ReplyKeyboardRemove()


@dataclass
class _FormStateUpdateEffect:
    action: _FormAction
    user_response: Optional[_UserResponse] = None


@dataclass
class _MutableFormState(Generic[FormResultT]):
    """User's state when they are filling out a form. Please note that a single object is
    used throughout the form with result_so_far mutating attribute.

    TODO: persistent form state?
    """

    current_field: FormField
    result_so_far: FormResultT

    async def update(
        self, message: tg.Message, language: MaybeLanguage, form_params: FormConfig
    ) -> _FormStateUpdateEffect:
        try:
            reply_paragraphs: list[str] = []

            message_cmd = message.text_content.strip() if message.content_type == "text" else None
            if message_cmd is not None and message_cmd in form_params.cancel_cmds:
                return _FormStateUpdateEffect(_FormAction.CANCELLED)
            elif message_cmd is not None and message_cmd == form_params.skip_cmd:
                if not self.current_field.required:
                    value = None
                    result_msg = None
                    field_ok = True
                else:
                    value = None
                    result_msg = any_text_to_str(form_params.cant_skip_field_msg, language)
                    field_ok = False
            else:
                result_msg, value = self.current_field.process_message(message, language)
                field_ok = value is not None

            if not field_ok:
                if result_msg:
                    reply_paragraphs.append(result_msg)
                reply_paragraphs.append(any_text_to_str(form_params.retry_field_msg, language))
                return _FormStateUpdateEffect(
                    _FormAction.KEEP_GOING,
                    user_response=_UserResponse(
                        join_paragraphs(reply_paragraphs),
                        reply_keyboard=self.current_field.get_reply_markup(language),
                    ),
                )

            self.result_so_far[self.current_field.name] = value
            if form_params.echo_filled_field and result_msg is not None:
                reply_paragraphs.append(result_msg)

            next_field = self.current_field.get_next_field(message.from_user, value)
            if next_field is None:
                return _FormStateUpdateEffect(
                    _FormAction.COMPLETED,
                    user_response=_UserResponse(join_paragraphs(reply_paragraphs)) if reply_paragraphs else None,
                )
            query_text = any_text_to_str(await next_field.get_query_message(message.from_user), language)
            if not next_field.required:
                query_text += " " + form_params.can_skip_field_msg(language)
            reply_paragraphs.append(query_text)
            self.current_field = next_field
            return _FormStateUpdateEffect(
                _FormAction.KEEP_GOING,
                user_response=_UserResponse(
                    message_html=join_paragraphs(reply_paragraphs),
                    reply_keyboard=self.current_field.get_reply_markup(language),
                ),
            )
        except Exception as e:
            return _FormStateUpdateEffect(
                _FormAction.CANCELLED,
                user_response=_UserResponse(
                    any_text_to_str(form_params.cancelling_because_of_error_template, language).format(str(e))
                ),
            )


class FormResultCallback(Protocol[FormResultT]):
    async def __call__(self, bot: AsyncTeleBot, last_message: tg.Message, user: tg.User, result: FormResultT):
        ...


class FormHandler(Generic[FormResultT]):
    def __init__(
        self,
        params: FormConfig,
        start_field: FormField,
        language_store: Optional[LanguageStore] = None,
    ):
        self.params = params
        self.start_field = start_field
        self.form_state_by_user_id: dict[int, _MutableFormState[FormResultT]] = dict()
        self.language_store = language_store

    async def get_maybe_language(self, user: tg.User) -> MaybeLanguage:
        if self.language_store is None:
            return None
        else:
            return await self.language_store.get_user_language(user)

    def setup(
        self,
        bot: AsyncTeleBot,
        on_form_completed: FormResultCallback[FormResultT],
        on_form_cancelled: Optional[FormResultCallback[FormResultT]] = None,
    ):
        async def currently_filling_form(update_content: tg.Message) -> bool:
            return update_content.from_user.id in self.form_state_by_user_id

        @bot.message_handler(func=currently_filling_form, chat_types=[constants.ChatType.private], priority=100)
        async def form_step_handler(message: tg.Message):
            user_id = message.from_user.id
            language = await self.get_maybe_language(message.from_user)
            form_state = self.form_state_by_user_id[user_id]
            state_update_effect = await form_state.update(message, language, form_params=self.params)
            response_to_user = state_update_effect.user_response
            if response_to_user is not None:
                await bot.send_message(
                    user_id,
                    text=response_to_user.message_html,
                    parse_mode="HTML",
                    reply_markup=response_to_user.reply_keyboard,
                )
            if state_update_effect.action is _FormAction.KEEP_GOING:
                return
            self.form_state_by_user_id.pop(user_id)
            if state_update_effect.action is _FormAction.CANCELLED:
                if on_form_cancelled is not None:
                    await on_form_cancelled(bot, message, message.from_user, form_state.result_so_far)
            elif state_update_effect.action is _FormAction.COMPLETED:
                await on_form_completed(bot, message, message.from_user, form_state.result_so_far)

    async def start(
        self, bot: AsyncTeleBot, user: tg.User, initial_form_result: Optional[FormResultT] = None
    ) -> tg.Message:
        self.form_state_by_user_id[user.id] = _MutableFormState(
            current_field=self.start_field,
            result_so_far=initial_form_result or cast(FormResultT, dict()),
        )
        language = await self.get_maybe_language(user)
        return await bot.send_message(
            user.id,
            text=join_paragraphs(
                [
                    self.params.form_starting_msg(language),
                    any_text_to_str((await self.start_field.get_query_message(user)), language),
                ]
            ),
            reply_markup=self.start_field.get_reply_markup(language),
            parse_mode="HTML",
        )
