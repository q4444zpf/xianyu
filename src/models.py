"""数据模型定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Item:
    """单个闲鱼商品的标准化表示。"""

    item_id: str
    title: str = ""
    price: str = ""
    location: str = ""
    publish_text: str = ""
    image_url: str = ""
    # 详情页商品主轮播大图（仅当前商品，不含推荐位）；发信前拉取；邮件可展示多张
    gallery_urls: list[str] = field(default_factory=list)
    # 详情页中的商品码（若未抓到则回退为 item_id）
    item_code: str = ""
    # 详情页右侧「APP/商品码」弹层里的官方二维码（data:image/png;base64,...）
    app_qr_data_url: str = ""
    # 详情接口 shareInfoJsonString 里的分享 deeplink（优先用于生成可跳转二维码）
    app_qr_payload: str = ""
    detail_url: str = ""
    seller: str = ""
    fetched_at: datetime = field(default_factory=datetime.now)

    def normalized_detail_url(self) -> str:
        """规范化详情链接，方便邮件中点击跳转。"""
        if self.detail_url:
            return self.detail_url
        if self.item_id:
            return f"https://www.goofish.com/item?id={self.item_id}"
        return ""
