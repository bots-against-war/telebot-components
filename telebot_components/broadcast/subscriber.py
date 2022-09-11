from typing import Optional, TypedDict


class Subscriber(TypedDict):
    user_id: int
    username: Optional[str]
    full_name: str
    subscribed_at: float
