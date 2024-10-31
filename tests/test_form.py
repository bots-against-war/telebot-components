import textwrap
from enum import Enum
from typing import Any

import pytest
from telebot import types as tg
from telebot.test_util import MockedAsyncTeleBot

from telebot_components.form.field import (
    DynamicOption,
    DynamicSingleSelectField,
    FormField,
    FormFieldResultExportOpts,
    FormFieldResultFormattingOpts,
    NextFieldGetter,
    PlainTextField,
    SingleSelectField,
)
from telebot_components.form.form import Form, FormBranch
from telebot_components.form.handler import (
    FormExitContext,
    FormHandler,
    FormHandlerConfig,
)
from telebot_components.redis_utils.interface import RedisInterface
from telebot_components.stores.language import MaybeLanguage
from tests.utils import (
    TelegramServerMock,
    assert_list_of_required_subdicts,
    extract_full_kwargs,
    generate_str,
    reply_markups_to_dict,
)

DUMMY_FORM_FIELD_KW = {"required": True, "query_message": "aaa", "empty_text_error_msg": "fail :("}


@pytest.mark.parametrize(
    "fields, expected_globally_required, expected_result_type",
    [
        pytest.param(
            [
                PlainTextField(name="a", **DUMMY_FORM_FIELD_KW),  # type: ignore
                PlainTextField(name="b", **DUMMY_FORM_FIELD_KW),  # type: ignore
                PlainTextField(name="c", **DUMMY_FORM_FIELD_KW),  # type: ignore
            ],
            {"a", "b", "c"},
            '''
            class MyFormResultT(TypedDict):
                """Generated by Form.generate_result_type() method"""
                a: str
                b: str
                c: str
            ''',
            id="basic linear form",
        ),
        pytest.param(
            [
                PlainTextField(name="a", **DUMMY_FORM_FIELD_KW),  # type: ignore
                PlainTextField(
                    name="b",
                    next_field_getter=NextFieldGetter.by_mapping({"one": "c1", "two": "c2"}, default="c3"),
                    **DUMMY_FORM_FIELD_KW,  # type: ignore
                ),
                PlainTextField(
                    name="c1",
                    next_field_getter=NextFieldGetter.by_name("d"),
                    **DUMMY_FORM_FIELD_KW,  # type: ignore
                ),
                PlainTextField(
                    name="c2",
                    next_field_getter=NextFieldGetter.by_name("c4"),
                    **DUMMY_FORM_FIELD_KW,  # type: ignore
                ),
                PlainTextField(
                    name="c4",
                    next_field_getter=NextFieldGetter.by_name("c5"),
                    **DUMMY_FORM_FIELD_KW,  # type: ignore
                ),
                PlainTextField(
                    name="c5",
                    next_field_getter=NextFieldGetter.by_name("d"),
                    **DUMMY_FORM_FIELD_KW,  # type: ignore
                ),
                PlainTextField(
                    name="c3",
                    next_field_getter=NextFieldGetter.by_name("d"),
                    **DUMMY_FORM_FIELD_KW,  # type: ignore
                ),
                PlainTextField(name="d", **DUMMY_FORM_FIELD_KW),  # type: ignore
                PlainTextField(name="e", **DUMMY_FORM_FIELD_KW),  # type: ignore
            ],
            {"a", "b", "d", "e"},
            '''
            class MyFormResultT(TypedDict):
                """Generated by Form.generate_result_type() method"""
                a: str
                b: str
                c1: NotRequired[str]
                c2: NotRequired[str]
                c3: NotRequired[str]
                c4: NotRequired[str]
                c5: NotRequired[str]
                d: str
                e: str
            ''',
            id="basic linear form",
        ),
    ],
)
def test_form_graph_analysis(
    fields: list[PlainTextField],
    expected_globally_required: set[str],
    expected_result_type: str,
) -> None:
    f = Form(fields=fields)
    assert f.globally_required_fields is not None
    assert f.globally_required_fields == expected_globally_required

    print(f.generate_result_type())
    print(f.format_graph())

    assert_equal_multiline_text(f.generate_result_type(), expected_result_type)


