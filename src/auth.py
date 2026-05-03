"""闲鱼登录态管理。

首次执行 `python main.py login` 时启动有头浏览器，由用户在窗口中
扫码登录闲鱼网页版，登录成功后将 cookies / localStorage 持久化到
`data/storage_state.json`，后续抓取直接复用，避免重复触发滑块。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from loguru import logger
from playwright.async_api import async_playwright

GOOFISH_HOME = "https://www.goofish.com/"
GOOFISH_SEARCH = "https://www.goofish.com/search?q={kw}"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# 无交互 stdin（如 IDE 后台任务）时，通过创建此空文件表示「已登录完毕，可以保存」
LOGIN_READY_FLAG = "login_ready.flag"


async def _wait_for_user_save_confirm(storage_path: Path, timeout_seconds: int) -> None:
    """交互终端：按 Enter；非交互：在 data 目录下创建 login_ready.flag。"""
    prompt = ">>> 已在浏览器中登录完毕？按 Enter 保存到 data/storage_state.json: "
    if sys.stdin.isatty():
        await asyncio.to_thread(input, prompt)
        return

    flag = storage_path.parent / LOGIN_READY_FLAG
    if flag.exists():
        try:
            flag.unlink()
        except OSError:
            pass

    logger.warning(
        "当前没有可用的交互式标准输入（常见于 IDE 自动运行任务）。\n"
        "请在浏览器中完成登录后，手动新建空文件（内容可为空）：\n  {}\n"
        "创建后程序会在 {} 秒内检测到并保存登录态；取消请 Ctrl+C。",
        flag.resolve(),
        timeout_seconds,
    )
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        await asyncio.sleep(1.0)
        if flag.exists():
            try:
                flag.unlink()
            except OSError:
                pass
            logger.info("已检测到 {}，继续保存登录态。", LOGIN_READY_FLAG)
            return
    raise asyncio.TimeoutError


async def interactive_login(storage_path: Path, timeout_seconds: int = 300) -> None:
    """启动有头浏览器引导用户扫码登录，**由用户在终端按 Enter 确认后再保存**登录态。

    说明：仅靠 cookie / 页面文案自动判断容易误判（未登录也可能已有部分
    cookie，或登录按钮文案与选择器不一致），因此改为人工确认。

    Args:
        storage_path: storage_state.json 落盘路径。
        timeout_seconds: 等待确认的最长时间（秒）：交互模式为「按 Enter」；
            非交互模式为等待创建 login_ready.flag 的最长时间。
    """
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    if sys.stdin.isatty():
        logger.info("建议在 {} 秒内在浏览器中完成登录，再回终端按 Enter。", timeout_seconds)
    else:
        logger.info("非交互终端：请在 {} 秒内完成浏览器登录并创建 {}", timeout_seconds, LOGIN_READY_FLAG)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--start-maximized"])
        context = await browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()
        logger.info("打开闲鱼首页，请在浏览器窗口中点击右上角登录并扫码…")
        await page.goto(GOOFISH_HOME, wait_until="domcontentloaded")

        if sys.stdin.isatty():
            logger.info(
                "请在浏览器中完成登录。确认网页上已是登录状态后，回到本终端按 Enter —— "
                "只有按 Enter 后才会写入登录文件（未按 Enter 可直接关闭浏览器并 Ctrl+C 退出）。"
            )
        try:
            await _wait_for_user_save_confirm(storage_path, timeout_seconds)
        except asyncio.TimeoutError:
            logger.error("等待确认超时（{}s），未写入登录文件。请重试。", timeout_seconds)
            await context.close()
            await browser.close()
            return

        await context.storage_state(path=str(storage_path))
        logger.success("登录态已保存到：{}", storage_path)
        await context.close()
        await browser.close()


def storage_state_exists(storage_path: Path) -> bool:
    """存在且含至少一条 cookie（空模板文件不算已登录）。"""
    if not storage_path.exists() or storage_path.stat().st_size < 30:
        return False
    try:
        data = json.loads(storage_path.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        return len(cookies) > 0
    except (json.JSONDecodeError, OSError):
        return False
