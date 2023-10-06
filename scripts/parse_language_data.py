import csv
import dataclasses
import json
from pathlib import Path
from typing import List, Literal, TypedDict, Union, cast

from typing_extensions import NotRequired

from telebot_components.language import LanguageData

DIR = Path(__file__).parent

# language data downloaded from here:
# https://github.com/mattcg/language-subtag-registry/blob/master/data/json/registry.json
with open(DIR / "registry.json") as f:
    iana_subtag_registry = json.load(f)

# flags manually associated
with open(DIR / "language-flags.tsv") as f:
    d = csv.DictReader(f, delimiter="\t", fieldnames=["flag", "language"])
    flags_data: dict[str, str] = {row["language"]: row["flag"] for row in d}


Subtag = TypedDict(
    "Subtag",
    {
        "Type": Union[
            Literal["language"],
            Literal["extlang"],
            Literal["script"],
            Literal["region"],
            Literal["variant"],
            Literal["grandfathered"],
            Literal["redundant"],
        ],
        "Subtag": NotRequired[str],
        "Description": List[str],
        "Added": str,
        "Suppress-Script": NotRequired[str],
        "Scope": NotRequired[
            Union[Literal["macrolanguage"], Literal["collection"], Literal["special"], Literal["private-use"]]
        ],
        "Macrolanguage": NotRequired[str],
        "Comments": NotRequired[List[str]],
        "Deprecated": NotRequired[str],
        "Preferred-Value": NotRequired[str],
        "Prefix": NotRequired[List[str]],
        "Tag": NotRequired[str],
    },
)


languages: list[LanguageData] = []

for subtag in iana_subtag_registry:
    subtag = cast(Subtag, subtag)
    if (
        subtag.get("Deprecated")
        or subtag.get("Type") != "language"
        or not subtag.get("Subtag")
        or not subtag.get("Description")
        or subtag.get("Scope") in {"collection", "special", "private-use"}
    ):
        continue
    name = subtag["Description"][0]
    if "sign" in name.lower():
        continue
    code = subtag["Subtag"]
    languages.append(LanguageData(code=code, name=name, emoji=flags_data.get(code)))


languages.sort(key=lambda lang: (lang.emoji is None, lang.code))


with open(DIR / "../telebot_components/data/language_data.json", "w") as f:
    json.dump(
        [dataclasses.asdict(lang) for lang in languages],
        f,
        indent=2,
        ensure_ascii=False,
    )
