from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum, auto
from itertools import chain
from typing import (
    Any,
    Callable,
    Coroutine,
    Generic,
    Mapping,
    Optional,
    TypeVar,
    Union,
    cast,
)

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.types import constants

from telebot_components.constants import times
from telebot_components.form.field import (
    INLINE_FIELD_CALLBACK_DATA,
    FormField,
    InlineFormField,
)
from telebot_components.form.form import Form
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.generic import KeyValueStore
from telebot_components.stores.language import (
    AnyText,
    LanguageStore,
    MaybeLanguage,
    any_text_to_str,
    vaildate_singlelang_text,
)
from telebot_components.utils import from_yaml_unsafe, join_paragraphs, to_yaml_unsafe
from telebot_components.utils.strings import telegram_html_escape

FormResultT = TypeVar("FormResultT", bound=Mapping[str, Any])


logger = logging.getLogger(__name__)


@dataclass
class FormHandlerConfig:
    echo_filled_field: bool

    # e.g. "Please enter the correct value."
    retry_field_msg: AnyText

    # should have placeholder for available commands, e.g. "Unknown command! Available commands are: {}."
    unsupported_cmd_error_template: AnyText

    # should have placeholder for error, e.g. "Something went wrong but we're working on it (details: {})."
    cancelling_because_of_error_template: AnyText

    # should have placeholder for cancel commands, e.g. "Welcome to my awesome form! To cancel, use {}."
    form_starting_template: AnyText

    # should have placeholder for skip command, e.g. "Use {} to skip this field."
    can_skip_field_template: AnyText
    # e.g. "This field can not be skipped!"
    cant_skip_field_msg: AnyText

    cancel_cmd: str = "/cancel"
    cancel_aliases: list[str] = dataclass_field(default_factory=list)
    skip_cmd: str = "/skip"

    # if set to any text value and any field already has results present (from initial_form_result or
    # because of circular form structure), this is added to the field query
    # should have 2 placeholders, e.g. "To keep existing value {}, use {}"
    keep_existing_field_value_template: Optional[AnyText] = None
    keep_cmd: str = "/keep"

    def texts(self) -> list[AnyText]:
        res = [
            self.retry_field_msg,
            self.cancelling_because_of_error_template,
            self.form_starting_template,
            self.can_skip_field_template,
            self.cant_skip_field_msg,
            self.unsupported_cmd_error_template,
        ]
        if self.keep_existing_field_value_template is not None:
            res.append(self.keep_existing_field_value_template)
        return res

    @property
    def cancel_cmds(self) -> list[str]:
        return [self.cancel_cmd] + self.cancel_aliases

    @property
    def available_cmds(self) -> list[str]:
        cmds = [self.skip_cmd] + self.cancel_cmds
        if self.is_keeping_existing_field_value():
            cmds.append(self.keep_cmd)
        return cmds

    def is_keeping_existing_field_value(self) -> bool:
        return self.keep_existing_field_value_template is not None

    def can_skip_field_msg(self, language: MaybeLanguage) -> str:
        return any_text_to_str(self.can_skip_field_template, language).format(self.skip_cmd)

    def keep_existing_field_value_msg(self, value_dump: str, language: MaybeLanguage) -> Optional[str]:
        if self.keep_existing_field_value_template is None:
            return None
        else:
            return any_text_to_str(self.keep_existing_field_value_template, language).format(
                telegram_html_escape(value_dump), self.keep_cmd
            )

    def form_starting_msg(self, language: MaybeLanguage) -> str:
        return any_text_to_str(self.form_starting_template, language).format(", ".join(self.cancel_cmds))

    def unsupported_cmd_error_msg(self, language: MaybeLanguage) -> str:
        return any_text_to_str(self.unsupported_cmd_error_template, language).format(", ".join(self.available_cmds))


class _FormAction(Enum):
    COMPLETE = auto()
    CANCEL = auto()
    KEEP_GOING = auto()
    DO_NOTHING = auto()


@dataclass
class _UserAction:
    send_message_html: Optional[str]
    send_reply_keyboard: tg.ReplyMarkup
    update_inline_markup: Optional[tg.ReplyMarkup] = None


