"""登录态探测（与抓取共用同一次浏览器会话，见 XianyuCrawler.fetch_with_result）。"""

from __future__ import annotations

from pathlib import Path

from .crawler import XianyuCrawler


async def probe_storage_state_usable(
    storage_state_path: Path,
    headless: bool,
    keyword: str,
) -> tuple[bool, str]:
    """兼容旧接口：内部只调用一次 fetch_with_result，不再单独打开搜索页。"""
    if not storage_state_path.exists():
        return False, "未找到登录态文件 data/storage_state.json"

    crawler = XianyuCrawler(
        storage_state_path, headless, fetch_detail_cover_image=False
    )
    result = await crawler.fetch_with_result(keyword, limit=30)
    return result.session_ok, result.session_message or (
        "登录态不可用" if not result.session_ok else ""
    )
