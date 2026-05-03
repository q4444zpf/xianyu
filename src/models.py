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
