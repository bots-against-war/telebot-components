import calendar
import datetime
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from telebot import types as tg

logger = logging.getLogger(__name__)


class CalendarAction(Enum):
    NOOP = "noop"
    SELECT = "select"
    UPDATE = "update"


@dataclass(frozen=True)
class CalendarCallbackPayload:
    """We can't use "normal" `telebot.callback_data.CallbackData` here because field handler
    defines its own INLINE_FIELD_CALLBACK_DATA and expects us to pack everything into its
    `payload` field"""

    action: CalendarAction
    year: Optional[int] = None
    month: Optional[int] = None
    day: Optional[int] = None

    def __post_init__(self):
        if self.action is CalendarAction.SELECT:
            if self.year is None or self.month is None or self.day is None:
                raise RuntimeError(
                    "All fields must be specified for SELECT action, but "
                    + f"{self.year = } {self.month = } {self.day = }"
                )
        if self.action is CalendarAction.UPDATE:
            if self.year is None or self.month is None:
                raise RuntimeError(f"Year and month must be specified for UPDATE action")

    def dump(self) -> str:
        parts: list[str] = [self.action.value]
        if self.year is not None:
            parts.append(f"y{self.year}")
        if self.month is not None:
            parts.append(f"m{self.month}")
        if self.day is not None:
            parts.append(f"d{self.day}")
        return "_".join(parts)

    @classmethod
    def load(cls, dump: str) -> Optional["CalendarCallbackPayload"]:
        try:
            parts = dump.split("_")
            kwargs: dict = {"action": CalendarAction(parts.pop(0))}
            for part in parts:
                if part.startswith("y"):
                    kwargs["year"] = int(part.removeprefix("y"))
                elif part.startswith("m"):
                    kwargs["month"] = int(part.removeprefix("m"))
                elif part.startswith("d"):
                    kwargs["day"] = int(part.removeprefix("d"))
                else:
                    raise RuntimeError(f"Unknown payload part: {part!r}")
            return CalendarCallbackPayload(**kwargs)
        except Exception:
            logger.exception("Unexpected error parsing CalendarCallbackPayload")
            return None


WEEKDAY_NAMES_EN = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
WEEKDAY_NAMES_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


@dataclass(frozen=True)
class CalendarKeyboardConfig:
    prev_month_button: str = "<"
    next_month_button: str = ">"
    weekday_names: tuple[str, str, str, str, str, str, str] = WEEKDAY_NAMES_EN
    future_only: bool = True
    today_transform: Callable[[str], str] = lambda day: f"[{day}]"
    selected_transform: Callable[[str], str] = lambda day: f"✅ {day}"


def calendar_keyboard(
    year: Optional[int],
    month: Optional[int],
    new_callback_data_with_payload: Callable[[str], str],
    config: CalendarKeyboardConfig = CalendarKeyboardConfig(),
    selected_date: Optional[datetime.date] = None,
) -> tg.InlineKeyboardMarkup:
    def noop_button(label: str) -> tg.InlineKeyboardButton:
        return tg.InlineKeyboardButton(
            label,
            callback_data=new_callback_data_with_payload(CalendarCallbackPayload(CalendarAction.NOOP).dump()),
        )

    now = datetime.datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month
    is_current_month = (month == now.month) and (year == now.year)
    keyboard = []
    keyboard.append([noop_button(f"{calendar.month_name[month]} {str(year)}")])
    keyboard.append([noop_button(weekday_name) for weekday_name in config.weekday_names])

    my_calendar = calendar.monthcalendar(year, month)
    for week in my_calendar:
        row = []
        week_has_buttons = False
        for day in week:
            ignore_day = day == 0  # i.e. day is outside this month
            if config.future_only:
                ignore_day = ignore_day or (is_current_month and day < now.day)

            if ignore_day:
                row.append(noop_button(" "))
            else:
                week_has_buttons = True
                day_label = str(day)
                if selected_date is not None and datetime.date(year, month, day) == selected_date:
                    day_label = config.selected_transform(day_label)
                elif is_current_month and day == now.day:
                    day_label = config.today_transform(day_label)
                row.append(
                    tg.InlineKeyboardButton(
                        day_label,
                        callback_data=new_callback_data_with_payload(
                            CalendarCallbackPayload(CalendarAction.SELECT, year, month, day).dump()
                        ),
                    )
                )
        if week_has_buttons:
            keyboard.append(row)

    if config.future_only and is_current_month:
        prev_button = noop_button(" ")
    else:
        some_day_prev_month = datetime.datetime(year, month, 15) - datetime.timedelta(days=32)
        prev_button = tg.InlineKeyboardButton(
            config.prev_month_button,
            callback_data=new_callback_data_with_payload(
                CalendarCallbackPayload(
                    CalendarAction.UPDATE, year=some_day_prev_month.year, month=some_day_prev_month.month
                ).dump()
            ),
        )
    some_day_next_month = datetime.datetime(year, month, 15) + datetime.timedelta(days=32)
    next_button = tg.InlineKeyboardButton(
        config.next_month_button,
        callback_data=new_callback_data_with_payload(
            CalendarCallbackPayload(
                CalendarAction.UPDATE, year=some_day_next_month.year, month=some_day_next_month.month
            ).dump()
        ),
    )
    keyboard.append([prev_button, next_button])
    return tg.InlineKeyboardMarkup(keyboard)