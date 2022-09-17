import math
import re


def mask(string: str, open_ratio: float) -> str:
    """some-very-secret-string -> some-ver****************"""
    open_ratio = max(min(open_ratio, 1.0), 0.0)
    open_characters = math.floor(len(string) * open_ratio)
    return string[:open_characters] + "*" * (len(string) - open_characters)


def telegram_html_escape(string: str) -> str:
    """See https://core.telegram.org/bots/api#html-style"""
    return string.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def long_text(s: str) -> str:
    cleaned_multiline = "\n".join([line.strip() for line in s.strip().splitlines()])
    paragraphs = cleaned_multiline.split("\n\n")
    return "\n\n".join([" ".join(p.splitlines()) for p in paragraphs])


command_regex = re.compile(r"^/(?P<command>\w+)(@(?P<bot_username>\w{5,64}))?")


def remove_command_prefix(message_text: str) -> str:
    return command_regex.sub("", message_text).strip()


def html_link(href: str, text: str) -> str:
    return f'<a href="{href}">{text}</a>'
