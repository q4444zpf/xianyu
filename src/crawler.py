"""闲鱼搜索结果抓取器。

主路径：监听浏览器对 mtop 搜索接口的响应，从 JSON 中解析商品列表，
        这种方式比解析 DOM 更稳健、不易随前端改版失效。
降级路径：如果未能拦截到 mtop 响应（接口名/路径变化），则退回到 DOM 解析。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
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


def _is_item_detail_mtop_url(url: str) -> bool:
    """详情页内触发的 mtop / h5api 请求（排除 PC 搜索接口）。"""
    if _is_pc_search_mtop_url(url):
        return False
    lu = url.lower()
    if "h5api.m.goofish.com" in lu or "h5api.wapa.goofish.com" in lu:
        return True
    if "h5api.m.taobao.com" in lu:
        return True
    if "guide-acs.m.taobao.com" in lu or "acs.m.taobao.com" in lu:
        return "mtop" in lu
    # 少数同源或子域直链
    if "goofish.com" in lu and "mtop" in lu:
        return True
    return False


def _mtop_payload_usable_quiet(data: dict) -> bool:
    """与 _mtop_payload_usable 相同判断，不向日志打 warning（详情页会扫大量响应）。"""
    ret = data.get("ret") or []
    if not ret:
        return bool(data.get("data"))
    head = ret[0] if isinstance(ret[0], str) else ""
    err_markers = ("ERROR::", "FAIL", "RGV587", "被挤爆", "SESSION", "非法请求")
    if any(m in head for m in err_markers):
        return False
    return True


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


def _classify_mtop_search_body(data: dict) -> str:
    """将搜索接口 JSON 粗分为 ok / err / unknown（不打 warning）。"""
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


@dataclass
class FetchResult:
    """单次浏览器会话的抓取结果（含登录态是否可用，避免再单独开一页探测）。"""

    items: list[Item]
    session_ok: bool = True
    session_message: str = ""


# 阿里 CDN 上常见的「列表/卡片缩略」尾缀，去掉后一般为原图或更大尺寸 key
_ALI_RESIZE_TAIL = re.compile(
    r"_\d+x\d+(?:[qQ]\d+)?\.(?:jpg|jpeg|png)(?:_\.webp)?$",
    re.I,
)


def _strip_alicdn_resize_suffix(url: str) -> str:
    """去掉 URL 路径末尾的尺寸与 .jpg_.webp 等尾缀，尽量还原大图链接。"""
    if not url or not url.strip().startswith("http"):
        return url
    raw = url.strip()
    path, sep, query = raw.partition("?")
    p = path
    while True:
        m = _ALI_RESIZE_TAIL.search(p)
        if not m:
            break
        p = p[: m.start()]
    if len(p) < 30 and p != path:
        return path + (f"?{query}" if sep else "")
    return p + (f"?{query}" if sep else "")


_TPS_IN_URL = re.compile(r"-tps-(\d+)-(\d+)", re.I)
_ITEM_CODE_RE = re.compile(r"商品码[:：\s]*([A-Za-z0-9-]{4,})", re.I)


def _build_sharexy_qr_payload(item_id: str) -> str:
    """按闲鱼 sharexy 规则拼装可扫码跳转商品详情的链接。"""
    sid = (item_id or "").strip()
    if not sid:
        return ""
    bfp = quote(f'{{"id":{sid}}}', safe="")
    return (
        "https://pages.goofish.com/sharexy"
        "?loadingVisible=false"
        "&bft=item"
        "&bfs=idlepc.item"
        "&spm=a21ybx.item.0.0"
        f"&bfp={bfp}"
        "&wechat_flag=1"
    )


def _url_is_likely_idle_product_photo(url: str) -> bool:
    """排除站点 Logo、顶栏图、旺旺头像等非商品实拍 URL。"""
    if not url or not url.strip().lower().startswith("http"):
        return False
    lu = url.lower()
    if any(
        x in lu
        for x in (
            "wangwang",
            "avatar",
            "/face/",
            "qqface",
            "loading",
            "placeholder",
            "default-avatar",
            "emote",
            "emoji",
            "o1cn01puu0xc",
            "logo",
        )
    ):
        return False
    # 典型顶栏/品牌条（非用户上传商品图）
    for pat in (
        "-tps-242-150",
        "-tps-240-150",
        "-tps-200-60",
        "-tps-120-120",
        "-tps-96-96",
        "-tps-80-80",
        "-tps-72-72",
        "-tps-64-64",
    ):
        if pat in lu and "/bao/uploaded/" not in lu:
            return False
    # 闲鱼 App 方块营销图（常见 480 白底/黄底，非 bao/uploaded）
    if re.search(r"-tps-480-480\.(png|webp)(\?|$)", lu) and "/bao/uploaded/" not in lu:
        return False
    m = _TPS_IN_URL.search(lu)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if max(w, h) <= 200 and "/bao/uploaded/" not in lu and "fleamarket" not in lu and "0-fleamarket" not in lu:
            return False
    if "/bao/uploaded/" in lu:
        return True
    if "fleamarket" in lu or "0-fleamarket" in lu or "!!0-fleamarket" in lu:
        return True
    if "o1cn" in lu and ("imgextra" in lu or "img.alicdn" in lu or "gw.alicdn" in lu):
        return True
    return False


_ITEM_ID_KEY_PRIORITY = (
    "itemid",
    "item_id",
    "num_iid",
    "numiid",
    "id",
)


def _dict_direct_item_id(d: dict) -> str | None:
    """从当前 dict 上读取商品 id（与详情页 itemDO 等字段对齐）。"""
    keys_lower = {str(k).lower(): k for k in d}
    for cand in _ITEM_ID_KEY_PRIORITY:
        if cand not in keys_lower:
            continue
        raw = d.get(keys_lower[cand])
        if raw is None:
            continue
        s = str(raw).strip()
        if s.isdigit() and len(s) >= 10:
            return s
    return None


# 不向这些 key 下递归，避免 seller / 推荐 里的图误继承 item_id 作用域
_SKIP_JSON_BRANCH_KEYS = frozenset(
    {
        "seller",
        "sellerdo",
        "sellerinfo",
        "user",
        "shop",
        "shopinfo",
        "recolist",
        "recommend",
        "recommendlist",
        "guessyoulike",
        "similar",
        "feeds",
        "related",
        "hotsale",
        "vicinity",
        "tabfeed",
        "footer",
        "navigation",
        "coupon",
        "activity",
        "advert",
        "banner",
    }
)


def _should_skip_json_branch_key(key: str) -> bool:
    k = str(key).lower()
    if k in _SKIP_JSON_BRANCH_KEYS:
        return True
    if k.startswith("reco") or k.startswith("recommend") or k.startswith("similar"):
        return True
    if k.startswith("guess") or k.startswith("feed") or k.startswith("hotrank"):
        return True
    return False


_SKETCHY_IMG_URL_MARKERS = (
    "avatar",
    "face/",
    "/tps/",
    "wangwang",
    "qqface",
    "emoji",
    "badge",
    "cert/",
    "32-32",
    "_32x32",
    "_48x48",
    "_30x30",
    "logo",
    "icon",
)


def _url_looks_like_non_product_asset(url: str) -> bool:
    """卖家装修、角标、活动条等常见非轮播图 URL 特征。"""
    lu = url.lower()
    return any(m in lu for m in _SKETCHY_IMG_URL_MARKERS)


def _append_triples_from_image_infos_list(
    lst: list, out: list[tuple[str, int, bool]]
) -> None:
    for im in lst:
        if not isinstance(im, dict):
            continue
        u = str(im.get("url") or "").strip()
        if not u or _url_looks_like_non_product_asset(u):
            continue
        try:
            wi = int(im.get("widthSize") or im.get("width") or 0)
        except (TypeError, ValueError):
            wi = 0
        try:
            hi = int(im.get("heightSize") or im.get("height") or 0)
        except (TypeError, ValueError):
            hi = 0
        # 接口若给出尺寸，过小的多为角标/缩略 sprite，排除
        if wi > 0 and hi > 0 and wi < 96 and hi < 96:
            continue
        major = bool(im.get("major"))
        area = wi * hi if wi and hi else max(wi, hi, 1)
        out.append((u, area, major))


def _dict_is_item_detail_root(d: dict, item_id: str) -> bool:
    """当前节点是否为「在售商品详情」主体（有 id + 标题/价格/detail 等），而非卖家卡片等。"""
    if _dict_direct_item_id(d) != item_id:
        return False
    keys_low = {str(k).lower() for k in d}
    if any(
        x in keys_low
        for x in (
            "title",
            "soldprice",
            "description",
            "itemstatus",
            "itemstatusstr",
            "pricedto",
            "reserveprice",
        )
    ):
        return True
    # 仅有详情嵌套、无 title 字段时（部分接口形态）
    if "detail" in keys_low or "itemdo" in keys_low:
        return True
    return False


def _dict_is_small_gallery_holder(d: dict) -> bool:
    """detail 下仅承载多图的小对象（字段很少）。"""
    keys = {str(k).lower() for k in d}
    if "imageinfos" not in keys:
        return False
    noise = keys - {
        "imageinfos",
        "width",
        "height",
        "type",
        "major",
        "index",
        "id",
    }
    return len(noise) <= 2


def _may_take_imageinfos_here(
    node: dict, item_id: str, sid: str | None, in_item_detail_zone: bool
) -> bool:
    """仅在商品详情区内收集 imageInfos：itemDO 根节点或详情区内的小相册节点。"""
    if sid != item_id:
        return False
    keys = {str(k).lower() for k in node}
    if "imageinfos" not in keys:
        return False
    if _dict_is_item_detail_root(node, item_id):
        return True
    if in_item_detail_zone and _dict_is_small_gallery_holder(node):
        return True
    if in_item_detail_zone and any(
        x in keys for x in ("gallery", "pics", "pictures", "album", "imagelist")
    ):
        return True
    return False


def _collect_image_infos_triples_for_item(
    data: object, item_id: str
) -> list[tuple[str, int, bool]]:
    """只收集商品详情主数据区内的 imageInfos（排除推荐、卖家等分支）。"""
    out: list[tuple[str, int, bool]] = []

    def visit(
        node: object, scope_id: str | None, in_item_detail_zone: bool
    ) -> None:
        if isinstance(node, dict):
            own = _dict_direct_item_id(node)
            sid = own if own else scope_id
            in_zone = in_item_detail_zone or _dict_is_item_detail_root(node, item_id)
            for k, v in node.items():
                lk = str(k).lower()
                if lk == "imageinfos" and isinstance(v, list):
                    if _may_take_imageinfos_here(node, item_id, sid, in_zone):
                        _append_triples_from_image_infos_list(v, out)
                elif not _should_skip_json_branch_key(k) and isinstance(v, (dict, list)):
                    visit(v, sid, in_zone)
        elif isinstance(node, list):
            for el in node:
                visit(el, scope_id, in_item_detail_zone)

    visit(data, None, False)
    return out


def _pick_best_cover(triples: list[tuple[str, int, bool]]) -> str:
    if not triples:
        return ""
    triples.sort(key=lambda t: (0 if t[2] else 1, -t[1]))
    return _strip_alicdn_resize_suffix(triples[0][0])


def _ordered_gallery_urls_from_triples(
    triples: list[tuple[str, int, bool]],
) -> list[str]:
    """当前商品 imageInfos 全量 URL，去重且保持接口出现顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for url, _, _ in triples:
        u = _strip_alicdn_resize_suffix(url)
        if not u or not u.startswith("http"):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