@pytest.mark.parametrize("with_omitted_fields", [True, False])
def test_form_result_processing(with_omitted_fields: bool) -> None:
    class GDPRConsent(Enum):
        YES = "yes"
        NO = "no"
        MAYBE = "maybe"

    def format_gdrp_consent(v: GDPRConsent, lang: MaybeLanguage) -> str:
        if v is GDPRConsent.YES:
            return "✅"
        elif v is GDPRConsent.NO:
            return "❌"
        else:
            return "🧐"

    fields: list[FormField] = [
        PlainTextField(
            "message",
            result_formatting_opts=FormFieldResultFormattingOpts(descr="Message"),
            export_opts=FormFieldResultExportOpts(column="A"),
            **DUMMY_FORM_FIELD_KW,  # type: ignore
        ),
        PlainTextField(
            "name",
            result_formatting_opts=FormFieldResultFormattingOpts(descr="Name"),
            export_opts=FormFieldResultExportOpts(column="B", value_processor=lambda s: s.upper()),
            **DUMMY_FORM_FIELD_KW,  # type: ignore
        ),
        SingleSelectField(
            "gdrp",
            result_formatting_opts=FormFieldResultFormattingOpts(
                descr="GDRP OK?",
                value_formatter=format_gdrp_consent,
            ),
            export_opts=FormFieldResultExportOpts(
                column="C",
                value_mapping={
                    GDPRConsent.YES: "+",
                    GDPRConsent.NO: "-",
                    GDPRConsent.MAYBE: "?",
                },
            ),
            required=True,
            query_message="?",
            EnumClass=GDPRConsent,
            invalid_enum_value_error_msg="",
        ),
        PlainTextField(
            "long_description",
            result_formatting_opts=FormFieldResultFormattingOpts(descr="Description", is_multiline=True),
            export_opts=None,
            **DUMMY_FORM_FIELD_KW,  # type: ignore
        ),
    ]

    if with_omitted_fields:
        fields.append(
            PlainTextField(
                "extra_info",
                export_opts=None,
                **DUMMY_FORM_FIELD_KW,  # type: ignore
            )
        )
    form = Form(fields)

    form_result = {
        "message": "Hello world",
        "name": "Igor",
        "gdrp": GDPRConsent.MAYBE,
        "long_description": "Lorem ipsum yada yada yada",
    }
    if with_omitted_fields:
        form_result["extra_info"] = "hi"

    telegram_msg_html = form.result_to_html(form_result, lang=None)
    expected_msg = """
        <b>Message</b>: Hello world
        <b>Name</b>: Igor
        <b>GDRP OK?</b>: 🧐
        <b>Description</b>
        Lorem ipsum yada yada yada"""
    if with_omitted_fields:
        expected_msg += "\n        <i>+1 omitted</i>"
    assert_equal_multiline_text(telegram_msg_html, expected_msg)

    assert form.result_to_export(form_result) == {"A": "Hello world", "B": "IGOR", "C": "?"}


def assert_equal_multiline_text(t1: str, t2: str) -> None:
    def preproc(s: str) -> str:
        s = "\n".join(line for line in s.splitlines() if line)
        s = textwrap.dedent(s)
        s = s.strip()
        return s

    assert preproc(t1) == preproc(t2)


def test_branching_form_constructor() -> None:
    def field(name: str) -> FormField:
        return PlainTextField(name=name, required=True, query_message=name, empty_text_error_msg="qwerty")

    f = Form.branching(
        [
            field("A"),
            field("B"),
            FormBranch(
                [
                    field("C"),
                    field("D"),
                    field("E"),
                ],
                condition="branch-1",
            ),
            FormBranch(
                [
                    field("F"),
                    field("G"),
                ],
                condition="branch-2",
            ),
            FormBranch(
                [
                    field("foo"),
                    FormBranch([field("foo-sub-1"), field("foo-sub-2")], condition="sub-branch-3-1"),
                    field("bar"),
                ],
                condition="branch-3",
            ),
            field("final field 1"),
            field("final field 2"),
        ]
    )

    assert f.next_field_names == {
        "A": {"B"},
        "B": {"foo", "F", "C", "final field 1"},
        "C": {"D"},
        "D": {"E"},
        "E": {"final field 1"},
        "F": {"G"},
        "G": {"final field 1"},
        "foo": {"bar", "foo-sub-1"},
        "foo-sub-1": {"foo-sub-2"},
        "foo-sub-2": {"bar"},
        "bar": {"final field 1"},
        "final field 1": {"final field 2"},
        "final field 2": {None},
    }


