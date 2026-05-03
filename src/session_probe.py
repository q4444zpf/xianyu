"""探测闲鱼登录态是否仍能正常发起搜索（避免 cookie 过期后静默抓空）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import quote

from loguru import logger
from playwright.async_api import Response, TimeoutError as PWTimeoutError, async_playwright

try:
    from playwright_stealth import Stealth as _StealthCls  # type: ignore
except Exception:  # pragma: no cover
    _StealthCls = None  # type: ignore

from .auth import DEFAULT_USER_AGENT
from .crawler import SEARCH_URL_TEMPLATE, _is_blocked, _is_pc_search_mtop_url, _strip_jsonp


def _classify_mtop_search_body(data: dict) -> str:
    """将搜索接口 JSON 粗分为 ok / err / unknown（不打 warning，供探测用）。"""
    ret = data.get("ret") or []
    if ret and isinstance(ret[0], str):
        head = ret[0]
        if any(
            m in head
            for m in ("ERROR::", "FAIL", "RGV587", "被挤爆", "SESSION", "非法请求", "LOGIN")
        ):
            return "err"
        if "SUCCESS" in head or "调用成功" in head:
            return "ok"
    data_obj = data.get("data")
    if isinstance(data_obj, dict):
        if data_obj.get("resultList"):
            return "ok"
        if data_obj.get("url") and "mini_login" in str(data_obj.get("url", "")):
            return "err"
    return "unknown"


async def probe_storage_state_usable(
    storage_state_path: Path,
    headless: bool,
    keyword: str,
) -> tuple[bool, str]:
    """打开一次搜索页，根据 mtop 响应与页面状态判断登录态是否可用。

    Returns:
        (True, "") 表示可用；(False, 原因) 表示不可用。
    """
    if not storage_state_path.exists():
        return False, "未找到登录态文件 data/storage_state.json"

    mtop_flags: list[str] = []

    async def on_response(resp: Response) -> None:
        if not _is_pc_search_mtop_url(resp.url):
            return
        try:
            body_text = await resp.text()
        except Exception:
            return
        try:
            data = json.loads(_strip_jsonp(body_text))
        except json.JSONDecodeError:
            return
        mtop_flags.append(_classify_mtop_search_body(data))

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            context = await browser.new_context(
                storage_state=str(storage_state_path),
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
            )
            if _StealthCls is not None:
                try:
                    await _StealthCls().apply_stealth_async(context)
                except Exception:
                    pass
            page = await context.new_page()
            page.on("response", lambda r: asyncio.create_task(on_response(r)))

            url = SEARCH_URL_TEMPLATE.format(kw=quote(keyword))
            logger.info("登录态探测：打开 {}", url)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except PWTimeoutError:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=25000)
            except PWTimeoutError:
                pass
            await asyncio.sleep(3.0)

            purl = (page.url or "").lower()
            if "login" in purl or "passport.goofish.com" in purl:
                await context.close()
                await browser.close()
                return False, "页面已跳转登录相关地址，登录态可能已失效"

            if await _is_blocked(page):
                await context.close()
                await browser.close()
                return False, "命中风控或验证页，无法确认搜索可用"

            if "err" in mtop_flags:
                await context.close()
                await browser.close()
                return False, "搜索接口返回错误（挤爆/会话失效等），请重新执行 python main.py login"

            if "ok" in mtop_flags:
                await context.close()
                await browser.close()
                return True, ""

            try:
                n = await page.locator(
                    "a[href*='item?id='], a[href*='goofish.com/item']"
                ).count()
            except Exception:
                n = 0

            await context.close()
            await browser.close()

            if n > 0:
                return True, ""

            return (
                False,
                "未收到有效搜索接口响应且页面上无商品链接，登录态可能无效或网络异常",
            )
    except Exception as exc:
        logger.exception("登录态探测过程异常：{}", exc)
        return False, f"探测过程异常：{exc}"
