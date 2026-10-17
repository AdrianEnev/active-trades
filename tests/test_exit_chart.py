import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D

import pytest

Image = pytest.importorskip("PIL.Image")

from active_trades.exit_chart import (
    BACKGROUND, BULLISH, BEARISH, Candle, ExitChartSpec, _font, _price_tag_layout, render_exit_chart,
)


T = 1_792_152_000_000
HOUR = 3_600_000


def candles():
    return [Candle(T + index * HOUR, D("100"), D("104"), D("97"), D("102") if index % 2 else D("99"))
            for index in range(8)]


def spec(**kw):
    values = dict(symbol="BTCUSDT", exchange="bingx", side="long", opened_ms=T + HOUR + 123_456,
                  closed_ms=T + 7 * HOUR + 654_321, entry=D("100.00"), stop=D("98.000"),
                  target=D("104.0000"), exit_price=D("103.1234"))
    return ExitChartSpec(**(values | kw))


def test_exact_plan_and_intracandle_execution_times_survive_in_generated_png(tmp_path):
    position = spec()
    path = render_exit_chart(position, candles(), tmp_path / "nested" / "long.png")
    with Image.open(path) as image:
        assert image.size == (1800, 1040)
        manifest = json.loads(image.info["active_trades_exit_chart"])
        assert manifest["entry"] == "100.00"
        assert manifest["stop"] == "98.000"
        assert manifest["target"] == "104.0000"
        assert manifest["exit_price"] == "103.1234"
        assert manifest["opened_ms"] == position.opened_ms
        assert manifest["closed_ms"] == position.closed_ms
        assert manifest["plan_end_ms"] == position.closed_ms + 2 * HOUR
        assert manifest["last_candle_ms"] < manifest["closed_ms"] < manifest["plan_end_ms"]
        # The reserved future area contains no fabricated candles.
        assert manifest["candle_count"] == len(candles())
        assert image.getpixel((5, 5)) == tuple(bytes.fromhex(BACKGROUND[1:]))
        colors = {color for _, color in image.getcolors(image.width * image.height)}
        assert tuple(bytes.fromhex(BULLISH[1:])) in colors
        assert tuple(bytes.fromhex(BEARISH[1:])) in colors


@pytest.mark.parametrize("side,stop,target", [("long", "0.00000012100", "0.00000013000"),
                                             ("short", "0.00000013000", "0.00000012100")])
def test_long_short_and_subcent_assets_keep_precise_prices(tmp_path, side, stop, target):
    price = D("0.00000012500")
    data = [Candle(T + index * HOUR, price, D("0.00000013100"), D("0.00000012000"), price)
            for index in range(8)]
    path = render_exit_chart(spec(side=side, entry=price, stop=D(stop), target=D(target), exit_price=None),
                             data, tmp_path / f"{side}.png")
    with Image.open(path) as image:
        manifest = json.loads(image.info["active_trades_exit_chart"])
        assert manifest["side"] == side
        assert manifest["entry"] == "0.00000012500"
        assert manifest["stop"] == stop
        assert manifest["target"] == target


def test_long_decimal_prices_have_separate_nonoverlapping_caption_and_price_space(tmp_path):
    from PIL import ImageFont

    position = spec(entry=D("100.123456789012345678"), stop=D("98.123456789012345678"),
                    target=D("104.123456789012345678"))
    labels = [("ENTRY", format(position.entry, "f")), ("SL", format(position.stop, "f")),
              ("TP", format(position.target, "f"))]
    layout = _price_tag_layout(ImageFont, labels)
    caption_font = _font(ImageFont, layout.caption_font_size)
    price_font = _font(ImageFont, layout.price_font_size)
    assert layout.width > 207  # Expand the margin rather than collide or truncate.
    for caption, price in labels:
        caption_right = 10 + caption_font.getlength(caption)
        price_left = layout.width - 10 - price_font.getlength(price)
        assert price_left - caption_right >= 14
    path = render_exit_chart(position, candles(), tmp_path / "precise.png")
    with Image.open(path) as image:
        assert json.loads(image.info["active_trades_exit_chart"])["entry"] == labels[0][1]
        # The expanded tag border is part of the actual rendered output.
        tag_left = image.width - 35 - layout.width
        assert any(image.getpixel((tag_left, row)) != tuple(bytes.fromhex(BACKGROUND[1:]))
                   for row in range(128, 870))


