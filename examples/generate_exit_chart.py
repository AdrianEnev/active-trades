"""Generate the labeled, synthetic example used to preview the journal renderer.

Run from the project directory: python examples/generate_exit_chart.py
This fixture is illustrative OHLC data, never used by the live journal.
"""
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from random import Random

from active_trades.exit_chart import Candle, ExitChartSpec, render_exit_chart


def main():
    rng = Random(31)
    start = int(datetime(2026, 10, 14, 10, tzinfo=UTC).timestamp() * 1000)
    hour = 3_600_000
    knots = [(0, 83100), (9, 82700), (17, 81200), (24, 81600), (31, 80850),
             (39, 81900), (44, 81700), (51, 82700), (57, 82300), (63, 83200), (70, 83400)]
    data = []
    previous = D("83080.0")
    for index in range(71):
        (a, va), (b, vb) = next((left, right) for left, right in zip(knots, knots[1:])
                               if left[0] <= index <= right[0])
        midpoint = va + (vb - va) * (index - a) / (b - a)
        close = D(str(round(midpoint + rng.uniform(-170, 170), 1)))
        high = max(previous, close) + D(str(round(rng.uniform(65, 290), 1)))
        low = min(previous, close) - D(str(round(rng.uniform(65, 245), 1)))
        data.append(Candle(start + index * hour, previous, high, low, close))
        previous = close
    position = ExitChartSpec(
        symbol="BTCUSDT  /  ILLUSTRATIVE EXAMPLE", exchange="bingx", side="long",
        opened_ms=start + 24 * hour + 17 * 60_000 + 3421,
        closed_ms=start + 63 * hour + 24 * 60_000 + 8912,
        entry=D("81600.5"), stop=D("80500.0"), target=D("83200.0"), exit_price=D("83200.0"),
    )
    path = render_exit_chart(position, data, Path(__file__).with_name("exit-chart-example.png"))
    print(path)


if __name__ == "__main__":
    main()
