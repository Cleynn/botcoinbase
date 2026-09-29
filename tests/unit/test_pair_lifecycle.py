"""The lifecycle transition table: what can and cannot happen, independent of any database."""

from __future__ import annotations

from collections import defaultdict, deque

import pytest

from app.domain.enums import AuditEventType
from app.domain.pairs import (
    ACTIONS,
    UNREPRESENTABLE_STATES,
    ActorClass,
    Chain,
    PairAction,
    PairState,
    all_transitions,
    phrase_for,
    web_transition_allowed,
)

ROWS = all_transitions()


def edges(actor: ActorClass | None = None) -> dict[PairState, set[PairState]]:
    graph: dict[PairState, set[PairState]] = defaultdict(set)
    for row in ROWS:
        if actor is None or row.actor is actor:
            graph[row.from_state].add(row.to_state)
    return graph


def test_stored_states_are_exactly_the_eight_pair_states() -> None:
    assert {s.value for s in PairState} == {
        "PROPOSED",
        "VALIDATING",
        "RESEARCH_ONLY",
        "PAPER_ELIGIBLE",
        "PAPER_ACTIVE",
        "PAUSED",
        "DISABLED",
        "ARCHIVED",
    }


def test_live_states_are_not_representable_and_never_a_transition_endpoint() -> None:
    assert UNREPRESENTABLE_STATES == ("LIVE_ELIGIBLE", "LIVE_ACTIVE")
    for name in UNREPRESENTABLE_STATES:
        assert name not in {s.value for s in PairState}
    endpoints = {r.from_state.value for r in ROWS} | {r.to_state.value for r in ROWS}
    assert not endpoints & set(UNREPRESENTABLE_STATES)


def test_discovered_is_not_a_pair_state() -> None:
    assert "DISCOVERED" not in {s.value for s in PairState}


def test_archived_is_terminal() -> None:
    assert PairState.ARCHIVED not in edges()
    assert not any(r.from_state is PairState.ARCHIVED for r in ROWS)


def test_an_active_pair_can_only_be_paused() -> None:
    assert edges()[PairState.PAPER_ACTIVE] == {PairState.PAUSED}
    for action in (PairAction.DISABLE, PairAction.ARCHIVE):
        assert not web_transition_allowed(action, PairState.PAPER_ACTIVE)


@pytest.mark.parametrize("action", [PairAction.DISABLE, PairAction.ARCHIVE])
def test_disable_and_archive_reach_every_non_active_open_state(action: PairAction) -> None:
    spec = ACTIONS[action]
    assert PairState.PAPER_ACTIVE not in spec.from_states
    assert PairState.ARCHIVED not in spec.from_states
    assert {
        PairState.PROPOSED,
        PairState.VALIDATING,
        PairState.RESEARCH_ONLY,
        PairState.PAPER_ELIGIBLE,
        PairState.PAUSED,
    } <= spec.from_states


def test_activation_is_only_from_paper_eligible_or_paused_and_never_by_the_runner() -> None:
    into_active = [r for r in ROWS if r.to_state is PairState.PAPER_ACTIVE]
    assert {(r.from_state, r.actor) for r in into_active} == {
        (PairState.PAPER_ELIGIBLE, ActorClass.WEB),
        (PairState.PAUSED, ActorClass.WEB),
    }
    assert all(r.chain is Chain.FULL for r in into_active)


def test_validation_outcomes_are_written_only_by_the_runner() -> None:
    for target in (PairState.RESEARCH_ONLY, PairState.PAPER_ELIGIBLE):
        producers = {
            r.actor for r in ROWS if r.to_state is target and r.from_state is PairState.VALIDATING
        }
        assert producers == {ActorClass.HOST}
    assert not [
        r for r in ROWS if r.actor is ActorClass.WEB and r.to_state in (PairState.RESEARCH_ONLY,)
    ]
    # the web can never mark a pair PAPER_ELIGIBLE from VALIDATING
    assert not [
        r
        for r in ROWS
        if r.actor is ActorClass.WEB
        and r.from_state is PairState.VALIDATING
        and r.to_state is PairState.PAPER_ELIGIBLE
    ]


def test_full_chain_actions_have_an_exact_typed_phrase() -> None:
    for action, spec in ACTIONS.items():
        if spec.chain is Chain.FULL:
            assert spec.phrase and "{product_id}" in spec.phrase, action
        else:
            assert spec.phrase is None, action
    assert phrase_for(PairAction.DISABLE, "BTC-USDC") == "DISABLE PAIR BTC-USDC"
    assert phrase_for(PairAction.ARCHIVE, "ETH-USDC") == "ARCHIVE PAIR ETH-USDC"
    assert phrase_for(PairAction.ACTIVATE, "SOL-USDC") == "ACTIVATE PAPER PAIR SOL-USDC"
    assert phrase_for(PairAction.VALIDATE, "BTC-USDC") is None


def test_every_disabling_or_archiving_or_activating_action_needs_the_full_chain() -> None:
    for action in (
        PairAction.ACTIVATE,
        PairAction.RESUME,
        PairAction.DISABLE,
        PairAction.REENABLE,
        PairAction.ARCHIVE,
    ):
        assert ACTIONS[action].chain is Chain.FULL


def test_every_transition_names_a_catalogued_audit_event() -> None:
    catalogue = {e.value for e in AuditEventType}
    assert {r.event.value for r in ROWS} <= catalogue
    assert "pair.transition_denied" in catalogue and "pair.proposed" in catalogue


def test_every_state_is_reachable_from_proposed_and_nothing_reaches_a_live_state() -> None:
    graph = edges()
    seen = {PairState.PROPOSED}
    queue = deque([PairState.PROPOSED])
    while queue:
        for nxt in graph[queue.popleft()]:
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    assert seen == set(PairState)


def test_table_rows_are_unique_per_actor() -> None:
    keys = [(r.from_state, r.to_state, r.actor) for r in ROWS]
    assert len(keys) == len(set(keys))


def test_the_web_can_only_make_restrictive_or_confirmed_changes() -> None:
    for row in ROWS:
        if row.actor is ActorClass.WEB:
            assert row.chain in (Chain.CSRF, Chain.RESTRICTIVE, Chain.FULL)
        else:
            assert row.chain in (Chain.NONE, Chain.CSRF, Chain.RESTRICTIVE)