def test_extremely_long_prices_fit_with_bounded_margin_and_font():
    from PIL import ImageFont

    labels = [("ENTRY", "100." + "1234567890" * 8)]
    layout = _price_tag_layout(ImageFont, labels)
    assert layout.width == 650
    assert 8 <= layout.price_font_size < 16
    caption_font = _font(ImageFont, layout.caption_font_size)
    price_font = _font(ImageFont, layout.price_font_size)
    assert caption_font.getlength("ENTRY") + price_font.getlength(labels[0][1]) + 34 <= layout.width


def test_renderer_is_deterministic_and_inputs_are_immutable(tmp_path):
    position, data = spec(), candles()
    first = render_exit_chart(position, data, tmp_path / "one.png")
    second = render_exit_chart(position, iter(data), tmp_path / "two.png")
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(FrozenInstanceError):
        position.entry = D("99")
    with pytest.raises(FrozenInstanceError):
        data[0].close = D("99")


@pytest.mark.parametrize("change", [dict(side="buy"), dict(stop=D("101")), dict(target=D("99")),
                                   dict(entry=D("NaN")), dict(target=D("Infinity")),
                                   dict(exit_price=D("0")), dict(closed_ms=T), dict(opened_ms=-1)])
def test_invalid_plans_fail_without_creating_a_misleading_chart(tmp_path, change):
    path = tmp_path / "invalid.png"
    with pytest.raises(ValueError):
        render_exit_chart(spec(**change), candles(), path)
    assert not path.exists()


@pytest.mark.parametrize("data", [[], candles()[:1], list(reversed(candles())), candles() + candles()[-1:],
                                  [replace(candles()[0], high=D("98")), *candles()[1:]],
                                  [replace(candles()[0], low=D("101")), *candles()[1:]],
                                  [replace(candles()[0], close=D("NaN")), *candles()[1:]],
                                  candles()[2:], candles()[:4]])
def test_invalid_or_incomplete_market_data_is_not_drawn(tmp_path, data):
    with pytest.raises(ValueError):
        render_exit_chart(spec(), data, tmp_path / "invalid.png")


def test_short_plan_requires_stop_above_and_target_below_entry(tmp_path):
    with pytest.raises(ValueError, match="correct sides"):
        render_exit_chart(spec(side="short"), candles(), tmp_path / "invalid.png")


def test_position_region_begins_at_execution_time_and_ends_after_close(tmp_path):
    # Keep candle prices away from the reward area so sampling measures shading.
    data = [Candle(T + index * HOUR, D("99.1"), D("99.3"), D("98.9"), D("99.2"))
            for index in range(8)]
    position = spec(opened_ms=T + HOUR // 2, target=D("110"), stop=D("95"), exit_price=None)
    path = render_exit_chart(position, data, tmp_path / "timing.png")
    # Price range [95, 110] receives 11% padding; at reward center there are no candles/level lines.
    first_ms = T - HOUR
    last_ms = position.closed_ms + 4 * HOUR
    def x(timestamp):
        return round(44 + (timestamp - first_ms) / (last_ms - first_ms) * (1530 - 44))
    y = round(870 - float((D("105") - D("93.35")) / D("18.30")) * (870 - 128))
    with Image.open(path) as image:
        before = image.getpixel((x(position.opened_ms - HOUR // 8), y))
        during = image.getpixel((x(position.opened_ms + HOUR // 8), y))
        after_close = image.getpixel((x(position.closed_ms + HOUR), y))
        after_end = image.getpixel((x(position.closed_ms + 3 * HOUR), y))
        assert before == after_end == tuple(bytes.fromhex(BACKGROUND[1:]))
        assert during == after_close != before
