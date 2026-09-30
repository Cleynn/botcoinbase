"""Grid sizing from the funds actually available, under a named capital profile. SYNTHETIC data."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.capital.funds import Funds
from app.capital.profiles import get_profile
from app.config import PairPolicy, load_settings
from app.strategy import decision as strat
from app.strategy.grid import CapitalPolicy, GridRejected, MarketRules, build_grid
from tests.market_data import choppy_range

D = Decimal
NOW = datetime(2026, 9, 30, tzinfo=UTC)
BASE = load_settings({"TD_ENVIRONMENT": "test", "TD_SECRET_KEY": "x" * 40}).pair_policy
POLICY = BASE.model_copy(
    update={"strategy": BASE.strategy.model_copy(update={"max_trend_separation": D("0.05")})}
)
RULES = MarketRules(D("0.01"), D("0.0001"), D("0.01"), D("0.0001"), None, D("1"))
CANDLES = choppy_range(1500)[:1200]


def funds(available: str, hold: str = "0", inventory: str = "0") -> Funds:
    return Funds(D(available), D(hold), D(inventory), "EXCHANGE", NOW)


def decide(policy: PairPolicy = POLICY, *, rules: MarketRules = RULES, **kw: Any) -> strat.Decision:
    return strat.decide(CANDLES, policy=policy, rules=rules, maker_fee=D("0.002"), **kw)


def test_full_pilot_funds_size_exactly_like_the_static_policy() -> None:
    static = decide()
    sized = decide(funds=funds("50"))
    assert static.action == strat.GRID and sized.plan == static.plan


def test_extra_deposits_never_widen_the_grid() -> None:
    assert decide(funds=funds("5000")).plan == decide().plan


@pytest.mark.parametrize(("available", "room"), [("30", "15"), ("40", "25"), ("20", "5")])
def test_a_smaller_balance_shrinks_the_grid_to_available_less_reserve(
    available: str, room: str
) -> None:
    d = decide(funds=funds(available))
    assert d.action == strat.GRID and d.plan is not None
    assert d.plan.max_commitment <= D(room)
    assert d.plan.max_commitment < decide().plan.max_commitment  # type: ignore[union-attr]


@pytest.mark.parametrize("available", ["15", "10", "0"])
def test_nothing_above_the_reserve_is_no_trade(available: str) -> None:
    d = decide(funds=funds(available))
    assert d.action == strat.NO_TRADE and d.reasons == ("NO_DEPLOYABLE_CAPITAL",)


def test_quote_held_by_open_orders_and_inventory_shrink_the_grid() -> None:
    assert decide(funds=funds("50", hold="20", inventory="0")).plan.max_commitment <= D("15")  # type: ignore[union-attr]
    assert decide(funds=funds("50", hold="20", inventory="15")).reasons == (
        "NO_DEPLOYABLE_CAPITAL",
    )


def test_the_expanded_profile_sizes_against_its_own_limits() -> None:
    policy = POLICY.for_profile(get_profile("expanded"))
    assert (policy.total_capital, policy.min_reserve, policy.max_deployment) == (
        D(100), D(25), D(75),
    )  # fmt: skip
    d = decide(policy, funds=funds("100"))
    assert d.action == strat.GRID and d.plan is not None
    assert D("35") < d.plan.max_commitment <= D("75")
    assert decide(policy, funds=funds("100")).plan != decide().plan
    assert decide(policy, funds=funds("25")).reasons == ("NO_DEPLOYABLE_CAPITAL",)


def test_the_research_profile_never_deploys() -> None:
    policy = POLICY.for_profile(get_profile("research"))
    assert decide(policy, funds=funds("500")).reasons == ("NO_DEPLOYABLE_CAPITAL",)


def test_a_grid_below_the_product_minimum_uses_fewer_lines_before_giving_up() -> None:
    wide = MarketRules(D("0.01"), D("0.0001"), D("0.01"), D("0.15"), None, D("1"))
    five = decide(rules=wide, levels=5)
    assert five.action == strat.GRID and five.plan is not None
    assert five.plan.levels < 5  # five lines split the cap too thin for the 0.15 minimum
    strict = MarketRules(D("0.01"), D("0.0001"), D("0.01"), D("0.2"), None, D("1"))
    none = decide(rules=strict, levels=5)
    assert none.action == strat.NO_TRADE and none.reasons == ("BELOW_BASE_MINIMUM",)


def test_funds_never_raise_the_cap_above_the_policy() -> None:
    cap = strat.capital_policy(POLICY, funds("100000")).cap
    assert cap == POLICY.max_deployment
    assert strat.capital_policy(POLICY).cap == POLICY.max_deployment


# ------------------------------------------------------------------ policy and profile rules
def test_a_policy_cannot_exceed_its_named_profile() -> None:
    data = POLICY.model_dump()
    with pytest.raises(ValidationError):
        PairPolicy(**{**data, "total_capital": D("100")})  # pilot is 50
    with pytest.raises(ValidationError):
        PairPolicy(**{**data, "capital_profile": "unknown"})
    ok = PairPolicy(
        **{
            **data,
            "capital_profile": "expanded",
            "total_capital": D("100"),
            "min_reserve": D("25"),
            "max_deployment": D("75"),
        }
    )
    assert ok.capital_profile == "expanded"
    with pytest.raises(ValidationError):
        PairPolicy(**{**ok.model_dump(), "max_deployment": D("80")})


def test_the_grid_builder_checks_against_the_named_profile() -> None:
    cap = CapitalPolicy(D("100"), D("25"), D("75"), profile="expanded")
    cap.validate()
    with pytest.raises(GridRejected) as err:
        build_grid(
            lower=D("96"),
            upper=D("104"),
            levels=4,
            rules=RULES,
            capital=CapitalPolicy(D("100"), D("25"), D("75")),  # the pilot profile is the default
            costs=strat.cost_model(POLICY, maker_fee=D("0.002")),
        )
    assert err.value.code == "CAPITAL_POLICY"
    with pytest.raises(GridRejected):
        CapitalPolicy(D("100"), D("25"), D("75"), profile="nope").validate()


def test_production_refuses_a_larger_capital_profile(
    prod_env: dict[str, str], config_copy: Any
) -> None:
    import yaml

    from app.config import ConfigError, load_settings

    path = config_copy / "pair-policy.yaml"
    data = yaml.safe_load(path.read_text())
    assert (
        load_settings({**prod_env, "TD_CONFIG_DIR": str(config_copy)}).environment == "production"
    )
    data.update(capital_profile="expanded", total_capital="100", min_reserve="25")
    data["max_deployment"] = "75"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="pilot"):
        load_settings({**prod_env, "TD_CONFIG_DIR": str(config_copy)})
    # the same file is fine outside production
    assert (
        load_settings(
            {"TD_ENVIRONMENT": "test", "TD_SECRET_KEY": "x" * 40, "TD_CONFIG_DIR": str(config_copy)}
        ).pair_policy.capital_profile
        == "expanded"
    )