_CAROUSEL_CONTAINER_SRCS_JS = r"""
() => {
  function siteUiNoise(im, src) {
    const lu = (src || '').toLowerCase();
    const alt = ((im.getAttribute('alt') || '') + (im.getAttribute('title') || '')).toLowerCase();
    if (/闲鱼|goofish|网站|logo|图标/i.test(alt)) return true;
    if (/o1cn01puu0xc/i.test(lu)) return true;
    if (/-tps-242-150|-tps-240-150|-tps-200-60|-tps-120-120|-tps-96-96|-tps-80-80/.test(lu) && lu.indexOf('bao/uploaded') < 0) return true;
    if (/-tps-480-480\.(png|webp)/.test(lu) && lu.indexOf('bao/uploaded') < 0) return true;
    return false;
  }
  function pickSrc(im) {
    return (
      im.getAttribute('src') ||
      im.getAttribute('data-src') ||
      im.getAttribute('data-zoom-src') ||
      im.getAttribute('data-lazy-src') ||
      im.getAttribute('data-original') ||
      ''
    ).trim();
  }
  function hostOk(u) {
    return /alicdn\.com|gw\.alicdn|img\.taobao/i.test(u || '');
  }
  function isCarouselRoot(el) {
    const cls = el.getAttribute('class') || '';
    return cls.split(/\s+/).some((c) => c.startsWith('carousel-container'));
  }
  const seen = new Set();
  const out = [];
  for (const root of document.querySelectorAll('[class]')) {
    if (!isCarouselRoot(root)) continue;
    for (const im of root.querySelectorAll(
      'img[src], img[data-src], img[data-zoom-src], img[data-lazy-src], img[data-original]'
    )) {
      const src = pickSrc(im);
      if (!/^https?:/i.test(src) || !hostOk(src)) continue;
      if (siteUiNoise(im, src)) continue;
      if (seen.has(src)) continue;
      seen.add(src);
      out.push(src);
      if (out.length >= 48) return out;
    }
  }
  return out;
}
"""


