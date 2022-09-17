import datetime

import pytest

from telebot_components.form.helpers.calendar_keyboard import (
    CalendarAction,
    CalendarCallbackPayload,
    CalendarKeyboardConfig,
    calendar_keyboard,
)


@pytest.mark.parametrize(
    "payload",
    [
        CalendarCallbackPayload(CalendarAction.NOOP),
        CalendarCallbackPayload(CalendarAction.NOOP, year=12345),
        CalendarCallbackPayload(CalendarAction.NOOP, month=1451),
        CalendarCallbackPayload(CalendarAction.NOOP, day=12145),
        CalendarCallbackPayload(CalendarAction.NOOP, year=112451, month=1),
        CalendarCallbackPayload(CalendarAction.NOOP, year=1, day=0),
        CalendarCallbackPayload(CalendarAction.NOOP, month=1, day=1234),
        CalendarCallbackPayload(CalendarAction.NOOP, year=1, month=12341234, day=134),
        CalendarCallbackPayload(CalendarAction.SELECT, year=2022, month=6, day=23),
        CalendarCallbackPayload(CalendarAction.UPDATE, year=2023, month=1, day=23),
        CalendarCallbackPayload(CalendarAction.UPDATE, year=2023, month=1),
    ],
)
def test_calendar_callback_payload(payload: CalendarCallbackPayload):
    assert payload == CalendarCallbackPayload.load(payload.dump())


def test_calendar_keyboard():
    assert calendar_keyboard(
        year=2022,
        month=9,
        new_callback_data_with_payload=lambda x: x,
        config=CalendarKeyboardConfig(),
    ).to_dict()["inline_keyboard"] == [
        [{"text": "September 2022", "callback_data": "noop"}],
        [
            {"text": "Mo", "callback_data": "noop"},
            {"text": "Tu", "callback_data": "noop"},
            {"text": "We", "callback_data": "noop"},
            {"text": "Th", "callback_data": "noop"},
            {"text": "Fr", "callback_data": "noop"},
            {"text": "Sa", "callback_data": "noop"},
            {"text": "Su", "callback_data": "noop"},
        ],
        [
            {"text": " ", "callback_data": "noop"},
            {"text": " ", "callback_data": "noop"},
            {"text": " ", "callback_data": "noop"},
            {"text": " ", "callback_data": "noop"},
            {"text": " ", "callback_data": "noop"},
            {"text": "[17]", "callback_data": "select_y2022_m9_d17"},
            {"text": "18", "callback_data": "select_y2022_m9_d18"},
        ],
        [
            {"text": "19", "callback_data": "select_y2022_m9_d19"},
            {"text": "20", "callback_data": "select_y2022_m9_d20"},
            {"text": "21", "callback_data": "select_y2022_m9_d21"},
            {"text": "22", "callback_data": "select_y2022_m9_d22"},
            {"text": "23", "callback_data": "select_y2022_m9_d23"},
            {"text": "24", "callback_data": "select_y2022_m9_d24"},
            {"text": "25", "callback_data": "select_y2022_m9_d25"},
        ],
        [
            {"text": "26", "callback_data": "select_y2022_m9_d26"},
            {"text": "27", "callback_data": "select_y2022_m9_d27"},
            {"text": "28", "callback_data": "select_y2022_m9_d28"},
            {"text": "29", "callback_data": "select_y2022_m9_d29"},
            {"text": "30", "callback_data": "select_y2022_m9_d30"},
            {"text": " ", "callback_data": "noop"},
            {"text": " ", "callback_data": "noop"},
        ],
        [{"text": " ", "callback_data": "noop"}, {"text": ">", "callback_data": "update_y2022_m10"}],
    ]

    assert calendar_keyboard(
        year=2020,
        month=2,
        new_callback_data_with_payload=lambda x: x,
        config=CalendarKeyboardConfig(
            prev_month_button="previous",
            next_month_button="next",
            weekday_names=("a", "b", "c", "d", "e", "f", "g"),
            future_only=False,
            today_transform=lambda x: x,
            selected_transform=lambda x: f"xXx__{x}__xXx",
        ),
        selected_date=datetime.date(2020, 2, 16),
    ).to_dict()["inline_keyboard"] == [
        [{"text": "February 2020", "callback_data": "noop"}],
        [
            {"text": "a", "callback_data": "noop"},
            {"text": "b", "callback_data": "noop"},
            {"text": "c", "callback_data": "noop"},
            {"text": "d", "callback_data": "noop"},
            {"text": "e", "callback_data": "noop"},
            {"text": "f", "callback_data": "noop"},
            {"text": "g", "callback_data": "noop"},
        ],
        [
            {"text": " ", "callback_data": "noop"},
            {"text": " ", "callback_data": "noop"},
            {"text": " ", "callback_data": "noop"},
            {"text": " ", "callback_data": "noop"},
            {"text": " ", "callback_data": "noop"},
            {"text": "1", "callback_data": "select_y2020_m2_d1"},
            {"text": "2", "callback_data": "select_y2020_m2_d2"},
        ],
        [
            {"text": "3", "callback_data": "select_y2020_m2_d3"},
            {"text": "4", "callback_data": "select_y2020_m2_d4"},
            {"text": "5", "callback_data": "select_y2020_m2_d5"},
            {"text": "6", "callback_data": "select_y2020_m2_d6"},
            {"text": "7", "callback_data": "select_y2020_m2_d7"},
            {"text": "8", "callback_data": "select_y2020_m2_d8"},
            {"text": "9", "callback_data": "select_y2020_m2_d9"},
        ],
        [
            {"text": "10", "callback_data": "select_y2020_m2_d10"},
            {"text": "11", "callback_data": "select_y2020_m2_d11"},
            {"text": "12", "callback_data": "select_y2020_m2_d12"},
            {"text": "13", "callback_data": "select_y2020_m2_d13"},
            {"text": "14", "callback_data": "select_y2020_m2_d14"},
            {"text": "15", "callback_data": "select_y2020_m2_d15"},
            {"text": "xXx__16__xXx", "callback_data": "select_y2020_m2_d16"},
        ],
        [
            {"text": "17", "callback_data": "select_y2020_m2_d17"},
            {"text": "18", "callback_data": "select_y2020_m2_d18"},
            {"text": "19", "callback_data": "select_y2020_m2_d19"},
            {"text": "20", "callback_data": "select_y2020_m2_d20"},
            {"text": "21", "callback_data": "select_y2020_m2_d21"},
            {"text": "22", "callback_data": "select_y2020_m2_d22"},
            {"text": "23", "callback_data": "select_y2020_m2_d23"},
        ],
        [
            {"text": "24", "callback_data": "select_y2020_m2_d24"},
            {"text": "25", "callback_data": "select_y2020_m2_d25"},
            {"text": "26", "callback_data": "select_y2020_m2_d26"},
            {"text": "27", "callback_data": "select_y2020_m2_d27"},
            {"text": "28", "callback_data": "select_y2020_m2_d28"},
            {"text": "29", "callback_data": "select_y2020_m2_d29"},
            {"text": " ", "callback_data": "noop"},
        ],
        [
            {"text": "previous", "callback_data": "update_y2020_m1"},
            {"text": "next", "callback_data": "update_y2020_m3"},
        ],
    ]
