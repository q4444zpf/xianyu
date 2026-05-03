"""通过 QQ 邮箱 SMTP 发送 HTML 通知邮件。"""

from __future__ import annotations

import smtplib
import ssl
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from html import escape
from typing import Iterable

from loguru import logger

from .models import Item


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

    def _send_raw(self, subject: str, text_body: str, html_body: str | None = None) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr(("闲鱼监控", self.mail_from))
        msg["To"] = ", ".join(self.mail_to)
        msg["Date"] = formatdate(localtime=True)
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        if html_body:
            msg.attach(MIMEText(html_body, "html", "utf-8"))

        context = ssl.create_default_context()
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=30) as smtp:
                smtp.login(self.user, self.password)
                smtp.sendmail(self.mail_from, self.mail_to, msg.as_string())
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(self.user, self.password)
                smtp.sendmail(self.mail_from, self.mail_to, msg.as_string())

        logger.success("邮件已发送给 {}：{}", self.mail_to, subject)

    def send_login_action_required(self, summary: str) -> None:
        """登录态不可用时提醒用户执行 login（与商品推送邮件区分主题）。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subject = f"【闲鱼监控】需重新登录闲鱼 ({datetime.now():%Y-%m-%d %H:%M})"
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
            subject = f"【闲鱼监控】{keyword} 新增共 {total} 条 · 本邮件 {in_mail} 条 ({ts})"
        else:
            subject = f"【闲鱼监控】{keyword} 新增 {in_mail} 条 ({ts})"
        html_body = _build_html(keyword, items, omitted_extra=omitted)
        text_body = _build_text(keyword, items, omitted_extra=omitted)
        self._send_raw(subject, text_body, html_body)


def _build_html(keyword: str, items: Iterable[Item], omitted_extra: int = 0) -> str:
    rows: list[str] = []
    for item in items:
        url = escape(item.normalized_detail_url())
        title = escape(item.title or "(无标题)")
        price = escape(item.price or "-")
        location = escape(item.location or "")
        publish = escape(item.publish_text or "")
        seller = escape(item.seller or "")
        img = escape(_normalize_img(item.image_url))
        rows.append(
            f"""
            <tr>
              <td style="padding:8px;border-bottom:1px solid #eee;vertical-align:top;width:120px;">
                {f'<img src="{img}" alt="" style="width:110px;height:110px;object-fit:cover;border-radius:6px;">' if img else ''}
              </td>
              <td style="padding:8px;border-bottom:1px solid #eee;vertical-align:top;">
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
              </td>
            </tr>
            """
        )
    body = "\n".join(rows)
    n = sum(1 for _ in items)
    extra_block = ""
    if omitted_extra > 0:
        extra_block = (
            f'<div style="padding:12px 20px;background:#fff3e0;color:#e65100;font-size:14px;">'
            f'另有 <b>{omitted_extra}</b> 条新增未列入本邮件，将在后续监控轮次继续发送。'
            f"</div>"
        )
    return f"""
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
        lines.append("")
    if omitted_extra > 0:
        lines.append(
            f"（另有 {omitted_extra} 条新增未列入本邮件，将在后续监控轮次继续发送。）"
        )
        lines.append("")
    return "\n".join(lines)


def _normalize_img(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return url
