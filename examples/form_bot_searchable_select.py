import asyncio
import logging
import os
import re

from telebot import AsyncTeleBot
from telebot import types as tg
from telebot.runner import BotRunner

from telebot_components.form.field import (
    FormFieldResultFormattingOpts,
    PlainTextField,
    SearchableSingleSelectField,
    SearchableSingleSelectItem,
)
from telebot_components.form.form import Form
from telebot_components.form.handler import (
    FormExitContext,
    FormHandler,
    FormHandlerConfig,
)
from telebot_components.form.helpers.dynamic_enum import (
    EnumOption,
    create_dynamic_enum_class,
)
from telebot_components.redis_utils.emulation import RedisEmulation

COUNTRIES = [
    "Afghanistan",
    "Albania",
    "Algeria",
    "Andorra",
    "Angola",
    "Antigua and Barbuda",
    "Argentina",
    "Armenia",
    "Australia",
    "Austria",
    "Azerbaijan",
    "The Bahamas",
    "Bahrain",
    "Bangladesh",
    "Barbados",
    "Belarus",
    "Belgium",
    "Belize",
    "Benin",
    "Bhutan",
    "Bolivia",
    "Bosnia and Herzegovina",
    "Botswana",
    "Brazil",
    "Brunei",
    "Bulgaria",
    "Burkina Faso",
    "Burundi",
    "Cabo Verde",
    "Cambodia",
    "Cameroon",
    "Canada",
    "Central African Republic",
    "Chad",
    "Chile",
    "China",
    "Colombia",
    "Comoros",
    "Congo, Democratic Republic of the",
    "Congo, Republic of the",
    "Costa Rica",
    "Côte d'Ivoire",
    "Croatia",
    "Cuba",
    "Cyprus",
    "Czech Republic",
    "Denmark",
    "Djibouti",
    "Dominica",
    "Dominican Republic",
    "East Timor (Timor-Leste)",
    "Ecuador",
    "Egypt",
    "El Salvador",
    "Equatorial Guinea",
    "Eritrea",
    "Estonia",
    "Eswatini",
    "Ethiopia",
    "Fiji",
    "Finland",
    "France",
    "Gabon",
    "The Gambia",
    "Georgia",
    "Germany",
    "Ghana",
    "Greece",
    "Grenada",
    "Guatemala",
    "Guinea",
    "Guinea-Bissau",
    "Guyana",
    "Haiti",
    "Honduras",
    "Hungary",
    "Iceland",
    "India",
    "Indonesia",
    "Iran",
    "Iraq",
    "Ireland",
    "Israel",
    "Italy",
    "Jamaica",
    "Japan",
    "Jordan",
    "Kazakhstan",
    "Kenya",
    "Kiribati",
    "Korea, North",
    "Korea, South",
    "Kosovo",
    "Kuwait",
    "Kyrgyzstan",
    "Laos",
    "Latvia",
    "Lebanon",
    "Lesotho",
    "Liberia",
    "Libya",
    "Liechtenstein",
    "Lithuania",
    "Luxembourg",
    "Madagascar",
    "Malawi",
    "Malaysia",
    "Maldives",
    "Mali",
    "Malta",
    "Marshall Islands",
    "Mauritania",
    "Mauritius",
    "Mexico",
    "Micronesia, Federated States of",
    "Moldova",
    "Monaco",
    "Mongolia",
    "Montenegro",
    "Morocco",
    "Mozambique",
    "Myanmar (Burma)",
    "Namibia",
    "Nauru",
    "Nepal",
    "Netherlands",
    "New Zealand",
    "Nicaragua",
    "Niger",
    "Nigeria",
    "North Macedonia",
    "Norway",
    "Oman",
    "Pakistan",
    "Palau",
    "Panama",
    "Papua New Guinea",
    "Paraguay",
    "Peru",
    "Philippines",
    "Poland",
    "Portugal",
    "Qatar",
    "Romania",
    "Russia",
    "Rwanda",
    "Saint Kitts and Nevis",
    "Saint Lucia",
    "Saint Vincent and the Grenadines",
    "Samoa",
    "San Marino",
    "Sao Tome and Principe",
    "Saudi Arabia",
    "Senegal",
    "Serbia",
    "Seychelles",
    "Sierra Leone",
    "Singapore",
    "Slovakia",
    "Slovenia",
    "Solomon Islands",
    "Somalia",
    "South Africa",
    "Spain",
    "Sri Lanka",
    "Sudan",
    "Sudan, South",
    "Suriname",
    "Sweden",
    "Switzerland",
    "Syria",
    "Taiwan",
    "Tajikistan",
    "Tanzania",
    "Thailand",
    "Togo",
    "Tonga",
    "Trinidad and Tobago",
    "Tunisia",
    "Turkey",
    "Turkmenistan",
    "Tuvalu",
    "Uganda",
    "Ukraine",
    "United Arab Emirates",
    "United Kingdom",
    "United States",
    "Uruguay",
    "Uzbekistan",
    "Vanuatu",
    "Vatican City",
    "Venezuela",
    "Vietnam",
    "Yemen",
    "Zambia",
    "Zimbabwe",
]


form = Form(
    [
        SearchableSingleSelectField(
            name="country",
            required=True,
            query_message="Choose your country (send the name and pick one of the options from the menu).",
            EnumClass=create_dynamic_enum_class(
                "countries",
                [
                    EnumOption(
                        name=re.sub(r"[^\w]", "_", country_name).lower(),
                        value=SearchableSingleSelectItem(button_label=country_name),
                    )
                    for country_name in COUNTRIES
                ],
            ),
            no_matches_found="No matches found, try another query.",
            choose_from_matches="Pick the option from the menu or try another query",
            result_formatting_opts=FormFieldResultFormattingOpts(descr="Country"),
        ),
        PlainTextField(
            name="city",
            required=True,
            query_message="Enter your city name.",
            empty_text_error_msg="Please enter some text.",
            result_formatting_opts=FormFieldResultFormattingOpts(descr="City"),
        ),
    ]
)


async def create_branching_form_bot():
    bot_prefix = "form-bot-countries"
    redis = RedisEmulation()
    form_handler = FormHandler(
        redis=redis,
        bot_prefix=bot_prefix,
        name="main",
        form=form,
        config=FormHandlerConfig(
            echo_filled_field=False,
            retry_field_msg="Please correct the value.",
            unsupported_cmd_error_template="Unsupported cmd, supported are: {}",
            cancelling_because_of_error_template="Error: {}",
            form_starting_template="Please fill the form.",
            can_skip_field_template="Skip with {}",
            cant_skip_field_msg="This is a mandatory field.",
        ),
    )

    bot = AsyncTeleBot(os.environ["TOKEN"])

    @bot.message_handler(commands=["start"])
    async def start_form(message: tg.Message):
        await form_handler.start(bot, message.from_user)

    async def on_form_completed(ctx: FormExitContext):
        await ctx.bot.send_message(
            ctx.last_update.from_user.id, form.result_to_html(ctx.result, None), parse_mode="HTML"
        )

    form_handler.setup(bot, on_form_completed=on_form_completed)

    return BotRunner(bot_prefix=bot_prefix, bot=bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def main() -> None:
        br = await create_branching_form_bot()
        await br.run_polling()

    asyncio.run(main())
