import json
from pathlib import Path

import bs4  # type: ignore
import requests  # type: ignore

if __name__ == "__main__":
    scripts_dir = Path(__file__).parent
    data_dir = scripts_dir / "data"
    print(data_dir)
    data_dir.mkdir(exist_ok=True, parents=True)
    html_file = data_dir / "languagenames.html"
    if True:
        # if not html_file.exists():
        resp = requests.get(
            "https://docs.translatehouse.org/projects/localization-guide/en/latest/l10n/languagenames.html"
        )
        html_file.write_bytes(resp.content)

    soup = bs4.BeautifulSoup(html_file.read_text(), features="html.parser")
    table_body = soup.find("tbody")
    lang_data_file = scripts_dir / "../telebot_components/data/language_data.json"
    with open(lang_data_file) as f:
        lang_data_raw = json.load(f)

    assert isinstance(table_body, bs4.Tag)
    for row in table_body.children:
        try:
            assert isinstance(row, bs4.Tag)
            cols = row.find_all("td")
            lang_el, localized_el = cols
            lang = str(lang_el.text)
            local_name = str(localized_el.text).capitalize()
            found = False
            for ld in lang_data_raw:
                if ld["name"].lower() == lang.lower():
                    ld["local_name"] = local_name
                    found = True
            if not found:
                print(f"Not found lang data for {lang}")
                continue
        except Exception:
            pass

    with open(lang_data_file, "w") as f:
        json.dump(lang_data_raw, f, indent=2, ensure_ascii=False)
