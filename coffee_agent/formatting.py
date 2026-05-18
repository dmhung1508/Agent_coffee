from __future__ import annotations

from typing import Any

from .menu_client import detail_from_item
from .state import Cart


def format_price(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:,.0f} VND".replace(",", ".")
    return "chưa có giá"


def item_summary(item: dict[str, Any]) -> str:
    detail = detail_from_item(item)
    name = detail.get("name", "Khong ro ten")
    item_type = item.get("type") or detail.get("type") or "unknown"
    price = format_price(detail.get("price"))
    unit = detail.get("unit") or ""
    item_id = detail.get("id")
    description = detail.get("description") or ""
    options = detail.get("options")

    suffix = f" / {unit}" if unit else ""
    identifier = f" [id: {item_id}]" if item_id else ""
    lines = [f"- {name} ({item_type}) - {price}{suffix}{identifier}"]

    if description and description.strip().lower() != name.strip().lower():
        lines.append(f"  {description}")

    if options:
        if isinstance(options, list):
            opts_str = ", ".join(str(o) for o in options)
        elif isinstance(options, dict):
            opts_str = ", ".join(f"{k}: {v}" for k, v in options.items())
        else:
            opts_str = str(options)
        lines.append(f"  Options: {opts_str}")

    return "\n".join(lines)


def render_catalog(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(khong co ket qua moi)"
    return "\n".join(item_summary(item) for item in items)


def render_cart(cart: Cart) -> str:
    if cart.is_empty():
        return "Giỏ hàng đang trống."

    lines = []
    for index, item in enumerate(cart.contents, start=1):
        unit = item.unit or "phan"
        lines.append(f"{index}. {item.quantity} x {item.name} - {format_price(item.price)} / {unit}")

    total = cart.total()
    if total is not None:
        lines.append(f"Tạm tính: {format_price(total)}")
    return "\n".join(lines)
