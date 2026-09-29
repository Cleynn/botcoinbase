"""Walk-forward evaluation on one frozen snapshot.

For each fold: pick parameters on the TRAIN window only (best in-sample score from a small fixed
grid), then trade the following TEST window with those parameters, using the tail of the train
window purely as indicator history. Folds are independent (each starts with the policy capital),
deterministic, and never look at data after their test window.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import product
from typing import Any, Final

from app.backtest.engine import run_backtest
from app.backtest.trader import SimConfig
from app.domain.money import ZERO, canonical, pct, ratio
from app.market.candles import Candle
from app.strategy import decision as strat

LEVEL_CHOICES: Final = (3, 4, 5)
BUFFER_CHOICES: Final = (Decimal("0.01"), Decimal("0.02"))


@dataclass(frozen=True)
class Fold:
    train_start: int
    test_start: int
    test_end: int  # exclusive index


def folds(n_candles: int, train: int, test: int) -> list[Fold]:
    out, a = [], 0
    while a + train + test <= n_candles:
        out.append(Fold(a, a + train, a + train + test))
        a += test
    return out


def _variant(cfg: SimConfig, levels: int, buffer: Decimal) -> SimConfig:
    strategy = cfg.policy.strategy.model_copy(update={"breakout_buffer": buffer})
    policy = cfg.policy.model_copy(update={"strategy": strategy})
    return SimConfig(policy, cfg.rules, cfg.sim_fee, cfg.decision_fee, levels)


def _score(result: dict[str, Any]) -> Decimal:
    perf = result["performance"]
    return Decimal(perf["net_pnl"]) - Decimal(perf["max_drawdown"]) * Decimal(
        result["capital"]["total"]
    )


def walk_forward(candles: Sequence[Candle], cfg: SimConfig, *, scenario: str) -> dict[str, Any]:
    bt = cfg.policy.backtest
    plan = folds(len(candles), bt.walk_forward_train_candles, bt.walk_forward_test_candles)
    if not plan:
        raise ValueError("not enough candles for one walk-forward fold")
    results: list[dict[str, Any]] = []
    for fold in plan:
        train = candles[fold.train_start : fold.test_start]
        best: tuple[Decimal, int, Decimal] | None = None
        for levels, buffer in product(LEVEL_CHOICES, BUFFER_CHOICES):
            r = run_backtest(train, _variant(cfg, levels, buffer), scenario=scenario).data
            key = (_score(r), -levels, -buffer)  # ties: fewer levels, then the smaller buffer
            if best is None or key > (best[0], -best[1], -best[2]):
                best = (key[0], levels, buffer)
        if best is None:  # pragma: no cover  (the choice grid is never empty)
            raise ValueError("no parameter choice")
        _, levels, buffer = best
        history = candles[: fold.test_end]
        # only the tail of the train window is needed as indicator history
        offset = max(0, fold.test_start - (strat.lookback_needed(cfg.policy) + 1))
        test_run = run_backtest(
            history[offset:],
            _variant(cfg, levels, buffer),
            scenario=scenario,
            start_index=fold.test_start - offset,
        ).data
        results.append(
            {
                "fold": len(results) + 1,
                "train_first_candle": candles[fold.train_start].start,
                "test_first_candle": candles[fold.test_start].start,
                "test_last_candle": candles[fold.test_end - 1].start,
                "chosen_levels": levels,
                "chosen_breakout_buffer": canonical(buffer),
                "in_sample_score": canonical(best[0].quantize(Decimal("0.0001"))),
                "test": {
                    "net_pnl": test_run["performance"]["net_pnl"],
                    "return": test_run["performance"]["return"],
                    "max_drawdown": test_run["performance"]["max_drawdown"],
                    "fills": test_run["activity"]["fills"],
                    "cycles_completed": test_run["activity"]["cycles_completed"],
                    "decisions_no_trade": test_run["activity"]["decisions_no_trade"],
                    "halted": test_run["end_state"]["halted"],
                },
            }
        )
    pnls = [Decimal(f["test"]["net_pnl"]) for f in results]
    dds = [Decimal(f["test"]["max_drawdown"]) for f in results]
    total = sum(pnls, ZERO)
    capital = cfg.policy.total_capital
    return {
        "label": "BACKTEST",
        "kind": "WALK_FORWARD",
        "fee_scenario": scenario,
        "fee_rate": canonical(cfg.sim_fee),
        "train_candles": bt.walk_forward_train_candles,
        "test_candles": bt.walk_forward_test_candles,
        "folds": results,
        "summary": {
            "fold_count": len(results),
            "out_of_sample_net_pnl": canonical(total),
            "mean_fold_return": canonical(ratio(total, capital * len(results))),
            "mean_fold_return_display": pct(ratio(total, capital * len(results))),
            "positive_folds": sum(1 for p in pnls if p > 0),
            "negative_folds": sum(1 for p in pnls if p < 0),
            "flat_folds": sum(1 for p in pnls if p == 0),
            "worst_fold_pnl": canonical(min(pnls)),
            "worst_drawdown": canonical(max(dds)),
            "note": "out-of-sample only; folds are independent and start with the policy capital",
        },
    }
