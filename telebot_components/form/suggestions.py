from dataclasses import dataclass


@dataclass
class ResponseSuggestionsConfig:
    suggested_responses_count: int
    decline_all_suggestions_button_caption: str


def suggested_response_field_name(original_field_name: str):
    return f"__suggested_response_for_{original_field_name}"