_LIST_ITEM_ROOT_ALL_IMGS_JS = r"""
() => {
  const bad = /猜你喜欢|为你推荐|看了又看|相似宝贝|你可能还喜欢|热销榜|精选好货|宝贝推荐/;
  function banned(el) {
    let p = el;
    for (let i = 0; i < 26 && p; i++) {
      if (bad.test((p.textContent || '').slice(0, 160))) return true;
      p = p.parentElement;
    }
    return false;
  }
  function siteUiNoise(im, src) {
    const lu = (src || '').toLowerCase();
    const alt = ((im.getAttribute('alt') || '') + (im.getAttribute('title') || '')).toLowerCase();
    if (/闲鱼|goofish|网站|logo|图标/i.test(alt)) return true;
    if (/o1cn01puu0xc/i.test(lu)) return true;
    if (/-tps-242-150|-tps-240-150|-tps-200-60|-tps-120-120|-tps-96-96|-tps-80-80/.test(lu) && lu.indexOf('bao/uploaded') < 0) return true;
    if (/-tps-480-480\.(png|webp)/.test(lu) && lu.indexOf('bao/uploaded') < 0) return true;
    return false;
  }
  function pickSrc(im) {
    return (
      im.getAttribute('src') ||
      im.getAttribute('data-src') ||
      im.getAttribute('data-zoom-src') ||
      im.getAttribute('data-lazy-src') ||
      im.getAttribute('data-original') ||
      ''
    ).trim();
  }
  function hostOk(u) {
    return /alicdn\.com|gw\.alicdn|img\.taobao/i.test(u || '');
  }
  function isListItemRoot(el) {
    const cls = el.getAttribute('class') || '';
    return cls.split(/\s+/).some((c) => c.startsWith('item-main-window-list-item'));
  }
  const roots = [];
  for (const el of document.querySelectorAll('[class]')) {
    if (!isListItemRoot(el)) continue;
    if (banned(el)) continue;
    roots.push(el);
  }
  roots.sort((a, b) => {
    const ra = a.getBoundingClientRect();
    const rb = b.getBoundingClientRect();
    const t = ra.top - rb.top;
    if (Math.abs(t) > 2) return t;
    return ra.left - rb.left;
  });
  const seen = new Set();
  const out = [];
  for (const root of roots) {
    for (const im of root.querySelectorAll('img')) {
      const src = pickSrc(im);
      if (!/^https?:/i.test(src) || !hostOk(src)) continue;
      if (siteUiNoise(im, src)) continue;
      if (seen.has(src)) continue;
      seen.add(src);
      out.push(src);
      if (out.length >= 48) return out;
    }
  }
  return out;
}
"""


_LIST_ITEM_THUMB_CLICK_POINTS_JS = r"""
() => {
  const bad = /猜你喜欢|为你推荐|看了又看|相似宝贝|你可能还喜欢|热销榜|精选好货|宝贝推荐/;
  function banned(el) {
    let p = el;
    for (let i = 0; i < 26 && p; i++) {
      if (bad.test((p.textContent || '').slice(0, 160))) return true;
      p = p.parentElement;
    }
    return false;
  }
  function hostOk(u) {
    return /alicdn\.com|gw\.alicdn|img\.taobao/i.test(u || '');
  }
  function pickSrc(im) {
    return (
      im.getAttribute('src') ||
      im.getAttribute('data-src') ||
      im.getAttribute('data-zoom-src') ||
      im.getAttribute('data-lazy-src') ||
      im.getAttribute('data-original') ||
      ''
    ).trim();
  }
  function siteUiNoise(im, src) {
    const lu = (src || '').toLowerCase();
    const alt = ((im.getAttribute('alt') || '') + (im.getAttribute('title') || '')).toLowerCase();
    if (/闲鱼|goofish|网站|logo|图标/i.test(alt)) return true;
    if (/o1cn01puu0xc/i.test(lu)) return true;
    const r = im.getBoundingClientRect();
    if (r.top < 80) return true;
    if (/-tps-242-150|-tps-240-150|-tps-200-60|-tps-120-120|-tps-96-96|-tps-80-80/.test(lu) && lu.indexOf('bao/uploaded') < 0) return true;
    if (/-tps-480-480\.(png|webp)/.test(lu) && lu.indexOf('bao/uploaded') < 0) return true;
    return false;
  }
  function isListItemRow(el) {
    const cls = el.getAttribute('class') || '';
    if (cls.indexOf('item-main-window-list-item') >= 0) return true;
    return cls.split(/\s+/).some((c) => c.startsWith('item-main-window-list-item'));
  }
  const rows = [];
  for (const el of document.querySelectorAll('[class]')) {
    if (!isListItemRow(el)) continue;
    if (banned(el)) continue;
    const im = el.querySelector(
      'img[src], img[data-src], img[data-zoom-src], img[data-lazy-src], img[data-original], img'
    );
    if (im) {
      const r = im.getBoundingClientRect();
      if (r.width < 12 || r.height < 12) continue;
      const src = pickSrc(im);
      const hasHttp = /^https?:/i.test(src) && hostOk(src);
      const looksThumb =
        r.width <= 240 &&
        r.height <= 240 &&
        r.left < window.innerWidth * 0.48 &&
        r.top < window.innerHeight * 0.72;
      if (hasHttp && !siteUiNoise(im, src)) {
        rows.push({
          x: r.left + r.width / 2,
          y: r.top + r.height / 2,
          ord: r.top * 10000 + r.left,
        });
      } else if (looksThumb) {
        const alt = ((im.getAttribute('alt') || '') + (im.getAttribute('title') || '')).toLowerCase();
        if (/闲鱼|goofish|网站|logo|图标/i.test(alt)) continue;
        rows.push({
          x: r.left + r.width / 2,
          y: r.top + r.height / 2,
          ord: r.top * 10000 + r.left,
        });
      }
    } else {
      const r = el.getBoundingClientRect();
      if (r.width < 20 || r.height < 20) continue;
      rows.push({
        x: r.left + r.width / 2,
        y: r.top + r.height / 2,
        ord: r.top * 10000 + r.left,
      });
    }
  }
  rows.sort((a, b) => a.ord - b.ord);
  const out = [];
  const seen = new Set();
  for (const { x, y } of rows) {
    const key = Math.round(x / 8) + ',' + Math.round(y / 8);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ x, y });
    if (out.length >= 24) break;
  }
  return out;
}
"""