@dataclass
class _FormStateUpdateEffect:
    form_action: _FormAction
    user_action: Optional[_UserAction] = None


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

    async def get_current_query_message(
        self, user: tg.User, language: MaybeLanguage, form_handler_config: FormHandlerConfig
    ) -> str:
        sentences = [any_text_to_str(await self.current_field.get_query_message(user), language)]
        if not self.current_field.required:
            sentences.append(form_handler_config.can_skip_field_msg(language))
        existing_field_value = self.result_so_far.get(self.current_field.name)
        if existing_field_value is not None:
            keep_existing_field_value_sentence = form_handler_config.keep_existing_field_value_msg(
                self.current_field.value_to_str(existing_field_value, language),
                language,
            )
            if keep_existing_field_value_sentence is not None:
                sentences.append(keep_existing_field_value_sentence)

        return " ".join(sentences)

    def get_current_reply_markup(self, language: MaybeLanguage) -> tg.ReplyMarkup:
        return self.current_field.get_reply_markup(
            language,
            current_value=self.result_so_far.get(self.current_field.name),
        )

    async def update_with_message(
        self,
        message: tg.Message,
        language: MaybeLanguage,
        form_handler_config: FormHandlerConfig,
    ) -> _FormStateUpdateEffect:
        reply_paragraphs: list[str] = []

        save_field_value: bool = True
        value: Optional[Any] = None
        response_to_user: Optional[str] = None

        message_cmd = message.text_content.strip() if message.content_type == "text" else None
        if message_cmd is not None and message_cmd.startswith("/"):
            if message_cmd in form_handler_config.cancel_cmds:
                return _FormStateUpdateEffect(_FormAction.CANCEL)
            elif message_cmd == form_handler_config.skip_cmd:
                if not self.current_field.required:
                    is_field_ok = True
                else:
                    response_to_user = any_text_to_str(form_handler_config.cant_skip_field_msg, language)
                    is_field_ok = False
            elif message_cmd == form_handler_config.keep_cmd:
                if (
                    form_handler_config.is_keeping_existing_field_value()
                    and self.current_field.name in self.result_so_far
                ):
                    is_field_ok = True
                    save_field_value = False
                else:
                    is_field_ok = False
            else:
                return _FormStateUpdateEffect(
                    _FormAction.KEEP_GOING,
                    user_action=_UserAction(
                        send_message_html=form_handler_config.unsupported_cmd_error_msg(language),
                        send_reply_keyboard=self.get_current_reply_markup(language),
                    ),
                )
        else:
            result = await self.current_field.process_message(message, language)
            value = result.parsed_value
            response_to_user = result.response_to_user
            is_field_ok = result.parsed_value is not None
            if result.no_form_state_mutation:
                return _FormStateUpdateEffect(
                    _FormAction.KEEP_GOING,
                    user_action=_UserAction(
                        send_message_html=response_to_user,
                        send_reply_keyboard=result.response_reply_markup or tg.ReplyKeyboardRemove(),
                    ),
                )

        if not is_field_ok:
            if response_to_user:
                reply_paragraphs.append(response_to_user)
            reply_paragraphs.append(any_text_to_str(form_handler_config.retry_field_msg, language))
            return _FormStateUpdateEffect(
                _FormAction.KEEP_GOING,
                user_action=_UserAction(
                    send_message_html=join_paragraphs(reply_paragraphs),
                    send_reply_keyboard=self.get_current_reply_markup(language),
                ),
            )

        if save_field_value:
            # result_so_far is typed as immutable Mapping to allow TypedDict's, but here we actually construct it
            self.result_so_far[self.current_field.name] = value  # type: ignore

        if form_handler_config.echo_filled_field and response_to_user is not None:
            reply_paragraphs.append(response_to_user)

        next_field = await self.current_field.get_next_field_getter()(
            user=message.from_user,
            value=value,
            current_field_name=self.current_field.name,
        )
        if next_field is None:
            return _FormStateUpdateEffect(
                _FormAction.COMPLETE,
                user_action=(
                    _UserAction(
                        send_message_html=join_paragraphs(reply_paragraphs),
                        send_reply_keyboard=tg.ReplyKeyboardRemove(),
                    )
                    if reply_paragraphs
                    else None
                ),
            )
        self.current_field = next_field
        reply_paragraphs.append(await self.get_current_query_message(message.from_user, language, form_handler_config))
        return _FormStateUpdateEffect(
            _FormAction.KEEP_GOING,
            user_action=_UserAction(
                send_message_html=join_paragraphs(reply_paragraphs),
                send_reply_keyboard=self.get_current_reply_markup(language),
            ),
        )

    async def update_with_callback_query(
        self,
        call: tg.CallbackQuery,
        language: MaybeLanguage,
        form_handler_config: FormHandlerConfig,
    ) -> _FormStateUpdateEffect:
        if not isinstance(self.current_field, InlineFormField):
            return _FormStateUpdateEffect(_FormAction.DO_NOTHING)
        callback_data = INLINE_FIELD_CALLBACK_DATA.parse(call.data)
        if callback_data["fieldname"] != self.current_field.name:
            return _FormStateUpdateEffect(_FormAction.DO_NOTHING)
        field_result = await self.current_field.process_callback_query(
            callback_payload=callback_data["payload"],
            current_value=self.result_so_far.get(self.current_field.name),
            language=language,
        )
        if field_result.new_field_value is not None:
            # result_so_far is typed as immutable Mapping to allow TypedDict's, but here we actually construct it
            self.result_so_far[self.current_field.name] = field_result.new_field_value  # type: ignore
        paragraphs: list[str] = []
        if field_result.response_to_user:
            paragraphs.append(field_result.response_to_user)

        send_reply_keyboard: tg.ReplyMarkup = tg.ReplyKeyboardRemove()
        form_action = _FormAction.KEEP_GOING
        if field_result.complete_field:
            next_field = await self.current_field.get_next_field_getter()(
                user=call.from_user,
                value=field_result.new_field_value,
                current_field_name=self.current_field.name,
            )
            if next_field is None:
                form_action = _FormAction.COMPLETE
            else:
                self.current_field = next_field
                paragraphs.append(await self.get_current_query_message(call.from_user, language, form_handler_config))
                send_reply_keyboard = self.get_current_reply_markup(language)

        return _FormStateUpdateEffect(
            form_action=form_action,
            user_action=_UserAction(
                send_message_html=join_paragraphs(paragraphs),
                send_reply_keyboard=send_reply_keyboard,
                update_inline_markup=field_result.updated_inline_markup,
            ),
        )


