"""通过 QQ 邮箱 SMTP 发送 HTML 通知邮件。"""

from __future__ import annotations

import smtplib
import ssl
import urllib.error
import urllib.request
from datetime import datetime
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from html import escape
from typing import Iterable

from loguru import logger

from .auth import DEFAULT_USER_AGENT
from .crawler import _url_is_likely_idle_product_photo
from .models import Item

# 邮件主题行前缀（显示在收件箱「主题」列）
MAIL_SUBJECT_TAG = "I love you"

# 单条商品邮件内最多内联的轮播图张数（避免邮件过大）
_MAX_MAIL_GALLERY_IMAGES = 24


class EmailNotifier:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        mail_from: str,
        mail_to: list[str],
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.mail_from = mail_from
        self.mail_to = mail_to

    def _smtp_send(self, msg: MIMEMultipart) -> None:
        context = ssl.create_default_context()
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=60) as smtp:
                smtp.login(self.user, self.password)
                smtp.sendmail(self.mail_from, self.mail_to, msg.as_string())
        else:
            with smtplib.SMTP(self.host, self.port, timeout=60) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(self.user, self.password)
                smtp.sendmail(self.mail_from, self.mail_to, msg.as_string())

    def _send_raw(self, subject: str, text_body: str, html_body: str | None = None) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr(("闲鱼监控", self.mail_from))
        msg["To"] = ", ".join(self.mail_to)
        msg["Date"] = formatdate(localtime=True)
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        if html_body:
            msg.attach(MIMEText(html_body, "html", "utf-8"))
        self._smtp_send(msg)
        logger.success("邮件已发送给 {}：{}", self.mail_to, subject)

    def send_login_action_required(self, summary: str) -> None:
        """登录态不可用时提醒用户执行 login（与商品推送邮件区分主题）。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subject = f"【{MAIL_SUBJECT_TAG}】需重新登录闲鱼 ({datetime.now():%Y-%m-%d %H:%M})"
        text_body = (
            "闲鱼登录态不可用，本轮已跳过抓取。\n\n"
            f"原因：{summary}\n\n"
            "请在本机进入项目目录后执行：\n"
            "  python main.py login\n\n"
            "在浏览器中完成扫码登录后，再执行：\n"
            "  python main.py once\n"
            "或：\n"
            "  python main.py run\n\n"
            f"检测时间：{now}\n"
        )
        safe = escape(summary)
        html_body = (
            "<html><body style=\"font-family:sans-serif;line-height:1.6;padding:16px;\">"
            "<h2 style=\"color:#c62828;\">需要重新登录闲鱼</h2>"
            f"<p><b>原因：</b>{safe}</p>"
            "<p>请在本机项目目录执行 <code>python main.py login</code> 完成扫码登录后，再运行 "
            "<code>python main.py once</code> 或 <code>python main.py run</code>。</p>"
            f"<p style=\"color:#666;font-size:12px;\">检测时间：{escape(now)}</p>"
            "</body></html>"
        )
        self._send_raw(subject, text_body, html_body)

    def send_new_items(
        self,
        keyword: str,
        items: list[Item],
        *,
        total_new: int | None = None,
    ) -> None:
        if not items:
            logger.debug("没有新增商品，跳过发送邮件。")
            return

        in_mail = len(items)
        total = total_new if total_new is not None else in_mail
        omitted = max(0, total - in_mail)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        if omitted > 0:
            subject = f"【{MAIL_SUBJECT_TAG}】{keyword} 新增共 {total} 条 · 本邮件 {in_mail} 条 ({ts})"
        else:
            subject = f"【{MAIL_SUBJECT_TAG}】{keyword} 新增 {in_mail} 条 ({ts})"
        text_body = _build_text(keyword, items, omitted_extra=omitted)
        html_body, image_parts = _build_html_with_inline_images(
            keyword, items, omitted_extra=omitted
        )

        root = MIMEMultipart("alternative")
        root["Subject"] = Header(subject, "utf-8")
        root["From"] = formataddr(("闲鱼监控", self.mail_from))
        root["To"] = ", ".join(self.mail_to)
        root["Date"] = formatdate(localtime=True)
        root.attach(MIMEText(text_body, "plain", "utf-8"))

        if image_parts:
            rel = MIMEMultipart("related")
            rel.set_param("type", "text/html")
            rel.attach(MIMEText(html_body, "html", "utf-8"))
            for part in image_parts:
                rel.attach(part)
            root.attach(rel)
        else:
            root.attach(MIMEText(html_body, "html", "utf-8"))

        self._smtp_send(root)
        logger.success("邮件已发送给 {}：{}", self.mail_to, subject)


def _build_html_with_inline_images(
    keyword: str,
    items: Iterable[Item],
    omitted_extra: int = 0,
) -> tuple[str, list[MIMEImage]]:
    """生成 HTML，并尽可能把商品图下载为 CID 内联附件（客户端不拦截外链时也能显示）。"""
    items_list = list(items)
    rows: list[str] = []
    image_parts: list[MIMEImage] = []
    for idx, item in enumerate(items_list):
        url = escape(item.normalized_detail_url())
        title = escape(item.title or "(无标题)")
        price = escape(item.price or "-")
        location = escape(item.location or "")
        publish = escape(item.publish_text or "")
        seller = escape(item.seller or "")
        gallery = _effective_gallery_urls(item)
        img_html = _gallery_cell_html(gallery, idx, image_parts)
        rows.append(
            f"""
            <tr>
              <td style="padding:12px 14px;border-bottom:1px solid #eee;vertical-align:top;">
                <div style="font-size:14px;font-weight:bold;line-height:1.4;margin-bottom:6px;">
                  <a href="{url}" target="_blank" style="color:#1a73e8;text-decoration:none;">{title}</a>
                </div>
                <div style="font-size:16px;color:#e53935;font-weight:bold;margin-bottom:4px;">{price}</div>
                <div style="font-size:12px;color:#666;line-height:1.6;">
                  {f'<span>卖家：{seller}</span>　' if seller else ''}
                  {f'<span>地区：{location}</span>　' if location else ''}
                  {f'<span>{publish}</span>' if publish else ''}
                </div>
                <div style="font-size:12px;margin-top:6px;">
                  <a href="{url}" target="_blank" style="color:#1a73e8;">查看详情 -&gt;</a>
                </div>
                <div style="margin-top:10px;">
                  {img_html}
                </div>
              </td>
            </tr>
            """
        )
    body = "\n".join(rows)
    n = len(items_list)
    extra_block = ""
    if omitted_extra > 0:
        extra_block = (
            f'<div style="padding:12px 20px;background:#fff3e0;color:#e65100;font-size:14px;">'
            f'另有 <b>{omitted_extra}</b> 条新增未列入本邮件，将在后续监控轮次继续发送。'
            f"</div>"
        )
    html = f"""
    <html>
      <body style="font-family:-apple-system,Helvetica,Arial,'PingFang SC','Microsoft YaHei',sans-serif;background:#f6f7f9;padding:20px;">
        <div style="max-width:720px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06);">
          <div style="padding:16px 20px;background:#1a73e8;color:#fff;">
            <div style="font-size:18px;font-weight:bold;">闲鱼监控 · {escape(keyword)}</div>
            <div style="font-size:12px;opacity:0.85;margin-top:4px;">本邮件列出 {n} 条 · {datetime.now():%Y-%m-%d %H:%M:%S}</div>
          </div>
          {extra_block}
          <table style="width:100%;border-collapse:collapse;">
            {body}
          </table>
          <div style="padding:12px 20px;font-size:12px;color:#999;background:#fafafa;">
            本邮件由闲鱼监控程序自动发送，请勿直接回复。
          </div>
        </div>
      </body>
    </html>
    """
    return html, image_parts


def _build_text(keyword: str, items: Iterable[Item], omitted_extra: int = 0) -> str:
    lines = [f"闲鱼监控 - 关键词：{keyword}", ""]
    for idx, item in enumerate(items, 1):
        lines.append(f"{idx}. {item.title}")
        if item.price:
            lines.append(f"   价格：{item.price}")
        if item.seller or item.location or item.publish_text:
            extras = "   ".join(
                x for x in (item.seller, item.location, item.publish_text) if x
            )
            if extras:
                lines.append(f"   {extras}")
        lines.append(f"   链接：{item.normalized_detail_url()}")
        for j, img in enumerate(_effective_gallery_urls(item), 1):
            lines.append(f"   轮播图{j}：{img}")
        lines.append("")
    if omitted_extra > 0:
        lines.append(
            f"（另有 {omitted_extra} 条新增未列入本邮件，将在后续监控轮次继续发送。）"
        )
        lines.append("")
    return "\n".join(lines)


def _effective_gallery_urls(item: Item) -> list[str]:
    """详情轮播 URL + 列表缩略图（去重），过滤站点 Logo 等非商品图。"""
    seen: set[str] = set()
    out: list[str] = []
    for u in getattr(item, "gallery_urls", None) or []:
        nu = _normalize_img(u)
        if not nu or nu in seen or not _url_is_likely_idle_product_photo(nu):
            continue
        seen.add(nu)
        out.append(nu)
    back = _normalize_img(item.image_url or "")
    if back and back not in seen and _url_is_likely_idle_product_photo(back):
        out.append(back)
    return out[:_MAX_MAIL_GALLERY_IMAGES]


def _normalize_img(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return url


def _guess_image_subtype_from_bytes(data: bytes) -> str:
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 2 and data[:2] == b"\xff\xd8":
        return "jpeg"
    return "jpeg"


def _fetch_image_bytes_with_subtype(url: str, timeout: int = 20) -> tuple[bytes | None, str]:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Referer": "https://www.goofish.com/",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if len(data) > 5_000_000:
                logger.warning("商品图过大已跳过内联: {} bytes", len(data))
                return None, "jpeg"
            ct = (resp.headers.get("Content-Type") or "").lower()
            if "png" in ct:
                return data, "png"
            if "webp" in ct:
                return data, "webp"
            if "gif" in ct:
                return data, "gif"
            if "jpeg" in ct or "jpg" in ct:
                return data, "jpeg"
            return data, _guess_image_subtype_from_bytes(data)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.debug("下载商品图失败: {} {}", url[:120], exc)
        return None, "jpeg"


def _gallery_cell_html(
    urls: list[str],
    item_idx: int,
    image_parts: list[MIMEImage],
) -> str:
    """主轮播多图内联；每张独立 CID。"""
    raw_size_style = (
        "max-width:100%;width:auto;height:auto;display:block;border-radius:6px;"
        "border:1px solid #eee;margin-bottom:8px;"
    )
    if not urls:
        return '<span style="font-size:12px;color:#999;">无图</span>'
    cells: list[str] = []
    for j, raw_img in enumerate(urls):
        cid = f"idle-item-{item_idx}-g{j}@local"
        data, subtype = _fetch_image_bytes_with_subtype(raw_img)
        if data:
            part = MIMEImage(data, _subtype=subtype)
            part.add_header("Content-ID", f"<{cid}>")
            part.add_header(
                "Content-Disposition",
                "inline",
                filename=f"item-{item_idx}-g{j}.{subtype}",
            )
            image_parts.append(part)
            cells.append(
                f'<img src="cid:{cid}" alt="商品轮播图" style="{raw_size_style}">'
            )
        else:
            esc = escape(raw_img)
            cells.append(
                '<div style="font-size:11px;color:#666;word-break:break-all;'
                'padding:4px;border:1px dashed #ccc;border-radius:6px;">'
                f'<a href="{esc}" target="_blank" rel="noopener">图{j + 1}（外链）</a>'
                "</div>"
            )
    return '<div style="display:block;">' + "".join(cells) + "</div>"