_THUMB_CLICK_TARGETS_JS = r"""
() => {
  const bad = /猜你喜欢|为你推荐|看了又看|相似宝贝|你可能还喜欢|热销榜|精选好货|宝贝推荐/;
  function banned(el) {
    let p = el;
    for (let i = 0; i < 26 && p; i++) {
      if (bad.test((p.textContent || '').slice(0, 160))) return true;
      p = p.parentElement;
    }
    return false;
  }
  function hostOk(u) {
    return /alicdn\.com|gw\.alicdn|img\.taobao/i.test(u || '');
  }
  function siteUiNoise(im, src) {
    const lu = (src || '').toLowerCase();
    const alt = ((im.getAttribute('alt') || '') + (im.getAttribute('title') || '')).toLowerCase();
    if (/闲鱼|goofish|网站|logo|图标/i.test(alt)) return true;
    if (/o1cn01puu0xc/i.test(lu)) return true;
    const r = im.getBoundingClientRect();
    if (r.top < 80) return true;
    if (/-tps-242-150|-tps-240-150|-tps-200-60|-tps-120-120|-tps-96-96|-tps-80-80/.test(lu) && lu.indexOf('bao/uploaded') < 0) return true;
    if (/-tps-480-480\.(png|webp)/.test(lu) && lu.indexOf('bao/uploaded') < 0) return true;
    return false;
  }
  const pts = [];
  for (const im of document.querySelectorAll('img[src], img[data-src]')) {
    if (banned(im)) continue;
    const src = (im.getAttribute('src') || im.getAttribute('data-src') || '').trim();
    if (!/^https?:/i.test(src) || !hostOk(src)) continue;
    if (siteUiNoise(im, src)) continue;
    const r = im.getBoundingClientRect();
    if (r.width < 32 || r.height < 32) continue;
    if (r.width > 200 || r.height > 200) continue;
    if (r.left > window.innerWidth * 0.38) continue;
    if (r.top > window.innerHeight * 0.62) continue;
    pts.push({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
  }
  pts.sort((a, b) => (Math.abs(a.y - b.y) < 30 ? a.x - b.x : a.y - b.y));
  const out = [];
  const seen = new Set();
  for (const p of pts) {
    const key = Math.round(p.x / 10) + ',' + Math.round(p.y / 10);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ x: p.x, y: p.y });
    if (out.length >= 24) break;
  }
  return out;
}
"""

_THUMB_COLUMN_SRCS_JS = r"""
() => {
  const bad = /猜你喜欢|为你推荐|看了又看|相似宝贝|你可能还喜欢|热销榜|精选好货|宝贝推荐/;
  function banned(el) {
    let p = el;
    for (let i = 0; i < 26 && p; i++) {
      if (bad.test((p.textContent || '').slice(0, 160))) return true;
      p = p.parentElement;
    }
    return false;
  }
  function pickSrc(im) {
    return (
      im.getAttribute('src') ||
      im.getAttribute('data-src') ||
      im.getAttribute('data-zoom-src') ||
      im.getAttribute('data-lazy-src') ||
      im.getAttribute('data-original') ||
      ''
    ).trim();
  }
  function siteUiNoise(im, src) {
    const lu = (src || '').toLowerCase();
    const alt = ((im.getAttribute('alt') || '') + (im.getAttribute('title') || '')).toLowerCase();
    if (/闲鱼|goofish|网站|logo|图标/i.test(alt)) return true;
    if (/o1cn01puu0xc/i.test(lu)) return true;
    const r = im.getBoundingClientRect();
    if (r.top < 80) return true;
    if (/-tps-242-150|-tps-240-150|-tps-200-60|-tps-120-120|-tps-96-96|-tps-80-80/.test(lu) && lu.indexOf('bao/uploaded') < 0) return true;
    if (/-tps-480-480\.(png|webp)/.test(lu) && lu.indexOf('bao/uploaded') < 0) return true;
    return false;
  }
  const rows = [];
  for (const im of document.querySelectorAll(
    'img[src], img[data-src], img[data-zoom-src], img[data-lazy-src]'
  )) {
    if (banned(im)) continue;
    const src = pickSrc(im);
    if (!/^https?:/i.test(src)) continue;
    if (!/alicdn\.com|gw\.alicdn|img\.taobao/i.test(src)) continue;
    if (siteUiNoise(im, src)) continue;
    const r = im.getBoundingClientRect();
    if (r.width < 26 || r.height < 26) continue;
    if (r.width > 240 || r.height > 240) continue;
    if (r.left > window.innerWidth * 0.42) continue;
    if (r.top > window.innerHeight * 0.66) continue;
    rows.push({ src, top: r.top + r.height / 2, left: r.left + r.width / 2 });
  }
  rows.sort((a, b) => (Math.abs(a.top - b.top) < 32 ? a.left - b.left : a.top - b.top));
  const seen = new Set();
  const out = [];
  for (const { src } of rows) {
    if (seen.has(src)) continue;
    seen.add(src);
    out.push(src);
    if (out.length >= 24) break;
  }
  return out;
}
"""

