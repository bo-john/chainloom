"""Display UTC database timestamps in Beijing time without host timezone dependencies."""

from datetime import UTC, datetime, timedelta, timezone

BEIJING_TIMEZONE = timezone(timedelta(hours=8), "北京时间")


def format_beijing_time(value: str) -> str:
    try:
        timestamp = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return "时间未知"
    if timestamp.tzinfo is None:
        # SQLite timestamps in existing versions are always stored in UTC.
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