@dataclass
class FormExitContext(Generic[FormResultT]):
    bot: AsyncTeleBot
    last_update: Union[tg.Message, tg.CallbackQuery]
    result: FormResultT


FormExitCallback = Callable[[FormExitContext], Coroutine[None, None, None]]


class FormHandler(Generic[FormResultT]):
    def __init__(
        self,
        redis: RedisInterface,
        bot_prefix: str,
        name: str,
        form: Form,
        config: FormHandlerConfig,
        language_store: Optional[LanguageStore] = None,
    ):
        self.config = config
        self.form = form

        self.form_state_store = KeyValueStore[Optional[FormState]](
            name=f"form-state-for-{name}",
            prefix=bot_prefix,
            redis=redis,
            expiration_time=3 * times.HOUR,
            dumper=lambda fs: fs.to_store() if fs is not None else "",
            loader=lambda dump: FormState.from_store(dump, form.fields),
        )

        self.language_store = language_store

        for any_text in chain.from_iterable([self.config.texts()] + [f.texts() for f in self.form.fields]):
            if self.language_store is not None:
                self.language_store.validate_multilang(any_text)
            else:
                vaildate_singlelang_text(any_text)

    async def get_maybe_language(self, user: tg.User) -> MaybeLanguage:
        if self.language_store is None:
            return None
        else:
            return await self.language_store.get_user_language(user)

    def setup(
        self,
        bot: AsyncTeleBot,
        on_form_completed: FormExitCallback,
        on_form_cancelled: Optional[FormExitCallback] = None,
    ):
        async def currently_filling_form(update_content: Union[tg.Message, tg.CallbackQuery]) -> bool:
            return await self.form_state_store.exists(update_content.from_user.id)

        async def form_action_handler(
            user: tg.User,
            last_message_id: int,
            form_state_updater: Callable[[FormState, MaybeLanguage], Coroutine[None, None, _FormStateUpdateEffect]],
            form_exit_context_constructor: Callable[[AsyncTeleBot, FormResultT], FormExitContext],
        ):
            user_id = user.id
            language = await self.get_maybe_language(user)
            form_state = await self.form_state_store.load(user_id)
            if form_state is None:
                logger.error("Error loading form state from the store, dropping it")
                await self.form_state_store.drop(user_id)
                return
            try:
                state_update_effect = await form_state_updater(form_state, language)
            except Exception as e:
                logger.exception(f"Unexpected error updating form state with {form_state_updater!r}")
                state_update_effect = _FormStateUpdateEffect(
                    _FormAction.CANCEL,
                    user_action=_UserAction(
                        send_message_html=any_text_to_str(
                            self.config.cancelling_because_of_error_template, language
                        ).format(telegram_html_escape(str(e))),
                        send_reply_keyboard=tg.ReplyKeyboardRemove(),
                    ),
                )
            if state_update_effect.form_action is _FormAction.DO_NOTHING:
                return
            await self.form_state_store.save(user_id, form_state)
            user_action = state_update_effect.user_action
            if user_action is not None:
                if user_action.send_message_html:
                    await bot.send_message(
                        user_id,
                        text=user_action.send_message_html,
                        parse_mode="HTML",
                        reply_markup=user_action.send_reply_keyboard,
                    )
                if user_action.update_inline_markup is not None:
                    try:
                        await bot.edit_message_reply_markup(
                            chat_id=user.id,
                            message_id=last_message_id,
                            reply_markup=user_action.update_inline_markup,
                        )
                    except Exception:
                        pass
            if state_update_effect.form_action is _FormAction.KEEP_GOING:
                return
            else:
                await self.form_state_store.drop(user_id)
                form_exit_context = form_exit_context_constructor(bot, form_state.result_so_far)
                if state_update_effect.form_action is _FormAction.CANCEL:
                    if on_form_cancelled is not None:
                        await on_form_cancelled(form_exit_context)
                elif state_update_effect.form_action is _FormAction.COMPLETE:
                    await on_form_completed(form_exit_context)

        @bot.message_handler(func=currently_filling_form, chat_types=[constants.ChatType.private], priority=100)
        async def form_message_action_handler(message: tg.Message):
            async def form_state_updater(form_state: FormState, language: MaybeLanguage):
                return await form_state.update_with_message(message, language, self.config)

            def form_exit_context_constructor(bot: AsyncTeleBot, result: FormResultT):
                return FormExitContext(bot, message, result)

            await form_action_handler(
                user=message.from_user,
                last_message_id=message.id,
                form_state_updater=form_state_updater,
                form_exit_context_constructor=form_exit_context_constructor,
            )

        @bot.callback_query_handler(func=currently_filling_form, callback_data=INLINE_FIELD_CALLBACK_DATA)
        async def form_inline_action_handler(call: tg.CallbackQuery):
            async def form_state_updater(form_state: FormState, language: MaybeLanguage):
                return await form_state.update_with_callback_query(call, language, self.config)

            def form_exit_context_constructor(bot: AsyncTeleBot, result: FormResultT):
                return FormExitContext(bot, call, result)

            try:
                await form_action_handler(
                    user=call.from_user,
                    last_message_id=call.message.id,
                    form_state_updater=form_state_updater,
                    form_exit_context_constructor=form_exit_context_constructor,
                )
            except Exception:
                logger.exception("Unexpected error processing form action")
            finally:
                await bot.answer_callback_query(call.id)

    async def start(
        self,
        bot: AsyncTeleBot,
        user: tg.User,
        initial_form_result: Optional[FormResultT] = None,
        separate_field_prompt_message: bool = False,
    ) -> tg.Message:
        initial_form_state = FormState(
            current_field=self.form.start_field,
            result_so_far=initial_form_result or cast(FormResultT, dict()),
        )
        await self.form_state_store.save(user.id, initial_form_state)
        language = await self.get_maybe_language(user)

        if not separate_field_prompt_message:
            return await bot.send_message(
                user.id,
                text=join_paragraphs(
                    [
                        self.config.form_starting_msg(language),
                        await initial_form_state.get_current_query_message(user, language, self.config),
                    ]
                ),
                reply_markup=initial_form_state.get_current_reply_markup(language),
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                user.id,
                text=self.config.form_starting_msg(language),
                parse_mode="HTML",
            )
            return await bot.send_message(
                user.id,
                text=await initial_form_state.get_current_query_message(user, language, self.config),
                reply_markup=initial_form_state.get_current_reply_markup(language),
                parse_mode="HTML",
            )
