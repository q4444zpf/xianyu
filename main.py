"""闲鱼监控 CLI 入口。

子命令：
    login   首次扫码登录闲鱼并保存登录态
    once    跑一次抓取 + 对比 + 发邮件，便于调试
    run     进入定时循环，周期性抓取并推送
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import traceback

from loguru import logger

from config import (
    DB_PATH,
    LAST_LOGIN_ALERT_PATH,
    LOGS_DIR,
    STORAGE_STATE_PATH,
    AppConfig,
    load_config,
)
from src.auth import interactive_login, storage_state_exists
from src.crawler import XianyuCrawler
from src.login_reminder import mark_login_reminder_sent, should_send_login_reminder
from src.notifier import EmailNotifier
from src.storage import SeenItemStore


def _setup_logger() -> None:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, OSError):
                pass
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
    )
    logger.add(
        LOGS_DIR / "app.log",
        level="DEBUG",
        rotation="5 MB",
        retention=5,
        encoding="utf-8",
    )


async def _cmd_login() -> None:
    logger.info("启动登录流程，请在浏览器中扫码…")
    await interactive_login(STORAGE_STATE_PATH)


async def _notify_login_required_if_allowed(cfg: AppConfig, notifier: EmailNotifier, reason: str) -> None:
    """登录态不可用时发提醒邮件（受冷却时间限制）。"""
    if not should_send_login_reminder(
        LAST_LOGIN_ALERT_PATH, cfg.login_reminder_cooldown_seconds
    ):
        logger.info(
            "登录提醒邮件在冷却期内（{}s），本次不重复发送。",
            cfg.login_reminder_cooldown_seconds,
        )
        return
    try:
        notifier.send_login_action_required(reason)
        mark_login_reminder_sent(LAST_LOGIN_ALERT_PATH)
        logger.warning("已发送登录提醒邮件：{}", reason[:120])
    except Exception as exc:
        logger.error("发送登录提醒邮件失败：{}\n{}", exc, traceback.format_exc())


async def _run_one_cycle(cfg: AppConfig, store: SeenItemStore, notifier: EmailNotifier) -> None:
    if not storage_state_exists(STORAGE_STATE_PATH):
        msg = "未找到有效的登录文件，或 storage_state.json 中没有任何 cookie。"
        logger.error("{} 请先运行：python main.py login", msg)
        await _notify_login_required_if_allowed(cfg, notifier, msg)
        return

    crawler = XianyuCrawler(
        STORAGE_STATE_PATH,
        headless=cfg.headless,
        fetch_detail_cover_image=cfg.fetch_detail_cover_image,
    )
    try:
        fetch_result = await crawler.fetch_with_result(cfg.keyword, cfg.page_limit)
    except Exception as exc:
        logger.error("抓取失败：{}\n{}", exc, traceback.format_exc())
        return

    if not fetch_result.session_ok:
        logger.error("登录态或搜索不可用：{}", fetch_result.session_message)
        await _notify_login_required_if_allowed(
            cfg, notifier, fetch_result.session_message
        )
        return

    items = fetch_result.items

    if not items:
        logger.warning("本轮未抓到任何商品，跳过。")
        return

    logger.info("本轮共抓到 {} 条商品。", len(items))

    is_first_run = store.is_empty()
    new_items = store.filter_new(items)

    if is_first_run and not cfg.notify_on_first_run:
        store.upsert_many(items, notified=True)
        logger.info(
            "首次运行检测到 {} 条商品，已全部入库但不发邮件（如需首轮也发邮件，"
            "请将 NOTIFY_ON_FIRST_RUN 设为 true）。",
            len(items),
        )
        return

    if not new_items:
        logger.info("没有新增商品。")
        # 仍要更新本轮看到的记录（保持库与现网一致）
        store.upsert_many(items, notified=True)
        return

    to_mail = new_items[: cfg.email_max_items]
    rest_new = new_items[cfg.email_max_items :]
    rest_new_ids = {x.item_id for x in rest_new}

    logger.info(
        "待推送新增 {} 条，本封邮件最多 {} 条，实际发送 {} 条{}。",
        len(new_items),
        cfg.email_max_items,
        len(to_mail),
        f"，余 {len(rest_new)} 条已写入库（notified=0）、下轮继续推送" if rest_new else "",
    )
    if cfg.fetch_detail_cover_image and to_mail:
        try:
            await crawler.enrich_detail_covers(to_mail)
        except Exception as exc:
            logger.warning(
                "发信前详情主图拉取失败，邮件中将使用搜索列表中的图片链接: {}",
                exc,
            )
    try:
        notifier.send_new_items(
            cfg.keyword,
            to_mail,
            total_new=len(new_items),
        )
    except Exception as exc:
        logger.error("邮件发送失败：{}\n{}", exc, traceback.format_exc())
        # 失败时不入库，等下一轮重试
        return

    # 本轮抓取里：已发邮件的与旧商品标为已通知；未发完的新增标 notified=0 以便下轮继续进 new_items
    others = [it for it in items if it.item_id not in rest_new_ids]
    store.upsert_many(others, notified=True)
    if rest_new:
        store.upsert_many(rest_new, notified=False)


async def _cmd_once(cfg: AppConfig) -> None:
    cfg.validate_for_mail()
    store = SeenItemStore(DB_PATH)
    notifier = EmailNotifier(
        host=cfg.smtp_host,
        port=cfg.smtp_port,
        user=cfg.smtp_user,
        password=cfg.smtp_pass,
        mail_from=cfg.mail_from,
        mail_to=cfg.mail_to,
    )
    await _run_one_cycle(cfg, store, notifier)


async def _cmd_run(cfg: AppConfig) -> None:
    cfg.validate_for_mail()
    store = SeenItemStore(DB_PATH)
    notifier = EmailNotifier(
        host=cfg.smtp_host,
        port=cfg.smtp_port,
        user=cfg.smtp_user,
        password=cfg.smtp_pass,
        mail_from=cfg.mail_from,
        mail_to=cfg.mail_to,
    )
    logger.info(
        "开始定时监控：关键词={!r}  间隔={}s  每轮抓取={}",
        cfg.keyword,
        cfg.interval_seconds,
        cfg.page_limit,
    )
    while True:
        try:
            await _run_one_cycle(cfg, store, notifier)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.error("循环中出现未捕获异常：{}\n{}", exc, traceback.format_exc())
        logger.info("休眠 {}s 后进入下一轮…", cfg.interval_seconds)
        await asyncio.sleep(cfg.interval_seconds)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="闲鱼商品监控 + 邮件通知")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="首次扫码登录闲鱼并保存登录态")
    sub.add_parser("once", help="抓取一次并发送邮件（调试用）")
    sub.add_parser("run", help="进入定时循环")
    return parser.parse_args()


def main() -> None:
    _setup_logger()
    args = _parse_args()
    cfg = load_config()

    if args.cmd == "login":
        asyncio.run(_cmd_login())
    elif args.cmd == "once":
        asyncio.run(_cmd_once(cfg))
    elif args.cmd == "run":
        try:
            asyncio.run(_cmd_run(cfg))
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，退出。")


if __name__ == "__main__":
    main()
