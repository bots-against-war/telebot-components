from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum, auto
from typing import Any, Generic, MutableMapping, Optional, Protocol, TypeVar, cast

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.types import constants

from telebot_components.constants import times
from telebot_components.form.field import FormField, ReplyKeyboard
from telebot_components.form.form import Form
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import KeyValueStore
from telebot_components.stores.language import (
    AnyText,
    LanguageStore,
    MaybeLanguage,
    any_text_to_str,
)
from telebot_components.utils import from_yaml_unsafe, join_paragraphs, to_yaml_unsafe

FormResultT = TypeVar("FormResultT", bound=MutableMapping[str, Any], contravariant=True)


logger = logging.getLogger(__name__)


@dataclass
class FormHandlerConfig:
    echo_filled_field: bool
    retry_field_msg: AnyText
    # should have placeholder for available commands
    unsupported_cmd_error_template: AnyText
    # should have placeholder for error
    cancelling_because_of_error_template: AnyText
    # should have placeholder for cancel commands
    form_starting_template: AnyText
    # should have placeholder for skip command
    can_skip_field_template: AnyText
    cant_skip_field_msg: AnyText
    cancel_cmd: str = "/cancel"
    cancel_aliases: list[str] = dataclass_field(default_factory=list)
    skip_cmd: str = "/skip"

    @property
    def cancel_cmds(self) -> list[str]:
        return [self.cancel_cmd] + self.cancel_aliases

    @property
    def available_cmds(self) -> list[str]:
        return [self.skip_cmd] + self.cancel_cmds

    def can_skip_field_msg(self, language: MaybeLanguage) -> str:
        return any_text_to_str(self.can_skip_field_template, language).format(self.skip_cmd)

    def form_starting_msg(self, language: MaybeLanguage) -> str:
        return any_text_to_str(self.form_starting_template, language).format(", ".join(self.cancel_cmds))

    def unsupported_cmd_error_msg(self, language: MaybeLanguage) -> str:
        return any_text_to_str(self.unsupported_cmd_error_template, language).format(", ".join(self.available_cmds))


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
class FormState(Generic[FormResultT]):
    """User's state when they are filling out a form. Please note that a single object is
    used throughout the form with result_so_far mutating attribute.
    """

    current_field: FormField
    result_so_far: FormResultT

    def to_store(self) -> str:
        return json.dumps(
            {
                "current_field": self.current_field.name,
                "result_so_far": to_yaml_unsafe(self.result_so_far),
            }
        )

    @classmethod
    def from_store(self, dump: str, form_fields: list[FormField]) -> Optional["FormState"]:
        try:
            asdict = json.loads(dump)
            form_field_by_name = {f.name: f for f in form_fields}
            return FormState(
                current_field=form_field_by_name[asdict["current_field"]],
                result_so_far=from_yaml_unsafe(asdict["result_so_far"]),
            )
        except Exception:
            logger.exception("Error loading form state from persistent storage")
            return None

    async def update(
        self, message: tg.Message, language: MaybeLanguage, form_handler_config: FormHandlerConfig
    ) -> _FormStateUpdateEffect:
        try:
            reply_paragraphs: list[str] = []

            message_cmd = message.text_content.strip() if message.content_type == "text" else None
            if message_cmd is not None and message_cmd.startswith("/"):
                if message_cmd in form_handler_config.cancel_cmds:
                    return _FormStateUpdateEffect(_FormAction.CANCELLED)
                elif message_cmd == form_handler_config.skip_cmd:
                    if not self.current_field.required:
                        value = None
                        result_msg = None
                        field_ok = True
                    else:
                        value = None
                        result_msg = any_text_to_str(form_handler_config.cant_skip_field_msg, language)
                        field_ok = False
                else:
                    return _FormStateUpdateEffect(
                        _FormAction.KEEP_GOING,
                        user_response=_UserResponse(
                            form_handler_config.unsupported_cmd_error_msg(language),
                            reply_keyboard=self.current_field.get_reply_markup(language),
                        ),
                    )
            else:
                result_msg, value = self.current_field.process_message(message, language)
                field_ok = value is not None

            if not field_ok:
                if result_msg:
                    reply_paragraphs.append(result_msg)
                reply_paragraphs.append(any_text_to_str(form_handler_config.retry_field_msg, language))
                return _FormStateUpdateEffect(
                    _FormAction.KEEP_GOING,
                    user_response=_UserResponse(
                        join_paragraphs(reply_paragraphs),
                        reply_keyboard=self.current_field.get_reply_markup(language),
                    ),
                )

            self.result_so_far[self.current_field.name] = value
            if form_handler_config.echo_filled_field and result_msg is not None:
                reply_paragraphs.append(result_msg)

            next_field = self.current_field.next_field_getter.get_next_field(message.from_user, value)
            if next_field is None:
                return _FormStateUpdateEffect(
                    _FormAction.COMPLETED,
                    user_response=_UserResponse(join_paragraphs(reply_paragraphs)) if reply_paragraphs else None,
                )
            query_text = any_text_to_str(await next_field.get_query_message(message.from_user), language)
            if not next_field.required:
                query_text += " " + form_handler_config.can_skip_field_msg(language)
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
                    any_text_to_str(form_handler_config.cancelling_because_of_error_template, language).format(str(e))
                ),
            )


