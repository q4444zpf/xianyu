"""配置加载与校验。

从项目根目录的 .env 文件读取配置，集中暴露给其他模块使用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = ROOT_DIR / "logs"
STORAGE_STATE_PATH = DATA_DIR / "storage_state.json"
DB_PATH = DATA_DIR / "seen_items.db"
# 上次发送「需重新登录」提醒邮件的时间戳（ISO 本地时间一行文本）
LAST_LOGIN_ALERT_PATH = DATA_DIR / "last_login_alert.txt"

# 加载 .env（如果存在）
load_dotenv(ROOT_DIR / ".env")


def _get_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class AppConfig:
    keyword: str
    interval_seconds: int
    page_limit: int
    headless: bool
    notify_on_first_run: bool

    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    mail_from: str
    mail_to: list[str]

    # 登录失效提醒邮件的最小间隔（秒），避免 run 循环内重复轰炸邮箱
    login_reminder_cooldown_seconds: int

    # 单封商品推送邮件中最多包含的新增条数（超出部分留待后续轮次发送）
    email_max_items: int

    def validate_for_mail(self) -> None:
        """运行抓取/发送前校验邮件相关配置。"""
        missing = []
        if not self.smtp_host:
            missing.append("SMTP_HOST")
        if not self.smtp_user:
            missing.append("SMTP_USER")
        if not self.smtp_pass:
            missing.append("SMTP_PASS")
        if not self.mail_from:
            missing.append("MAIL_FROM")
        if not self.mail_to:
            missing.append("MAIL_TO")
        if missing:
            raise RuntimeError(
                f"以下配置未在 .env 中设置：{', '.join(missing)}。"
                "请参考 .env.example 完善配置。"
            )


def load_config() -> AppConfig:
    """读取并返回应用配置。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    mail_to_raw = os.getenv("MAIL_TO", "").strip()
    mail_to = [addr.strip() for addr in mail_to_raw.split(",") if addr.strip()]

    return AppConfig(
        keyword=os.getenv("KEYWORD", "像章").strip() or "像章",
        interval_seconds=max(60, _get_int("INTERVAL_SECONDS", 600)),
        page_limit=max(1, _get_int("PAGE_LIMIT", 30)),
        headless=_get_bool("HEADLESS", True),
        notify_on_first_run=_get_bool("NOTIFY_ON_FIRST_RUN", False),
        smtp_host=os.getenv("SMTP_HOST", "smtp.qq.com").strip(),
        smtp_port=_get_int("SMTP_PORT", 465),
        smtp_user=os.getenv("SMTP_USER", "").strip(),
        smtp_pass=os.getenv("SMTP_PASS", "").strip(),
        mail_from=os.getenv("MAIL_FROM", "").strip(),
        mail_to=mail_to,
        login_reminder_cooldown_seconds=max(
            0, _get_int("LOGIN_REMINDER_COOLDOWN_SECONDS", 21600)
        ),
        email_max_items=max(1, _get_int("EMAIL_MAX_ITEMS", 30)),
    )
