import abc
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Iterable,
    Mapping,
    Optional,
    TypeGuard,
    Union,
)

from telebot import AsyncTeleBot, types


class Language(Enum):
    """
    Static language enum for use in legacy code; under the hood it's converted to LanguageData
    """

    EN = "en"
    UK = "uk"
    RU = "ru"
    PL = "pl"

    @classmethod
    def parse(cls, string: str) -> "Language":
        return Language(string.lower())

    def __str__(self) -> str:
        return self.value

    def emoji(self) -> str:
        ld = self.as_data()
        return ld.emoji or ld.code.upper()

    def as_data(self) -> "LanguageData":
        return LanguageData.lookup(self.value)


@dataclass(frozen=True)
class LanguageData:
    code: str  # IETF code in lowercase, same as used in Telegram
    name: str
    local_name: Optional[str] = None
    emoji: Optional[str] = None

    def __str__(self) -> str:
        return self.code

    def __hash__(self) -> int:
        return hash(self.code)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, LanguageData):
            return self.code == other.code
        elif isinstance(other, Language):
            return self.code == other.value
        else:
            return False

    def __lt__(self, other: Any) -> bool:  # support ordering by lang code
        if isinstance(other, LanguageData):
            return self.code < other.code
        elif isinstance(other, Language):
            return self.code < other.value
        else:
            raise ValueError("Language data can only be compared (lexicographically) with other language data")

    _DATA: ClassVar[Optional[dict[str, "LanguageData"]]] = None

    @classmethod
    def all(cls) -> dict[str, "LanguageData"]:
        if cls._DATA is None:
            with open(Path(__file__).parent / "data/language_data.json") as f:
                raw_data = json.load(f)
            language_data_list = [LanguageData(**item) for item in raw_data]
            cls._DATA = {lang.code: lang for lang in language_data_list}
        return cls._DATA

    @classmethod
    def lookup(cls, code: str) -> "LanguageData":
        code = code.lower()
        code = code.split("-")[0]  # strip codes like "en-gb" to macrolanguages ("en")
        ld = cls.all().get(code)
        if ld is None:
            raise KeyError(f"Unexpected language code: {code!r}")
        else:
            return ld


AnyLanguage = Union[Language, LanguageData]


def any_language_to_language_data(any_lang: AnyLanguage) -> LanguageData:
    if isinstance(any_lang, Language):
        return any_lang.as_data()
    else:
        return any_lang


MaybeLanguage = Optional[AnyLanguage]  # None = multilang mode is off, using regular strings

MultilangText = Union[Mapping[Language, str], Mapping[LanguageData, str]]

AnyText = Union[str, MultilangText]


def is_multilang_text(t: Any) -> TypeGuard[MultilangText]:
    if not isinstance(t, dict):
        return False
    for key, value in t.items():
        if not isinstance(key, Language) and not isinstance(key, LanguageData):
            return False
        if not isinstance(value, str):
            return False
    return True


def is_any_text(t: Any) -> TypeGuard[AnyText]:
    return is_multilang_text(t) or isinstance(t, str)


def language_variants(language: AnyLanguage) -> list[AnyLanguage]:
    res: list[AnyLanguage] = [language]
    if isinstance(language, Language):
        res.append(language.as_data())
    else:
        try:
            res.append(Language.parse(language.code))
        except Exception:
            pass
    return res


def validate_multilang_text(t: Any, languages: Iterable[AnyLanguage]) -> MultilangText:
    if not is_multilang_text(t):
        raise TypeError(f"Not a multilang text: {t}")
    for language in languages:
        if all(variant not in t for variant in language_variants(language)):
            raise ValueError(f"Multilang text misses localisation to '{language}': {t}")
    return t


def vaildate_singlelang_text(t: Any) -> str:
    if not isinstance(t, str):
        raise TypeError(f"Single language text must be a string, found {type(t).__name__}: {t}")
    return t


def any_text_to_str(t: AnyText, language: MaybeLanguage) -> str:
    if language is None:
        if isinstance(t, str):
            return t
        else:
            raise ValueError("MultilangText requires a valid Language / LanguageData for localisation")
    else:
        if isinstance(t, str):
            raise ValueError("Plain string text requires language=None")
        else:
            keys_to_check = language_variants(language)
            for key in keys_to_check:
                # NOTE: this ignore is needed because ideally we would like to type MultilangText as
                # {Language | LanguageData -> str}, but it fails because Mapping is invariant in regard to key type
                # so, its type is {Language -> str} | {LanguageData -> str}, but we can .get() anything anyway
                localised = t.get(key)  # type: ignore
                if isinstance(localised, str):
                    return localised
            else:
                raise ValueError(
                    f"No valid localisation found for language '{language}' (checked keys {keys_to_check})"
                )


@dataclass
class LanguageChangeContext:
    bot: AsyncTeleBot
    message: Optional[types.Message]
    message_id: Optional[int]
    user: types.User
    language: LanguageData


LanguageChangeHandler = Callable[[LanguageChangeContext], Awaitable[Any]]


class LanguageStoreInterface(abc.ABC):
    """See telebot_components.stores.language for implementation."""

    @abc.abstractmethod
    def validate_multilang(self, ml_text: Any) -> None:
        """Checks that a multilanguage text includes all languages in the store"""
        ...

    @abc.abstractmethod
    async def get_user_language(self, user: types.User) -> LanguageData: ...

    @abc.abstractmethod
    async def setup(self, bot: AsyncTeleBot, on_language_change: Optional[LanguageChangeHandler] = None): ...