async def test_form_handler(redis: RedisInterface) -> None:
    bot_prefix = generate_str()

    def field(name: str, is_optional: bool = False) -> FormField:
        return PlainTextField(
            name=name,
            required=not is_optional,
            query_message=f"Question: {name}",
            empty_text_error_msg="empty test error",
            result_formatting_opts=FormFieldResultFormattingOpts(descr=name, is_multiline=False),
        )

    f = Form.branching(
        [
            field("your name"),
            field("your favourite food"),
            field("your pet's name", is_optional=True),
        ]
    )

    fh = FormHandler[Any, Any](
        redis=redis,
        bot_prefix=bot_prefix,
        name="test-form",
        form=f,
        config=FormHandlerConfig(
            echo_filled_field=False,
            retry_field_msg="retry",
            unsupported_cmd_error_template="unsupported ({})",
            cancelling_because_of_error_template="error: {}",
            form_starting_template="form start ({} - cancel)",
            can_skip_field_template="({} to skip)",
            cant_skip_field_msg="cant skip this",
        ),
    )

    bot = MockedAsyncTeleBot(token="foobar")

    completed_ctxs: list[FormExitContext] = []

    async def on_form_completed(ctx: FormExitContext):
        completed_ctxs.append(ctx)

    fh.setup(bot, on_form_completed=on_form_completed)

    @bot.message_handler(commands=["form"])
    async def form_start(message: tg.Message) -> None:
        await fh.start(bot, user=message.from_user)

    telegram = TelegramServerMock()

    await telegram.send_message_to_bot(bot, user_id=1, text="/form")
    assert len(bot.method_calls) == 1
    assert_list_of_required_subdicts(
        extract_full_kwargs(bot.method_calls["send_message"]),
        [{"chat_id": 1, "text": "form start (/cancel - cancel)\n\nQuestion: your name"}],
    )
    bot.method_calls.clear()

    await telegram.send_message_to_bot(bot, user_id=1, text="test user")
    assert len(bot.method_calls) == 1
    assert_list_of_required_subdicts(
        extract_full_kwargs(bot.method_calls["send_message"]),
        [{"chat_id": 1, "text": "Question: your favourite food"}],
    )
    bot.method_calls.clear()

    await telegram.send_message_to_bot(bot, user_id=1, text="pizza (<b>)")
    assert len(bot.method_calls) == 1
    assert_list_of_required_subdicts(
        extract_full_kwargs(bot.method_calls["send_message"]),
        [{"chat_id": 1, "text": "Question: your pet's name (/skip to skip)"}],
    )
    bot.method_calls.clear()

    await telegram.send_message_to_bot(bot, user_id=1, text="/skip")
    assert len(bot.method_calls) == 0

    assert len(completed_ctxs) == 1
    ctx = completed_ctxs[0]
    assert ctx.bot is bot
    assert ctx.result == {
        "your favourite food": "pizza (<b>)",
        "your name": "test user",
        "your pet's name": None,
    }

    assert (
        f.result_to_html(ctx.result, lang=None)
        == "<b>your name</b>: test user\n<b>your favourite food</b>: pizza (&lt;b&gt;)"
    )


