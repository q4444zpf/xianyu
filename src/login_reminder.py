"""登录提醒邮件冷却，避免同一问题短时间重复发信。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def should_send_login_reminder(last_sent_file: Path, cooldown_seconds: int) -> bool:
    """若从未发送过，或距上次发送已超过 cooldown_seconds，则返回 True。"""
    if cooldown_seconds <= 0:
        return True
    if not last_sent_file.exists():
        return True
    try:
        raw = last_sent_file.read_text(encoding="utf-8").strip()
        prev = datetime.fromisoformat(raw)
        delta = (datetime.now() - prev).total_seconds()
        return delta >= cooldown_seconds
    except (ValueError, OSError):
        return True


def mark_login_reminder_sent(last_sent_file: Path) -> None:
    last_sent_file.parent.mkdir(parents=True, exist_ok=True)
    last_sent_file.write_text(
        datetime.now().replace(microsecond=0).isoformat(timespec="seconds"),
        encoding="utf-8",
    )
