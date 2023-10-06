from typing import Any, Optional, Type

import pytest

from telebot_components.language import AnyLanguage
from telebot_components.stores.language import (
    AnyText,
    Language,
    MaybeLanguage,
    any_text_to_str,
    validate_multilang_text,
)


@pytest.mark.parametrize(
    "any_text, language, expected_str",
    [
        pytest.param("hello world", None, "hello world"),
        pytest.param({Language.RU: "пример"}, Language.RU, "пример"),
        pytest.param({Language.RU: "пример", Language.EN: "example"}, Language.EN, "example"),
    ],
)
def test_any_text_to_str(any_text: AnyText, language: MaybeLanguage, expected_str: str):
    assert any_text_to_str(any_text, language) == expected_str


@pytest.mark.parametrize(
    "any_text, language, expected_error_message",
    [
        pytest.param("hello world", Language.RU, "Plain string text requires language=None"),
        pytest.param({}, Language.RU, "No valid localisation found for language 'ru'"),
        pytest.param({Language.RU: "текст"}, Language.EN, "No valid localisation found for language 'en'"),
        pytest.param(
            {Language.RU: "текст"}, None, "MultilangText requires a valid Language / LanguageData for localisation"
        ),
    ],
)
def test_any_text_to_str_value_errors(any_text: Any, language: MaybeLanguage, expected_error_message: str):
    with pytest.raises(ValueError, match=expected_error_message):
        assert any_text_to_str(any_text, language)


@pytest.mark.parametrize(
    "ml_text, languages, expected_error_message, ErrorClass",
    [
        pytest.param(
            {Language.RU: "пример"},
            [Language.RU],
            None,
            None,
        ),
        pytest.param(
            {Language.RU: "пример", Language.UK: "приклад"},
            [Language.UK, Language.RU],
            None,
            None,
        ),
        pytest.param(
            {Language.RU: "пример", Language.UK: "приклад", Language.EN: "example"},
            [Language.UK, Language.RU],
            None,
            None,
            id="extra languages are fine",
        ),
        pytest.param(
            {Language.UK: "приклад"},
            [Language.UK, Language.RU],
            "Multilang text misses localisation to 'ru'",
            ValueError,
        ),
        pytest.param(
            {Language.UK: "приклад", Language.RU: "пример "},
            [Language.UK, Language.EN, Language.RU],
            "Multilang text misses localisation to 'en'",
            ValueError,
        ),
        pytest.param(
            "не словарь",
            [Language.UK, Language.RU],
            "Not a multilang text: не словарь",
            TypeError,
        ),
        pytest.param(
            1312,
            [Language.UK, Language.RU],
            "Not a multilang text: 1312",
            TypeError,
        ),
    ],
)
def test_multilang_text_validation(
    ml_text: Any,
    languages: list[AnyLanguage],
    expected_error_message: Optional[str],
    ErrorClass: Optional[Type[Exception]],
):
    if expected_error_message is not None:
        if ErrorClass is None:
            raise TypeError("test configuration error: ErrorClass is required")
        with pytest.raises(ErrorClass, match=expected_error_message):
            validate_multilang_text(ml_text, languages)
    else:
        assert validate_multilang_text(ml_text, languages) is ml_text
