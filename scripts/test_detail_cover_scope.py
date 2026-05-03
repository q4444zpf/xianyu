#!/usr/bin/env python3
"""验证详情 JSON 中只取「当前商品」的 imageInfos，不把推荐商品图算进来。

  python scripts/test_detail_cover_scope.py              # 仅内存用例
  python scripts/test_detail_cover_scope.py --live ID   # 真机打开详情（需 data/storage_state.json）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright

from src.auth import DEFAULT_USER_AGENT
from src.crawler import (
    _collect_image_infos_triples_for_item,
    _gallery_urls_by_clicking_thumbnails,
    _mtop_payload_usable,
    _ordered_gallery_urls_from_triples,
    _pick_best_cover,
    _strip_jsonp,
)
from src.models import Item

try:
    from playwright_stealth import Stealth as _StealthCls  # type: ignore
except Exception:  # pragma: no cover
    _StealthCls = None  # type: ignore


def _naive_collect_all_imageinfos(data: object) -> list[tuple[str, int, bool]]:
    """旧逻辑：全树 imageInfos（用于与按 item 过滤对比）。"""
    out: list[tuple[str, int, bool]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).lower() == "imageinfos" and isinstance(v, list):
                    for im in v:
                        if not isinstance(im, dict):
                            continue
                        u = str(im.get("url") or "").strip()
                        if not u:
                            continue
                        try:
                            wi = int(im.get("widthSize") or im.get("width") or 0)
                        except (TypeError, ValueError):
                            wi = 0
                        try:
                            hi = int(im.get("heightSize") or im.get("height") or 0)
                        except (TypeError, ValueError):
                            hi = 0
                        major = bool(im.get("major"))
                        area = wi * hi if wi and hi else max(wi, hi, 1)
                        out.append((u, area, major))
                else:
                    walk(v)
        elif isinstance(node, list):
            for el in node:
                walk(el)

    walk(data)
    return out


def _fixture_mixed() -> dict:
    """模拟：主商品 + 推荐列表里另一条带 imageInfos。"""
    return {
        "ret": ["SUCCESS::调用成功"],
        "data": {
            "itemDO": {
                "itemId": "1111111111111",
                "title": "主商品",
                "imageInfos": [
                    {
                        "url": "https://img.example.com/main-only.jpg",
                        "major": True,
                        "widthSize": 800,
                        "heightSize": 800,
                    }
                ],
            },
            "recoList": [
                {
                    "itemId": "2222222222222",
                    "title": "推荐商品",
                    "imageInfos": [
                        {
                            "url": "https://img.example.com/reco-bad.jpg",
                            "major": True,
                            "widthSize": 1200,
                            "heightSize": 1200,
                        }
                    ],
                }
            ],
        },
    }


def _fixture_nested_gallery() -> dict:
    """主商品 id 在父级，imageInfos 在子对象里（靠 scope 传递）。"""
    return {
        "data": {
            "item": {
                "itemId": "3333333333333",
                "detail": {
                    "gallery": {
                        "imageInfos": [
                            {"url": "https://img.example.com/nested.jpg", "major": True}
                        ]
                    }
                },
            }
        }
    }


def run_unit_cases() -> None:
    main_id = "1111111111111"
    doc = _fixture_mixed()
    naive = _naive_collect_all_imageinfos(doc)
    scoped = _collect_image_infos_triples_for_item(doc, main_id)
    assert len(naive) == 2, f"naive 应含 2 组，实际 {len(naive)}"
    assert len(scoped) == 1, f"scoped 应只含主商品 1 组，实际 {len(scoped)}"
    assert "main-only" in scoped[0][0]
    assert "reco-bad" not in scoped[0][0]
    best = _pick_best_cover(scoped)
    assert "main-only" in best
    ordered = _ordered_gallery_urls_from_triples(scoped)
    assert len(ordered) == 1 and "main-only" in ordered[0]
    print("[OK] 混合主商品+推荐：naive={} scoped={} ordered={}".format(len(naive), len(scoped), len(ordered)))

    nest = _fixture_nested_gallery()
    sid = "3333333333333"
    sc2 = _collect_image_infos_triples_for_item(nest, sid)
    assert len(sc2) == 1 and "nested" in sc2[0][0]
    print("[OK] 嵌套 gallery：scoped=1 且为 nested.jpg")


async def run_live(item_id: str, storage: Path, headless: bool) -> None:
    if not storage.is_file():
        print(f"[skip] 无登录态文件: {storage}", file=sys.stderr)
        return

    item = Item(
        item_id=item_id,
        title="probe",
        detail_url=f"https://www.goofish.com/item?id={item_id}",
    )
    bodies: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = await browser.new_context(
            storage_state=str(storage),
            user_agent=DEFAULT_USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        if _StealthCls is not None:
            try:
                await _StealthCls().apply_stealth_async(ctx)
            except Exception:
                pass
        page = await ctx.new_page()

        async def on_resp(resp):
            u = resp.url.lower()
            if "mtop" not in u and "h5api" not in u:
                return
            try:
                txt = await resp.text()
            except Exception:
                return
            if item_id not in txt:
                return
            try:
                data = json.loads(_strip_jsonp(txt))
            except json.JSONDecodeError:
                return
            if isinstance(data, dict) and _mtop_payload_usable(data):
                bodies.append(data)

        page.on("response", lambda r: asyncio.create_task(on_resp(r)))
        try:
            await page.goto(item.normalized_detail_url(), wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print(f"[err] goto: {exc}", file=sys.stderr)
            await browser.close()
            return
        await asyncio.sleep(2.5)

        mode = "有头" if not headless else "无头"
        try:
            click_urls = await _gallery_urls_by_clicking_thumbnails(page)
            print(f"[{mode} 缩略图点击] 共 {len(click_urls)} 张")
            for i, u in enumerate(click_urls[:15]):
                short = u[:110] + ("..." if len(u) > 110 else "")
                print(f"  {i + 1}. {short}")
            if len(click_urls) > 15:
                print(f"  ... 另有 {len(click_urls) - 15} 张未打印")
        except Exception as exc:
            print(f"[{mode} 缩略图点击] 失败: {exc}", file=sys.stderr)

        await page.close()
        await ctx.close()
        await browser.close()

    print(f"[live] 捕获含 item_id 的 JSON 体: {len(bodies)} 个")
    for i, data in enumerate(bodies):
        naive = _naive_collect_all_imageinfos(data)
        scoped = _collect_image_infos_triples_for_item(data, item_id)
        best = _pick_best_cover(scoped)
        preview = best[:100] + ("..." if len(best) > 100 else "")
        print(
            f"  --- 体 #{i+1} naive_imageInfos条数={len(naive)} "
            f"scoped={len(scoped)} best={preview!r}"
        )
        if len(naive) > len(scoped):
            print("       (scoped 已排除部分条目，通常为推荐/其它商品图)")

    if not bodies:
        print("[warn] 未拦截到含该 item_id 的 mtop JSON，可能登录失效或接口变更。", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", metavar="ITEM_ID", help="真实打开详情页并对比 naive vs scoped")
    ap.add_argument(
        "--storage",
        type=Path,
        default=ROOT / "data" / "storage_state.json",
        help="Playwright storage_state 路径",
    )
    ap.add_argument("--headed", action="store_true", help="有头模式便于观察")
    args = ap.parse_args()

    run_unit_cases()

    if args.live:
        asyncio.run(
            run_live(
                args.live.strip(),
                args.storage.resolve(),
                headless=not args.headed,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