_MAIN_HERO_LARGEST_SRC_JS = r"""
() => {
  const bad = /猜你喜欢|为你推荐|看了又看|相似宝贝|你可能还喜欢/;
  function bannedBlock(im) {
    let p = im;
    for (let i = 0; i < 26 && p; i++) {
      if (bad.test((p.textContent || '').slice(0, 160))) return true;
      p = p.parentElement;
    }
    return false;
  }
  function siteUiNoise(im, src) {
    const lu = (src || '').toLowerCase();
    const alt = ((im.getAttribute('alt') || '') + (im.getAttribute('title') || '')).toLowerCase();
    if (/闲鱼|goofish|网站|logo|图标/i.test(alt)) return true;
    if (/o1cn01puu0xc/i.test(lu)) return true;
    const r = im.getBoundingClientRect();
    if (r.top < 80) return true;
    if (/-tps-242-150|-tps-240-150|-tps-200-60|-tps-120-120|-tps-96-96|-tps-80-80/.test(lu) && lu.indexOf('bao/uploaded') < 0) return true;
    if (/-tps-480-480\.(png|webp)/.test(lu) && lu.indexOf('bao/uploaded') < 0) return true;
    return false;
  }
  let best = '', bestArea = 0;
  for (const im of document.querySelectorAll(
    'img[src], img[data-src], img[data-zoom-src], img[data-lazy-src]'
  )) {
    if (bannedBlock(im)) continue;
    const src = (
      im.getAttribute('src') ||
      im.getAttribute('data-src') ||
      im.getAttribute('data-zoom-src') ||
      im.getAttribute('data-lazy-src') ||
      ''
    ).trim();
    if (!/^https?:/i.test(src)) continue;
    if (!/alicdn\.com|gw\.alicdn|img\.taobao/i.test(src)) continue;
    if (siteUiNoise(im, src)) continue;
    const r = im.getBoundingClientRect();
    if (r.left < window.innerWidth * 0.18) continue;
    if (r.top > window.innerHeight * 0.68) continue;
    if (r.width < 110 || r.height < 110) continue;
    const a = r.width * r.height;
    if (a > bestArea) {
      bestArea = a;
      best = src;
    }
  }
  return best;
}
"""


async def _click_main_window_thumb_images_playwright(
    page: Page,
    *,
    max_thumbs: int,
    after_each_click: Callable[[], Awaitable[None]] | None = None,
) -> int:
    """用 Playwright 依次点击「含 item-main-window-list-item 的节点下的 img」以触发轮播加载。
    比纯坐标点击更可靠（懒加载缩略图常无 src）。
    after_each_click：每次成功点击后调用（用于在虚拟轮播 DOM 下立刻合并当前主图 URL）。"""
    loc = page.locator("[class*='item-main-window-list-item'] img")
    try:
        n = await loc.count()
    except Exception:
        return 0
    if n == 0:
        return 0
    clicks = 0
    for i in range(min(n, max_thumbs)):
        im = loc.nth(i)
        try:
            if not await im.is_visible():
                continue
            box = await im.bounding_box()
            if box and (box["width"] > 280 or box["height"] > 280):
                continue
        except Exception:
            pass
        try:
            await im.scroll_into_view_if_needed(timeout=8000)
            await im.click(timeout=12000)
            clicks += 1
            if after_each_click is not None:
                await asyncio.sleep(0.35)
                await after_each_click()
            await asyncio.sleep(0.6)
        except Exception as exc:
            logger.debug("Playwright 详情缩略图 img[{}] 点击失败: {}", i, exc)
            continue
    return clicks


async def _gallery_urls_by_clicking_thumbnails(
    page: Page, *, max_thumbs: int = 24
) -> list[str]:
    """先依次点击缩略图加载大图，再依次收集 carousel-container 内图片，并遍历每个
    class 以 item-main-window-list-item 开头的根节点下全部 img 合并去重；若无结果则回退旧逻辑。
    """
    try:
        await page.evaluate("() => { window.scrollTo(0, 0); }")
        await asyncio.sleep(0.35)
    except Exception:
        pass
    ordered: list[str] = []
    seen: set[str] = set()

    def _push(u: str) -> None:
        s = _strip_alicdn_resize_suffix(u.strip())
        if not s.startswith("http") or s in seen:
            return
        if not _url_is_likely_idle_product_photo(s):
            return
        seen.add(s)
        ordered.append(s)

    async def _merge_carousel_and_list_item_images() -> None:
        """从当前 DOM 合并 carousel-container 与 item-main-window-list-item* 子树中的图
        到 ordered（不清空，便于虚拟轮播下多次采样）。"""
        try:
            carousel_srcs = await page.evaluate(_CAROUSEL_CONTAINER_SRCS_JS)
        except Exception as exc:
            logger.debug("读取 carousel-container 内图片失败: {}", exc)
            carousel_srcs = []
        if isinstance(carousel_srcs, list):
            for u in carousel_srcs:
                if isinstance(u, str) and u:
                    _push(u)
        try:
            list_item_srcs = await page.evaluate(_LIST_ITEM_ROOT_ALL_IMGS_JS)
        except Exception as exc:
            logger.debug("读取 item-main-window-list-item 下全部图片失败: {}", exc)
            list_item_srcs = []
        if isinstance(list_item_srcs, list):
            for u in list_item_srcs:
                if isinstance(u, str) and u:
                    _push(u)

    await _merge_carousel_and_list_item_images()

    thumbs_clicked = False
    try:
        pw_n = await _click_main_window_thumb_images_playwright(
            page,
            max_thumbs=max_thumbs,
            after_each_click=_merge_carousel_and_list_item_images,
        )
        if pw_n > 0:
            thumbs_clicked = True
    except Exception as exc:
        logger.debug("Playwright 缩略图列点击异常: {}", exc)

    targets: list = []
    if not thumbs_clicked:
        try:
            targets = await page.evaluate(_LIST_ITEM_THUMB_CLICK_POINTS_JS)
        except Exception as exc:
            logger.debug("解析 item-main-window-list-item 缩略图坐标失败: {}", exc)
        if not isinstance(targets, list) or not targets:
            try:
                targets = await page.evaluate(_THUMB_CLICK_TARGETS_JS)
            except Exception as exc:
                logger.debug("回退：解析左侧缩略图坐标失败: {}", exc)
                targets = []

        if isinstance(targets, list):
            for i, t in enumerate(targets[:max_thumbs]):
                if not isinstance(t, dict):
                    continue
                try:
                    x = float(t.get("x", 0))
                    y = float(t.get("y", 0))
                except (TypeError, ValueError):
                    continue
                if x < 8 or y < 8:
                    continue
                try:
                    await page.mouse.click(x, y)
                except Exception as exc:
                    logger.debug("点击缩略图 {} 失败: {}", i, exc)
                    continue
                thumbs_clicked = True
                await asyncio.sleep(0.45)
                await _merge_carousel_and_list_item_images()
                await asyncio.sleep(0.4)

    if thumbs_clicked:
        await asyncio.sleep(0.45)
    await _merge_carousel_and_list_item_images()
    if ordered:
        return ordered

    # —— 回退：轮播区仍无可用图时，合并旧版 URL +（若尚未点击过）再逐次点缩略图读主图 ——
    try:
        carousel_srcs = await page.evaluate(_CAROUSEL_CONTAINER_SRCS_JS)
    except Exception as exc:
        logger.debug("回退读取 carousel-container 失败: {}", exc)
        carousel_srcs = []
    if isinstance(carousel_srcs, list):
        for u in carousel_srcs:
            if isinstance(u, str) and u:
                _push(u)

    try:
        list_item_srcs_fb = await page.evaluate(_LIST_ITEM_ROOT_ALL_IMGS_JS)
    except Exception as exc:
        logger.debug("回退：读取 item-main-window-list-item 下全部图片失败: {}", exc)
        list_item_srcs_fb = []
    if isinstance(list_item_srcs_fb, list):
        for u in list_item_srcs_fb:
            if isinstance(u, str) and u:
                _push(u)

    try:
        thumb_srcs = await page.evaluate(_THUMB_COLUMN_SRCS_JS)
    except Exception as exc:
        logger.debug("读取左侧缩略图 URL 列表失败: {}", exc)
        thumb_srcs = []
    if isinstance(thumb_srcs, list):
        for u in thumb_srcs:
            if isinstance(u, str) and u:
                _push(u)

    try:
        first = await page.evaluate(_MAIN_HERO_LARGEST_SRC_JS)
    except Exception as exc:
        logger.debug("读取主图区首帧失败: {}", exc)
        first = ""
    if isinstance(first, str) and first:
        _push(first)

    if thumbs_clicked or not isinstance(targets, list):
        return ordered

    for i, t in enumerate(targets[:max_thumbs]):
        if not isinstance(t, dict):
            continue
        try:
            x = float(t.get("x", 0))
            y = float(t.get("y", 0))
        except (TypeError, ValueError):
            continue
        if x < 8 or y < 8:
            continue
        try:
            await page.mouse.click(x, y)
        except Exception as exc:
            logger.debug("回退点击缩略图 {} 失败: {}", i, exc)
            continue
        await asyncio.sleep(0.45)
        await _merge_carousel_and_list_item_images()
        await asyncio.sleep(0.4)
        try:
            src = await page.evaluate(_MAIN_HERO_LARGEST_SRC_JS)
        except Exception:
            src = ""
        if isinstance(src, str) and src:
            _push(src)

    return ordered


