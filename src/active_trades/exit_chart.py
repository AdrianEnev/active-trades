"""Draw factual, reproducible trade-exit charts from exchange OHLC candles.

The renderer has no network access: callers supply the original order plan and
market candles. Decimal prices remain exact in labels and the PNG manifest.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from statistics import median
from typing import Iterable
from zoneinfo import ZoneInfo


BACKGROUND = "#191919"
BULLISH = "#3c679c"
BEARISH = "#b2b2b2"
WICK = "#929292"
REWARD = "#507fb2"
RISK = "#a5666c"
TEXT = "#d0d0d0"
MUTED = "#969696"
WIDTH, HEIGHT = 1800, 1040


@dataclass(frozen=True)
class Candle:
    """One genuine exchange candle, identified by its opening UTC timestamp."""

    time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class ExitChartSpec:
    symbol: str
    exchange: str
    side: str
    opened_ms: int
    closed_ms: int
    entry: Decimal
    stop: Decimal
    target: Decimal
    exit_price: Decimal | None = None
    timezone: str = "Europe/Sofia"


def _price(value: Decimal) -> str:
    # Never round a sub-cent asset or turn a recorded price into scientific notation.
    return format(value, "f")


def _positive(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def _validate(spec: ExitChartSpec, candles: list[Candle]) -> int:
    if spec.side not in {"long", "short"}:
        raise ValueError("Chart side must be long or short.")
    if (not isinstance(spec.opened_ms, int) or not isinstance(spec.closed_ms, int)
            or spec.opened_ms < 0 or spec.closed_ms < spec.opened_ms):
        raise ValueError("Chart needs valid opening and closing timestamps.")
    if not spec.symbol.strip() or not spec.exchange.strip():
        raise ValueError("Chart needs a symbol and exchange.")
    ZoneInfo(spec.timezone)
    if not all(_positive(value) for value in (spec.entry, spec.stop, spec.target)):
        raise ValueError("Chart entry, stop and target must be finite positive Decimal prices.")
    if spec.exit_price is not None and not _positive(spec.exit_price):
        raise ValueError("Chart exit price must be a finite positive Decimal price.")
    direction = 1 if spec.side == "long" else -1
    if (spec.entry - spec.stop) * direction <= 0 or (spec.target - spec.entry) * direction <= 0:
        raise ValueError("Chart stop and target must be on the correct sides of entry.")
    if len(candles) < 2:
        raise ValueError("Chart needs at least two real candles to determine its interval.")
    for index, candle in enumerate(candles):
        if not isinstance(candle.time_ms, int) or candle.time_ms < 0:
            raise ValueError("Candle timestamps must be nonnegative integer milliseconds.")
        if index and candle.time_ms <= candles[index - 1].time_ms:
            raise ValueError("Candles must have unique, increasing timestamps.")
        if (not all(_positive(value) for value in (candle.open, candle.high, candle.low, candle.close))
                or candle.low > min(candle.open, candle.close)
                or candle.high < max(candle.open, candle.close)):
            raise ValueError("Candle OHLC geometry is invalid.")
    interval = int(median(b.time_ms - a.time_ms for a, b in zip(candles, candles[1:])))
    if candles[0].time_ms > spec.opened_ms or candles[-1].time_ms + interval < spec.closed_ms:
        raise ValueError("Market candles do not cover this trade's opening and closing times.")
    return interval


def _font(ImageFont, size: int):
    # Use common system fonts without a platform-specific hard dependency.
    for name in ("DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/System/Library/Fonts/Helvetica.ttc", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


@dataclass(frozen=True)
class _PriceTagLayout:
    width: int
    caption_font_size: int
    price_font_size: int


def _price_tag_layout(ImageFont, labels: list[tuple[str, str]]) -> _PriceTagLayout:
    """Reserve enough axis space for exact prices and their separate captions.

    Normal tags retain their compact width. Long prices widen the right margin
    first, then reduce only the price font if the margin would consume the plot.
    Reject physically unrenderable precision instead of silently overlapping it.
    """
    caption_font_size = price_font_size = 16
    caption_font = _font(ImageFont, caption_font_size)

    def needed_width(size: int) -> int:
        price_font = _font(ImageFont, size)
        return math.ceil(max(caption_font.getlength(caption) + price_font.getlength(price)
                             for caption, price in labels)) + 34  # two 10px insets and 14px separation

    width = min(650, max(207, needed_width(price_font_size)))
    while needed_width(price_font_size) > width and price_font_size > 8:
        price_font_size -= 1
    if needed_width(price_font_size) > width:
        raise ValueError("Recorded prices have too many digits to display legibly in a chart.")
    return _PriceTagLayout(width, caption_font_size, price_font_size)


def _time_label(time_ms: int, timezone: str, *, precise: bool = False) -> str:
    value = datetime.fromtimestamp(time_ms / 1000, ZoneInfo(timezone))
    result = value.strftime("%d %b %Y  %H:%M:%S" if precise else "%d %b  %H:%M")
    if precise and time_ms % 1000:
        result += f".{time_ms % 1000:03d}"
    return result


def _interval_label(interval: int) -> str:
    if interval % 86_400_000 == 0:
        return f"{interval // 86_400_000}D"
    if interval % 3_600_000 == 0:
        return f"{interval // 3_600_000}H"
    if interval % 60_000 == 0:
        return f"{interval // 60_000}m"
    return f"{interval / 1000:g}s"


def _ticks(low: Decimal, high: Decimal) -> list[Decimal]:
    rough = (high - low) / 9
    unit = Decimal(1).scaleb(rough.adjusted())
    step = next(value * unit for value in map(Decimal, (1, 2, 5, 10)) if value * unit >= rough)
    first = (low / step).to_integral_value(rounding=ROUND_CEILING) * step
    count = int(((high - first) / step).to_integral_value(rounding=ROUND_FLOOR))
    return [first + step * index for index in range(count + 1)]


def render_exit_chart(spec: ExitChartSpec, candles: Iterable[Candle], path: Path | str) -> Path:
    """Save a PNG with the recorded plan and exact lifecycle time boundaries.

    The position rectangle starts at ``opened_ms`` rather than rounding to a
    candle. It ends two candle intervals after ``closed_ms``. Empty future space
    remains empty when those market candles do not yet exist. Invalid or missing
    plans/candle coverage raise ValueError, so callers can retry without inventing
    SL/TP values or price history. Pillow is an optional journal dependency.
    """
    candles = list(candles)
    interval = _validate(spec, candles)
    from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_font, font, small = (_font(ImageFont, size) for size in (27, 19, 16))
    levels = [("TP", spec.target, REWARD), ("ENTRY", spec.entry, TEXT), ("SL", spec.stop, RISK)]
    tag_layout = _price_tag_layout(ImageFont, [(name, _price(price)) for name, price, _ in levels])
    price_font = _font(ImageFont, tag_layout.price_font_size)
    left, top, right, bottom = 44, 128, WIDTH - tag_layout.width - 63, HEIGHT - 170
    end_ms = spec.closed_ms + 2 * interval
    first_ms = min(candles[0].time_ms - interval, spec.opened_ms - interval)
    last_ms = max(candles[-1].time_ms + interval * 3, end_ms + interval * 2)
    prices = [candle.low for candle in candles] + [candle.high for candle in candles]
    prices.extend((spec.stop, spec.target, spec.entry))
    if spec.exit_price is not None:
        prices.append(spec.exit_price)
    low, high = min(prices), max(prices)
    margin = (high - low) * Decimal("0.11")
    low, high = low - margin, high + margin

    def x(time_ms: int) -> float:
        return left + (time_ms - first_ms) / (last_ms - first_ms) * (right - left)

    def y(price: Decimal) -> float:
        return bottom - float((price - low) / (high - low)) * (bottom - top)

    def dotted_line(a: tuple[float, float], b: tuple[float, float], color: str, dash=5):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = max(abs(dx), abs(dy))
        if not length:
            return
        for offset in range(0, int(length), dash * 2):
            end = min(offset + dash, length)
            draw.line((a[0] + dx * offset / length, a[1] + dy * offset / length,
                       a[0] + dx * end / length, a[1] + dy * end / length), fill=color, width=1)

    draw.text((left, 29), f"{spec.symbol}  /  {spec.exchange.upper()}", font=title_font, fill=TEXT)
    draw.text((right, 32), f"{spec.side.upper()}  ·  {_interval_label(interval)}", font=font,
              fill=REWARD if spec.side == "long" else BEARISH, anchor="ra")
    draw.text((left, 76), "TRADE EXIT  /  ORIGINAL POSITION PLAN", font=small, fill=MUTED)
    draw.text((WIDTH - 44, 35), "USDT", font=small, fill=MUTED, anchor="ra")
    draw.line((left, 105, WIDTH - 44, 105), fill="#303030")

    # Put risk/reward underneath the candles, preserving their reference palette.
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    start_x, end_x = x(spec.opened_ms), x(end_ms)
    for level, color, alpha in ((spec.target, REWARD, 47), (spec.stop, RISK, 43)):
        rgb = tuple(bytes.fromhex(color[1:]))
        overlay_draw.rectangle((start_x, min(y(spec.entry), y(level)), end_x, max(y(spec.entry), y(level))),
                               fill=(*rgb, alpha), outline=(*rgb, 150), width=1)
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(image)

    candle_width = max(1, min(11, (x(candles[0].time_ms + interval) - x(candles[0].time_ms)) * .57))
    for candle in candles:
        cx = x(candle.time_ms)
        color = BULLISH if candle.close >= candle.open else BEARISH
        draw.line((cx, y(candle.high), cx, y(candle.low)), fill=WICK, width=1)
        body_top, body_bottom = sorted((y(candle.open), y(candle.close)))
        draw.rectangle((cx - candle_width / 2, body_top, cx + candle_width / 2, max(body_top + 1, body_bottom)),
                       fill=color, outline=BACKGROUND)

    # Timestamp markers use real execution instants, including inside a candle.
    dotted_line((start_x, top), (start_x, bottom), "#596372", dash=4)
    close_x = x(spec.closed_ms)
    dotted_line((close_x, top), (close_x, bottom), "#777777", dash=4)
    entry_y = y(spec.entry)
    draw.polygon(((start_x, entry_y - 7), (start_x + 7, entry_y),
                  (start_x, entry_y + 7), (start_x - 7, entry_y)), fill=TEXT, outline=BACKGROUND)
    if spec.exit_price is not None:
        exit_y = y(spec.exit_price)
        draw.ellipse((close_x - 5, exit_y - 5, close_x + 5, exit_y + 5), fill=BEARISH, outline=BACKGROUND, width=1)
    draw.line((left, bottom + 1, WIDTH - 44, bottom + 1), fill="#303030")

    # Axis labels are independent of the exact price tags and do not form a grid.
    for tick in _ticks(low, high):
        ty = y(tick)
        if all(abs(ty - y(price)) > 28 for _, price, _ in levels):
            label = _price(tick).rstrip("0").rstrip(".") if "." in _price(tick) else _price(tick)
            draw.text((WIDTH - 44, ty), label, font=small, fill=MUTED, anchor="rm")

    label_rows = sorted((y(price), name, price, color) for name, price, color in levels)
    # Very tight SLs still get individually legible exact price labels.
    placed = []
    for actual_y, name, price, color in label_rows:
        label_y = max(actual_y, placed[-1][0] + 45) if placed else actual_y
        placed.append((label_y, actual_y, name, price, color))
    overflow = max(0, placed[-1][0] - bottom + 15)
    for label_y, actual_y, name, price, color in placed:
        label_y -= overflow
        dotted_line((start_x, actual_y), (end_x, actual_y), color, dash=5)
        draw.line((end_x, actual_y, right + 13, actual_y, right + 26, label_y), fill=color)
        draw.rounded_rectangle((right + 28, label_y - 19, WIDTH - 35, label_y + 20), radius=4,
                               fill="#232323", outline=color)
        draw.text((right + 38, label_y), name, font=small, fill=color, anchor="lm")
        draw.text((WIDTH - 45, label_y), _price(price), font=price_font, fill=TEXT, anchor="rm")

    # Time ticks remain evenly spaced in time even if exchange candle data has gaps.
    for index in range(6):
        tick_ms = int(first_ms + (last_ms - first_ms) * (index + .35) / 6)
        draw.text((x(tick_ms), bottom + 24), _time_label(tick_ms, spec.timezone), font=small, fill=MUTED, anchor="mt")
    draw.text((left, HEIGHT - 96), f"OPENED  {_time_label(spec.opened_ms, spec.timezone, precise=True)}",
              font=small, fill=TEXT)
    draw.text((left, HEIGHT - 58), f"CLOSED  {_time_label(spec.closed_ms, spec.timezone, precise=True)}",
              font=small, fill=TEXT)
    rr = abs((spec.target - spec.entry) / (spec.entry - spec.stop))
    draw.text((right, HEIGHT - 96), f"PLANNED R:R  {rr:.2f}", font=small, fill=TEXT, anchor="ra")
    draw.text((right, HEIGHT - 58), f"{spec.timezone}  ·  Shaded plan ends 2 candles after close",
              font=small, fill=MUTED, anchor="ra")
    draw.text((WIDTH - 44, HEIGHT - 58), "ACTIVE TRADES", font=small, fill=MUTED, anchor="ra")

    manifest = {
        "version": 1, "symbol": spec.symbol, "exchange": spec.exchange, "side": spec.side,
        "opened_ms": spec.opened_ms, "closed_ms": spec.closed_ms, "plan_end_ms": end_ms,
        "entry": _price(spec.entry), "stop": _price(spec.stop), "target": _price(spec.target),
        "exit_price": _price(spec.exit_price) if spec.exit_price is not None else None,
        "timezone": spec.timezone, "interval_ms": interval, "candle_count": len(candles),
        "first_candle_ms": candles[0].time_ms, "last_candle_ms": candles[-1].time_ms,
    }
    info = PngImagePlugin.PngInfo()
    info.add_text("active_trades_exit_chart", json.dumps(manifest, sort_keys=True))
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(target_path, format="PNG", pnginfo=info)
    return target_path
