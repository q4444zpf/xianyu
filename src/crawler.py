"""闲鱼搜索结果抓取器。

主路径：监听浏览器对 mtop 搜索接口的响应，从 JSON 中解析商品列表，
        这种方式比解析 DOM 更稳健、不易随前端改版失效。
降级路径：如果未能拦截到 mtop 响应（接口名/路径变化），则退回到 DOM 解析。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from loguru import logger
from playwright.async_api import (
    BrowserContext,
    Page,
    Response,
    TimeoutError as PWTimeoutError,
    async_playwright,
)

try:
    from playwright_stealth import Stealth as _StealthCls  # type: ignore
except Exception:  # pragma: no cover
    _StealthCls = None  # type: ignore

from .auth import DEFAULT_USER_AGENT
from .models import Item

SEARCH_URL_TEMPLATE = "https://www.goofish.com/search?q={kw}"

# PC 搜索接口在 h5api.m.goofish.com，路径含 idlemtopsearch.pc.search（旧域名仍兼容）
def _is_pc_search_mtop_url(url: str) -> bool:
    lu = url.lower()
    if "idlemtopsearch" not in lu:
        return False
    if "pc.search" not in lu:
        return False
    path = lu.split("?", 1)[0]
    return not path.endswith(".js")


def _mtop_payload_usable(data: dict) -> bool:
    """排除风控 / 挤爆 / 需登录等错误响应，避免误解析。"""
    ret = data.get("ret") or []
    if not ret:
        return bool(data.get("data"))
    head = ret[0] if isinstance(ret[0], str) else ""
    err_markers = ("ERROR::", "FAIL", "RGV587", "被挤爆", "SESSION", "非法请求")
    if any(m in head for m in err_markers):
        logger.warning("mtop 搜索响应异常，已忽略：{}", head[:200])
        return False
    return True


class XianyuCrawler:
    """对外暴露 fetch_latest 方法，单次进出上下文，避免长时间占用浏览器。"""

    def __init__(self, storage_state_path: Path, headless: bool = True) -> None:
        self.storage_state_path = storage_state_path
        self.headless = headless

    async def fetch_latest(self, keyword: str, limit: int = 30) -> list[Item]:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            context = await browser.new_context(
                storage_state=str(self.storage_state_path)
                if self.storage_state_path.exists()
                else None,
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
            )
            if _StealthCls is not None:
                try:
                    await _StealthCls().apply_stealth_async(context)
                except Exception as exc:  # pragma: no cover
                    logger.debug("stealth 应用失败: {}", exc)
            try:
                items = await self._fetch_with_context(context, keyword, limit)
            finally:
                await context.close()
                await browser.close()
            return items

    async def _fetch_with_context(
        self,
        context: BrowserContext,
        keyword: str,
        limit: int,
    ) -> list[Item]:
        page = await context.new_page()

        captured: list[dict] = []

        async def on_response(resp: Response) -> None:
            url = resp.url
            if not _is_pc_search_mtop_url(url):
                return
            try:
                body_text = await resp.text()
            except Exception:
                return
            json_str = _strip_jsonp(body_text)
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                return
            if not _mtop_payload_usable(data):
                return
            captured.append(data)

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        target_url = SEARCH_URL_TEMPLATE.format(kw=quote(keyword))
        logger.info("打开搜索页: {}", target_url)
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        except PWTimeoutError:
            logger.warning("搜索页加载超时，仍尝试继续解析当前 DOM")
        try:
            await page.wait_for_load_state("networkidle", timeout=45000)
        except PWTimeoutError:
            logger.debug("networkidle 等待超时，继续后续步骤。")

        # 检测是否被风控（滑块 / 登录跳转）
        if await _is_blocked(page):
            logger.warning("检测到风控页（滑块或要求登录），本轮跳过。")
            await page.close()
            return []

        await _try_switch_to_newest(page)

        # 等待商品列表出现 / mtop 接口返回（闲鱼链接可能为绝对路径或 ?id= ）
        try:
            await page.wait_for_selector(
                "a[href*='item?id='], a[href*='/item?id='], a[href*='goofish.com/item']",
                timeout=20000,
            )
        except PWTimeoutError:
            logger.warning("未等到商品列表 DOM。")

        # 给 mtop 响应一点时间到达
        await asyncio.sleep(2.0)

        items: list[Item] = []
        if captured:
            items = _parse_from_mtop(captured, limit)
            logger.info("通过 mtop 响应解析到 {} 条商品", len(items))

        if not items:
            items = await _parse_from_dom(page, limit)
            logger.info("通过 DOM 解析到 {} 条商品", len(items))

        await page.close()
        return items[:limit]


async def _try_switch_to_newest(page: Page) -> None:
    """按闲鱼当前 UI：先点筛选条「新发布」展开下拉，再点「最新」按发布时间排序。

    下拉内顺序一般为：最新、1天内、3天内… 用「1天内」出现作为菜单已展开的信号，
    再在菜单容器内点「最新」，避免误点页面上其它含「最新」字样的区域。
    """
    for trigger_label in ("新发布", "最新发布"):
        trigger = page.get_by_text(trigger_label, exact=True)
        if await trigger.count() == 0:
            continue
        try:
            await trigger.first.click(timeout=4000)
        except Exception as exc:
            logger.debug("点击「{}」入口失败: {}", trigger_label, exc)
            continue
        try:
            await page.wait_for_selector("text=1天内", timeout=5000)
        except PWTimeoutError:
            logger.debug("点击「{}」后未出现下拉项「1天内」。", trigger_label)
            continue
        menu = page.locator('[role="listbox"], [role="menu"]').filter(has_text="1天内")
        try:
            if await menu.count() > 0:
                opt = menu.first.get_by_text("最新", exact=True)
                if await opt.count() > 0:
                    await opt.click(timeout=3000)
                    logger.debug("已在「{}」下拉里选择「最新」排序。", trigger_label)
                    await asyncio.sleep(2.0)
                    return
            await page.get_by_text("最新", exact=True).click(timeout=3000)
            logger.debug("已选择「最新」排序（入口：{}，未命中 role 菜单容器）。", trigger_label)
            await asyncio.sleep(2.0)
            return
        except Exception as exc:
            logger.debug("选择「最新」失败: {}", exc)
            continue
    logger.debug("未能通过「新发布」→「最新」切换排序，使用默认。")


async def _is_blocked(page: Page) -> bool:
    url = page.url or ""
    if "login" in url or "sec.taobao.com" in url or "punish" in url:
        return True
    try:
        if await page.locator("text=请输入验证码").count() > 0:
            return True
        if await page.locator("text=滑动验证").count() > 0:
            return True
    except Exception:
        pass
    return False


def _strip_jsonp(body: str) -> str:
    """mtop 响应可能是 JSONP 形式，去掉外层包裹。"""
    body = body.strip()
    if body.startswith("{") or body.startswith("["):
        return body
    m = re.match(r"^[a-zA-Z0-9_\.]+\((.*)\)\s*;?\s*$", body, re.DOTALL)
    if m:
        return m.group(1)
    return body


def _parse_from_mtop(payloads: list[dict], limit: int) -> list[Item]:
    """从 mtop 接口 JSON 中提取商品列表。

    闲鱼接口返回结构常见为 data.resultList[*].data.item.main.exContent，
    这里使用一个通用递归遍历方案：扫描所有 dict，凡是带 itemId / id + title
    + price 字段的对象都尝试转成 Item。
    """
    seen_ids: set[str] = set()
    items: list[Item] = []
    for payload in payloads:
        for raw in _walk_for_items(payload):
            item = _coerce_item(raw)
            if item is None:
                continue
            if item.item_id in seen_ids:
                continue
            seen_ids.add(item.item_id)
            items.append(item)
            if len(items) >= limit:
                return items
    return items


def _walk_for_items(node):
    """生成器：递归遍历 JSON，yield 出可能是商品的 dict。"""
    if isinstance(node, dict):
        # 启发式：含有标题 + 价格 + id-like 字段
        keys_lower = {k.lower(): k for k in node.keys()}
        has_id = any(k in keys_lower for k in ("itemid", "id"))
        has_title = any(k in keys_lower for k in ("title", "name"))
        has_price = any(
            "price" in k for k in keys_lower
        )
        if has_id and has_title and has_price:
            yield node
        for v in node.values():
            yield from _walk_for_items(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_for_items(v)


def _coerce_item(raw: dict) -> Optional[Item]:
    def get_first(*keys: str) -> str:
        for key in keys:
            for k in raw.keys():
                if k.lower() == key.lower():
                    val = raw[k]
                    if isinstance(val, (str, int, float)):
                        return str(val)
                    if isinstance(val, dict):
                        # 价格对象可能形如 {priceText: "￥100"} 或 {amount: 100}
                        for vk in ("priceText", "text", "value", "amount", "title"):
                            if vk in val and isinstance(val[vk], (str, int, float)):
                                return str(val[vk])
        return ""

    item_id = get_first("itemId", "id")
    if not item_id or not item_id.isdigit():
        return None
    title = get_first("title", "name")
    if not title:
        return None
    price = get_first("price", "priceText", "soldPrice", "reservePrice")
    location = get_first("area", "location", "city", "userNick")
    publish_text = get_first("publishTime", "pubTime", "time", "modifiedTime")
    image_url = get_first("picUrl", "imageUrl", "image", "pic")
    seller = get_first("userNick", "nick", "sellerNick")

    return Item(
        item_id=item_id,
        title=title.strip(),
        price=price.strip(),
        location=location.strip(),
        publish_text=publish_text.strip(),
        image_url=image_url.strip(),
        seller=seller.strip(),
        detail_url=f"https://www.goofish.com/item?id={item_id}",
    )


async def _parse_from_dom(page: Page, limit: int) -> list[Item]:
    """DOM 兜底解析：扫描含商品 id 的链接（item?id=、/item/数字 等）。"""
    js = """
    () => {
      const out = [];
      const seen = new Set();
      const anchors = document.querySelectorAll('a[href]');
      for (const a of anchors) {
        const href = a.getAttribute('href') || '';
        let m = href.match(/[?&]id=(\\d{10,})/i);
        if (!m) m = href.match(/\\/item\\/(\\d{10,})/);
        if (!m) continue;
        const id = m[1];
        if (seen.has(id)) continue;
        seen.add(id);
        const card = a.closest('div') || a;
        const text = (card.innerText || '').trim();
        const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);
        const title = lines[0] || a.getAttribute('title') || '';
        // 找到第一个含 ¥ / ￥ 的行作为价格
        let price = '';
        for (const line of lines) {
          if (line.includes('¥') || line.includes('￥')) { price = line; break; }
        }
        const img = card.querySelector('img');
        const image_url = img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '';
        let detail = href;
        if (detail.startsWith('//')) detail = 'https:' + detail;
        else if (detail.startsWith('/')) detail = 'https://www.goofish.com' + detail;
        else if (!detail.startsWith('http')) detail = 'https://www.goofish.com/' + detail.replace(/^\\/+/, '');
        out.push({
          id, title, price, image_url,
          detail_url: detail,
          extra_lines: lines.slice(1, 6),
        });
      }
      return out;
    }
    """
    try:
        raw_list = await page.evaluate(js)
    except Exception as exc:
        logger.warning("DOM 解析失败: {}", exc)
        return []
    items: list[Item] = []
    for row in raw_list[: limit * 2]:
        item_id = str(row.get("id") or "").strip()
        if not item_id:
            continue
        items.append(
            Item(
                item_id=item_id,
                title=(row.get("title") or "").strip(),
                price=(row.get("price") or "").strip(),
                image_url=(row.get("image_url") or "").strip(),
                detail_url=(row.get("detail_url") or "").strip()
                or f"https://www.goofish.com/item?id={item_id}",
                location="",
                publish_text="",
                seller="",
            )
        )
        if len(items) >= limit:
            break
    return items