def _gallery_urls_from_detail_mtop_payloads(
    payloads: list[dict], item_id: str
) -> list[str]:
    """从详情页拦截到的 mtop JSON 中抽取当前商品的 imageInfos（与搜索解析共用遍历逻辑）。"""
    all_triples: list[tuple[str, int, bool]] = []
    for p in payloads:
        all_triples.extend(_collect_image_infos_triples_for_item(p, item_id))
    urls = _ordered_gallery_urls_from_triples(all_triples)
    return [u for u in urls if _url_is_likely_idle_product_photo(u)]


def _extract_item_code_from_payload(payload: object) -> str:
    """从详情 JSON 里提取商品码文本，未找到返回空字符串。"""
    found = ""

    def visit(node: object, depth: int = 0) -> None:
        nonlocal found
        if found or depth > 14:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                lk = str(k).lower()
                if lk in ("itemcode", "item_code", "commoditycode", "commodity_code"):
                    s = str(v).strip() if v is not None else ""
                    if s:
                        found = s
                        return
                if isinstance(v, str):
                    m = _ITEM_CODE_RE.search(v)
                    if m:
                        found = m.group(1).strip()
                        return
                if isinstance(v, (dict, list)):
                    visit(v, depth + 1)
                    if found:
                        return
        elif isinstance(node, list):
            for el in node:
                visit(el, depth + 1)
                if found:
                    return

    visit(payload)
    return found


def _extract_share_payload_from_detail_payload(payload: dict) -> str:
    """从详情接口里提取分享 deeplink（优先官方字段）。"""
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    item_do = data.get("itemDO")
    if not isinstance(item_do, dict):
        return ""
    share_data = item_do.get("shareData")
    if not isinstance(share_data, dict):
        return ""
    raw = share_data.get("shareInfoJsonString")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(obj, dict):
        return ""
    url = obj.get("url")
    if isinstance(url, str):
        return url.strip()
    return ""


async def _extract_item_code_from_page_dom(page: Page) -> str:
    """从详情页可见文本提取“商品码: XXXXX”，用于 mtop 未命中时兜底。"""
    try:
        text = await page.evaluate("() => (document.body && document.body.innerText) || ''")
    except Exception:
        return ""
    if not isinstance(text, str) or not text:
        return ""
    m = _ITEM_CODE_RE.search(text)
    return m.group(1).strip() if m else ""


async def _extract_official_app_qr_data_url(page: Page) -> str:
    """点击详情页右侧 APP/商品码入口，提取官方扫码弹层里的二维码 data URL。"""
    # 入口文案在不同页面/AB 实验里可能是 APP 或 商品码。
    entry = page.get_by_text("APP", exact=True)
    if await entry.count() == 0:
        entry = page.get_by_text("商品码", exact=True)
    if await entry.count() == 0:
        return ""
    try:
        await entry.first.hover(timeout=3000)
    except Exception:
        pass
    try:
        await entry.first.click(timeout=3000)
    except Exception:
        # 有些页面仅 hover 出弹层，不要求 click 成功。
        pass
    await asyncio.sleep(0.9)
    try:
        data_url = await page.evaluate(
            """
            () => {
              const canvases = Array.from(document.querySelectorAll('canvas'));
              const visible = canvases.filter(cv => {
                const r = cv.getBoundingClientRect();
                if (r.width < 120 || r.height < 120) return false;
                const cs = getComputedStyle(cv);
                if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
                return r.left >= 0 && r.top >= 0 && r.right <= window.innerWidth + 20 && r.bottom <= window.innerHeight + 20;
              });
              if (!visible.length) return '';
              visible.sort((a, b) => {
                const ra = a.getBoundingClientRect();
                const rb = b.getBoundingClientRect();
                const sa = ra.width * ra.height;
                const sb = rb.width * rb.height;
                return sb - sa;
              });
              const target = visible[0];
              try {
                const data = target.toDataURL('image/png');
                return typeof data === 'string' ? data : '';
              } catch (e) {
                return '';
              }
            }
            """
        )
    except Exception:
        return ""
    return data_url if isinstance(data_url, str) else ""


