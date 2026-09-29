"""Fixtures for the Phase 5 market, backtest and paper integration tests (synthetic data only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.market.ingest import Importer
from app.storage.database import Storage
from tests.conftest import FakeClock
from tests.market_env import Source, ingest_settings, install


@dataclass
class Market:
    settings: Settings
    storage: Storage  # td_ctl
    clock: FakeClock
    coinbase: Any
    source: Source
    data_dir: Path
    sleeps: list[float] = field(default_factory=list)

    def importer(self) -> Importer:
        return Importer(
            storage=self.storage,
            clock=self.clock,
            settings=self.settings,
            client=self.coinbase.client(),
            sleep=self.sleeps.append,
        )

    def imported(self, product: str = "BTC-USDC") -> Any:
        """Discover the pair (public metadata) and commit a full import."""
        from app.pairs.runner import PairRunner

        PairRunner(
            storage=self.storage,
            clock=self.clock,
            settings=self.settings,
            client=self.coinbase.client(),
        ).discover()
        result = self.importer().run(product, commit=True)
        assert result.status == "COMPLETE", result
        return result


@pytest.fixture
def mkt(
    settings: Settings,
    ctl_storage: Storage,
    clock: FakeClock,
    coinbase: Any,
    tmp_path: Path,
) -> Market:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return Market(
        settings=ingest_settings(settings, str(data_dir)),
        storage=ctl_storage,
        clock=clock,
        coinbase=coinbase,
        source=install(coinbase),
        data_dir=data_dir,
    )


# ---------------------------------------------------------------- Phase 6: review packages
@pytest.fixture
def review(
    settings: Settings,
    storage: Storage,
    ctl_storage: Storage,
    clock: FakeClock,
    admin: Any,
    tmp_path: Path,
) -> Any:
    from tests.integration.review_env import build_review_env

    directory = tmp_path / "review"
    return build_review_env(
        storage=storage,
        ctl_storage=ctl_storage,
        clock=clock,
        settings=settings,
        admin=admin,
        review_dir=directory,
    )


# ---------------------------------------------------------------- Phase 7: proposals
@pytest.fixture
def prop(
    settings: Settings,
    storage: Storage,
    ctl_storage: Storage,
    clock: FakeClock,
    admin: Any,
    review: Any,
    tmp_path: Path,
) -> Any:
    """A proposal environment whose review package is real: enabled, requested, built."""
    from tests.integration.proposal_env import build_proposal_env
    from tests.integration.test_review_service import enabled, ready

    enabled(review)
    pid = ready(review)
    with ctl_storage.tx() as repos:
        package = repos.review.package(pid)
    return build_proposal_env(
        storage=storage,
        ctl_storage=ctl_storage,
        clock=clock,
        settings=settings,
        admin=admin,
        directory=tmp_path / "proposals",
        review=review,
        package=package,
    )


# ---------------------------------------------------------------- Phase 8: safety machinery
@pytest.fixture
def safe(
    mkt: Market,
    storage: Storage,
    ctl_storage: Storage,
    clock: FakeClock,
    coinbase: Any,
    admin: Any,
) -> Any:
    """An active PAPER pair with SYNTHETIC candles plus the scripted FAKE exchange."""
    from app.domain.pairs import PairAction
    from app.paper.gate import PaperRuntimeGate
    from tests.integration.safety_env import build_safety_env
    from tests.pair_env import build_env

    settings = mkt.settings.model_copy(update={"mode": "PAPER"})
    mkt.settings = settings
    env = build_env(
        storage=storage,
        ctl_storage=ctl_storage,
        clock=clock,
        settings=settings,
        coinbase=coinbase,
        admin=admin,
        gate=PaperRuntimeGate("PAPER"),
    )
    coinbase.range_source = None
    pair_id = env.eligible("BTC-USDC")
    assert env.act(pair_id, PairAction.ACTIVATE).kind == "ok"
    coinbase.range_source = mkt.source
    mkt.imported()
    return build_safety_env(
        settings=settings,
        storage=storage,
        ctl_storage=ctl_storage,
        clock=clock,
        admin=admin,
        pair_id=pair_id,
    )
