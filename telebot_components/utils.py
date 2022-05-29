from typing import Optional


def telegram_message_url(group_id: int, message_id: int, thread_op_message_id: Optional[int] = None):
    group_id_str = str(group_id).replace("-100", "")
    message_url = f"https://t.me/c/{group_id_str}/{message_id}"
    if thread_op_message_id is not None:
        message_url += f"?thread={thread_op_message_id}"
    return message_url


def join_paragraphs(lines: list[str]) -> str:
    return "\n\n".join(lines)