async def _fetch_item_detail_gallery(page: Page, item: Item) -> list[str]:
    """主路径：监听详情页 mtop/h5api 响应，从 JSON 的 imageInfos 取主图（与搜索同源逻辑）。
    若接口未命中再回退 DOM 点击轮播缩略图。"""
    detail = item.normalized_detail_url()
    if not detail:
        logger.info("商品 {} 无详情链接，未拉图", item.item_id)
        return []

    captured: list[dict] = []

    async def on_response(resp: Response) -> None:
        if not _is_item_detail_mtop_url(resp.url):
            return
        try:
            st = resp.status
            if st is not None and int(st) >= 400:
                return
        except (TypeError, ValueError):
            pass
        ct = (resp.headers.get("content-type") or "").lower()
        if not any(x in ct for x in ("json", "javascript", "text/plain")):
            return
        try:
            body = await resp.text()
        except Exception:
            return
        if len(body) > 4_000_000:
            return
        js = _strip_jsonp(body)
        try:
            data = json.loads(js)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        if not _mtop_payload_usable_quiet(data):
            return
        captured.append(data)

    page.on("response", on_response)
    try:
        try:
            await page.goto(detail, wait_until="domcontentloaded", timeout=35000)
        except PWTimeoutError:
            logger.debug("详情页加载超时: {}", item.item_id)
        try:
            await page.wait_for_load_state("networkidle", timeout=22000)
        except PWTimeoutError:
            logger.debug("详情页 networkidle 超时: {}", item.item_id)
        await asyncio.sleep(1.0)
    finally:
        try:
            page.off("response", on_response)
        except Exception:
            pass

    api_urls = _gallery_urls_from_detail_mtop_payloads(captured, item.item_id)
    code_from_payload = ""
    for payload in captured:
        code_from_payload = _extract_item_code_from_payload(payload)
        if code_from_payload:
            break
    if code_from_payload:
        item.item_code = code_from_payload
    else:
        item.item_code = await _extract_item_code_from_page_dom(page)
    # 优先用 sharexy（实测可被闲鱼 App 扫码直达）；详情接口 deeplink 仅作兜底。
    item.app_qr_payload = _build_sharexy_qr_payload(item.item_id)
    if not item.app_qr_payload:
        for payload in captured:
            from_detail = _extract_share_payload_from_detail_payload(payload)
            if from_detail:
                item.app_qr_payload = from_detail
                break
    item.app_qr_data_url = await _extract_official_app_qr_data_url(page)

    if api_urls:
        logger.info(
            "商品 {} 从详情 mtop 解析主图 {} 张: {}",
            item.item_id,
            len(api_urls),
            api_urls,
        )
        return api_urls

    logger.debug(
        "商品 {} 详情 mtop 未解析到主图（已捕获 {} 段 JSON），回退 DOM 点击",
        item.item_id,
        len(captured),
    )
    click_urls = await _gallery_urls_by_clicking_thumbnails(page)
    if click_urls:
        logger.info(
            "商品 {} 从 DOM 获取详情图 {} 张: {}",
            item.item_id,
            len(click_urls),
            click_urls,
        )
    else:
        logger.info(
            "商品 {} 详情图 0 张（mtop 与 DOM 均无）",
            item.item_id,
        )
    return click_urls


async def _enrich_items_cover_from_detail_pages(
    context: BrowserContext, items: list[Item]
) -> None:
    """逐条打开详情页：优先从 mtop 接口 JSON 取主图，失败再 DOM 点击轮播。"""
    logger.info("发信前：详情页拉主图（{} 条商品，mtop 优先）…", len(items))
    for item in items:
        detail_page = await context.new_page()
        try:
            gallery = await _fetch_item_detail_gallery(detail_page, item)
            if not item.item_code:
                item.item_code = item.item_id
            if gallery:
                item.gallery_urls = gallery
                item.image_url = gallery[0]
        finally:
            await detail_page.close()