async def test_dynamic_select_form(redis: RedisInterface) -> None:
    bot_prefix = generate_str()

    form = Form.branching(
        [
            PlainTextField(
                name="name",
                required=True,
                query_message="Your name.",
                empty_text_error_msg="empty test error",
                result_formatting_opts=FormFieldResultFormattingOpts(descr="Name", is_multiline=False),
            ),
            DynamicSingleSelectField(
                name="number",
                required=True,
                query_message="Select a number.",
                invalid_enum_value_error_msg="nope",
                menu_row_width=1,
                result_formatting_opts=FormFieldResultFormattingOpts(descr="Number", is_multiline=False),
            ),
            DynamicSingleSelectField(
                name="letter",
                required=True,
                query_message="Select a letter.",
                invalid_enum_value_error_msg="nope",
                menu_row_width=1,
                result_formatting_opts=FormFieldResultFormattingOpts(descr="Letter", is_multiline=False),
            ),
        ]
    )

    handler = FormHandler[Any, Any](
        redis=redis,
        bot_prefix=bot_prefix,
        name="testing-form",
        form=form,
        config=FormHandlerConfig(
            echo_filled_field=False,
            retry_field_msg="retry",
            unsupported_cmd_error_template="unsupported ({})",
            cancelling_because_of_error_template="error: {}",
            form_starting_template="form start ({} - cancel)",
            can_skip_field_template="({} to skip)",
            cant_skip_field_msg="cant skip this",
        ),
    )

    bot = MockedAsyncTeleBot(token="foobar")

    completed_ctxs: list[FormExitContext] = []

    async def on_form_completed(ctx: FormExitContext):
        completed_ctxs.append(ctx)

    handler.setup(bot, on_form_completed=on_form_completed)

    @bot.message_handler(commands=["form"])
    async def form_start(message: tg.Message) -> None:
        await handler.start(
            bot,
            user=message.from_user,
            dynamic_data={
                "dynamic_options": {
                    "number": [
                        DynamicOption(id="1", label="1"),
                        DynamicOption(id="3", label="3"),
                        DynamicOption(id="1564", label="1564"),
                    ],
                    "letter": [
                        DynamicOption(id="A", label="A"),
                        DynamicOption(id="C", label="C"),
                        DynamicOption(id="L", label="L"),
                    ],
                }
            },
        )

    telegram = TelegramServerMock()

    await telegram.send_message_to_bot(bot, user_id=1, text="/form")
    assert len(bot.method_calls) == 1
    assert_list_of_required_subdicts(
        extract_full_kwargs(bot.method_calls["send_message"]),
        [{"chat_id": 1, "text": "form start (/cancel - cancel)\n\nYour name."}],
    )
    bot.method_calls.clear()

    await telegram.send_message_to_bot(bot, user_id=1, text="Alice")
    assert len(bot.method_calls) == 1
    assert_list_of_required_subdicts(
        reply_markups_to_dict(extract_full_kwargs(bot.method_calls["send_message"])),
        [
            {
                "chat_id": 1,
                "text": "Select a number.",
                "parse_mode": "HTML",
                "reply_markup": {
                    "keyboard": [[{"text": "1"}], [{"text": "3"}], [{"text": "1564"}]],
                    "one_time_keyboard": True,
                    "resize_keyboard": True,
                },
            }
        ],
    )
    bot.method_calls.clear()

    await telegram.send_message_to_bot(bot, user_id=1, text="bad answer")
    assert len(bot.method_calls) == 1
    assert_list_of_required_subdicts(
        extract_full_kwargs(bot.method_calls["send_message"]),
        [{"chat_id": 1, "text": "nope\n\nretry"}],
    )
    bot.method_calls.clear()

    await telegram.send_message_to_bot(bot, user_id=1, text="1564")
    assert len(bot.method_calls) == 1
    assert_list_of_required_subdicts(
        reply_markups_to_dict(extract_full_kwargs(bot.method_calls["send_message"])),
        [
            {
                "chat_id": 1,
                "text": "Select a letter.",
                "parse_mode": "HTML",
                "reply_markup": {
                    "keyboard": [[{"text": "A"}], [{"text": "C"}], [{"text": "L"}]],
                    "one_time_keyboard": True,
                    "resize_keyboard": True,
                },
            }
        ],
    )
    bot.method_calls.clear()

    await telegram.send_message_to_bot(bot, user_id=1, text="C")
    assert len(bot.method_calls) == 0

    assert len(completed_ctxs) == 1
    ctx = completed_ctxs[0]
    assert ctx.bot is bot
    assert ctx.result == {
        "name": "Alice",
        "number": "1564",
        "letter": "C",
    }

    assert form.result_to_html(ctx.result, lang=None) == "<b>Name</b>: Alice\n<b>Number</b>: 1564\n<b>Letter</b>: C"