class FormResultCallback(Protocol[FormResultT]):
    async def __call__(self, bot: AsyncTeleBot, last_message: tg.Message, user: tg.User, result: FormResultT):
        ...


class FormHandler(Generic[FormResultT]):
    def __init__(
        self,
        redis: RedisInterface,
        bot_prefix: str,
        form: Form,
        config: FormHandlerConfig,
        language_store: Optional[LanguageStore] = None,
    ):
        self.config = config
        self.form = form

        self.form_state_by_user_id_store = KeyValueStore[Optional[FormState]](
            name="form-state-for",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=times.HOUR,
            # NOTE: unsafe is ok here because we fully control the data
            dumper=lambda fs: fs.to_store() if fs is not None else "",
            loader=lambda dump: FormState.from_store(dump, form.fields),
        )

        self.language_store = language_store

        form_related_texts = [
            self.config.retry_field_msg,
            self.config.cancelling_because_of_error_template,
            self.config.form_starting_template,
            self.config.can_skip_field_template,
            self.config.cant_skip_field_msg,
            self.config.unsupported_cmd_error_template,
        ]
        for field in self.form.fields:
            form_related_texts.extend(field.texts())

        for text in form_related_texts:
            if self.language_store is not None:
                self.language_store.validate_multilang(text)
            else:
                if not isinstance(text, str):
                    raise ValueError(
                        "All form-related texts are expected to be strings when language store is not used"
                    )

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
            return await self.form_state_by_user_id_store.exists(update_content.from_user.id)

        @bot.message_handler(func=currently_filling_form, chat_types=[constants.ChatType.private], priority=100)
        async def form_step_handler(message: tg.Message):
            user_id = message.from_user.id
            language = await self.get_maybe_language(message.from_user)
            form_state = await self.form_state_by_user_id_store.load(user_id)
            if form_state is None:
                logger.error("Error loading form state from the store, dropping it")
                await self.form_state_by_user_id_store.drop(user_id)
                return
            state_update_effect = await form_state.update(message, language, form_handler_config=self.config)
            await self.form_state_by_user_id_store.save(user_id, form_state)
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
            await self.form_state_by_user_id_store.drop(user_id)
            if state_update_effect.action is _FormAction.CANCELLED:
                if on_form_cancelled is not None:
                    await on_form_cancelled(bot, message, message.from_user, form_state.result_so_far)
            elif state_update_effect.action is _FormAction.COMPLETED:
                await on_form_completed(bot, message, message.from_user, form_state.result_so_far)

    async def start(
        self, bot: AsyncTeleBot, user: tg.User, initial_form_result: Optional[FormResultT] = None
    ) -> tg.Message:
        initial_form_state = FormState(
            current_field=self.form.start_field,
            result_so_far=initial_form_result or cast(FormResultT, dict()),
        )
        await self.form_state_by_user_id_store.save(user.id, initial_form_state)
        language = await self.get_maybe_language(user)
        return await bot.send_message(
            user.id,
            text=join_paragraphs(
                [
                    self.config.form_starting_msg(language),
                    any_text_to_str((await self.form.start_field.get_query_message(user)), language),
                ]
            ),
            reply_markup=self.form.start_field.get_reply_markup(language),
            parse_mode="HTML",
        )