class XianyuCrawler:
    """对外暴露 fetch_latest 方法，单次进出上下文，避免长时间占用浏览器。"""

    def __init__(
        self,
        storage_state_path: Path,
        headless: bool = True,
        fetch_detail_cover_image: bool = True,
    ) -> None:
        self.storage_state_path = storage_state_path
        self.headless = headless
        self.fetch_detail_cover_image = fetch_detail_cover_image

    async def fetch_with_result(self, keyword: str, limit: int = 30) -> FetchResult:
        """打开**一次**搜索页 → 点「新发布」→「最新」→ 解析；同时给出登录态结论（不再单独探测）。"""
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
                return await self._fetch_with_context(context, keyword, limit)
            finally:
                await context.close()
                await browser.close()

    async def fetch_latest(self, keyword: str, limit: int = 30) -> list[Item]:
        r = await self.fetch_with_result(keyword, limit)
        return r.items

    async def enrich_detail_covers(self, items: list[Item]) -> None:
        """仅在发信前调用：为给定商品打开详情页，用大图 URL 覆盖 image_url。"""
        if not items or not self.fetch_detail_cover_image:
            return
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
                await _enrich_items_cover_from_detail_pages(context, items)
            finally:
                await context.close()
                await browser.close()

    async def _fetch_with_context(
        self,
        context: BrowserContext,
        keyword: str,
        limit: int,
    ) -> FetchResult:
        page = await context.new_page()

        captured: list[dict] = []
        mtop_flags: list[str] = []

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
            mtop_flags.append(_classify_mtop_search_body(data))
            if not _mtop_payload_usable(data):
                return
            captured.append(data)

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        target_url = SEARCH_URL_TEMPLATE.format(kw=quote(keyword))
        logger.info("打开搜索页（本回合仅此一次）: {}", target_url)
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
            return FetchResult(
                [],
                False,
                "命中风控或验证页，无法完成搜索",
            )

        purl = (page.url or "").lower()
        if "login" in purl or "passport.goofish.com" in purl:
            await page.close()
            return FetchResult(
                [],
                False,
                "页面已跳转登录相关地址，登录态可能已失效",
            )

        await _try_switch_to_newest(page)

        purl2 = (page.url or "").lower()
        if "login" in purl2 or "passport.goofish.com" in purl2:
            await page.close()
            return FetchResult(
                [],
                False,
                "排序后跳转登录页，登录态可能已失效",
            )
        if await _is_blocked(page):
            await page.close()
            return FetchResult(
                [],
                False,
                "排序后出现风控或验证页",
            )

        # 等待商品列表出现 / mtop 接口返回（闲鱼链接可能为绝对路径或 ?id= ）
        try:
            await page.wait_for_selector(
                "a[href*='item?id='], a[href*='/item?id='], a[href*='goofish.com/item']",
                timeout=20000,
            )
        except PWTimeoutError:
            logger.warning("未等到商品列表 DOM。")

        # 给 mtop 响应一点时间到达（含「最新」排序后的二次请求）
        await asyncio.sleep(2.0)

        items: list[Item] = []
        if captured:
            items = _parse_from_mtop(captured, limit)
            logger.info("通过 mtop 响应解析到 {} 条商品", len(items))

        if not items:
            items = await _parse_from_dom(page, limit)
            logger.info("通过 DOM 解析到 {} 条商品", len(items))

        session_ok = True
        session_msg = ""
        try:
            n_dom = await page.locator(
                "a[href*='item?id='], a[href*='goofish.com/item']"
            ).count()
        except Exception:
            n_dom = 0

        if not items:
            if "err" in mtop_flags:
                session_ok = False
                session_msg = (
                    "搜索接口返回错误（挤爆/会话失效等），请重新执行 python main.py login"
                )
            elif "ok" not in mtop_flags and n_dom == 0:
                session_ok = False
                session_msg = (
                    "未收到有效搜索接口响应且页面上无商品链接，登录态可能无效或网络异常"
                )

        items = items[:limit]

        await page.close()
        return FetchResult(items, session_ok, session_msg)


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

    闲鱼 PC 搜索里价格常在嵌套对象（如 priceInfo、exContent）中，同一 itemId
    也可能对应多层浅/深节点；因此遍历只要求 id+title，价格由 _coerce_item 深搜，
    并对重复 itemId 合并优先保留有价格的条目。
    """
    merged: dict[str, Item] = {}
    order: list[str] = []
    for payload in payloads:
        for raw in _walk_for_items(payload):
            item = _coerce_item(raw)
            if item is None:
                continue
            prev = merged.get(item.item_id)
            if prev is None:
                merged[item.item_id] = item
                order.append(item.item_id)
            else:
                merged[item.item_id] = _prefer_richer_item(prev, item)
    items = [merged[i] for i in order]
    return items[:limit]


def _prefer_richer_item(a: Item, b: Item) -> Item:
    """同一商品多条解析结果时，优先保留有价格、信息更全的一条。"""
    if b.price and not a.price:
        return b
    if a.price and not b.price:
        return a
    if len(b.title) > len(a.title) and (b.price or not a.price):
        return b
    return a


def _walk_for_items(node):
    """生成器：递归遍历 JSON，yield 出可能是商品的 dict。"""
    if isinstance(node, dict):
        keys_lower = {k.lower(): k for k in node.keys()}
        if _looks_like_product_dict(node, keys_lower):
            yield node
        for v in node.values():
            yield from _walk_for_items(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_for_items(v)


def _looks_like_product_dict(raw: dict, keys_lower: dict[str, str]) -> bool:
    """是否为「同一层」带商品 id + 标题的节点（价格可在子对象里）。"""
    has_title = False
    for tk in ("title", "name"):
        if tk not in keys_lower:
            continue
        v = raw.get(keys_lower[tk])
        if isinstance(v, str) and v.strip():
            has_title = True
            break
    if not has_title:
        return False
    if "itemid" in keys_lower:
        vid = raw.get(keys_lower["itemid"])
        return str(vid).strip().isdigit() if vid is not None else False
    if "id" in keys_lower:
        vid = raw.get(keys_lower["id"])
        s = str(vid).strip() if vid is not None else ""
        return s.isdigit() and len(s) >= 10
    return False


def _normalize_price_display(val: str) -> str:
    """把纯数字尽量格式化为带 ￥ 的展示（闲鱼常见为分）。"""
    s = str(val).strip()
    if not s:
        return ""
    if "￥" in s or "¥" in s or "元" in s or s.startswith("."):
        return s
    if not s.replace(".", "", 1).isdigit():
        return s
    if "." in s:
        return f"￥{s}" if not s.startswith("￥") else s
    n = int(s)
    # 大整数多为分（如 9900 -> 99）
    if n >= 100 and n % 100 == 0 and n < 100_000_000:
        yuan = n / 100.0
        out = f"￥{yuan:.2f}".rstrip("0").rstrip(".")
        return out or f"￥{yuan}"
    if n < 100_000:
        return f"￥{n}"
    return s


def _deep_find_price(obj: object, depth: int = 0, max_depth: int = 14) -> str:
    """在嵌套 dict/list 中查找价格文案（PC 搜索 mtop 里价格常在子对象）。"""
    if depth > max_depth or obj is None:
        return ""
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = k.lower()
            if "pricerange" in lk or lk in ("pricefilter", "pricetype"):
                continue
            if lk in (
                "pricetext",
                "soldpricetext",
                "currentpricetext",
                "fishpricetext",
                "simpleprice",
            ):
                if isinstance(v, (str, int, float)):
                    t = str(v).strip()
                    if t:
                        return t if ("￥" in t or "¥" in t) else _normalize_price_display(t)
            if lk in ("priceinfo", "pricedto", "itemprice", "cardprice"):
                if isinstance(v, dict):
                    got = _deep_find_price(v, depth + 1, max_depth)
                    if got:
                        return got
            if any(
                x in lk
                for x in (
                    "soldprice",
                    "fishprice",
                    "reserveprice",
                    "currentprice",
                    "originprice",
                )
            ) or (lk == "price" and not isinstance(v, (dict, list))):
                if isinstance(v, (str, int, float)):
                    t = str(v).strip()
                    if t and t != "0":
                        return t if ("￥" in t or "¥" in t or "元" in t) else _normalize_price_display(t)
                if isinstance(v, dict):
                    got = _deep_find_price(v, depth + 1, max_depth)
                    if got:
                        return got
            if isinstance(v, (dict, list)):
                got = _deep_find_price(v, depth + 1, max_depth)
                if got:
                    return got
    elif isinstance(obj, list):
        for el in obj:
            got = _deep_find_price(el, depth + 1, max_depth)
            if got:
                return got
    return ""


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
    price = get_first("price", "priceText", "soldPrice", "reservePrice", "fishPrice", "currentPrice")
    if not price.strip() or price.strip() == "0":
        price = _deep_find_price(raw)
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
