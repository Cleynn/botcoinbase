"""Database-level enforcement of the pair lifecycle (migration 0002), exercised as each role.

These tests bypass the application services on purpose: the point is that the database refuses an
illegal change even if the application code were wrong or a role were compromised.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors

from app.domain.pairs import PairState, all_transitions
from tests.conftest import TestDb


def connect(db: TestDb, role: str, *, autocommit: bool = True) -> psycopg.Connection[Any]:
    s = db.settings_for(role)
    return psycopg.connect(
        host=s.host,
        port=s.port,
        dbname=s.name,
        user=role,
        password=s.password.get_secret_value() if s.password else None,
        autocommit=autocommit,
    )


Sql = Callable[..., list[dict[str, Any]]]


def test_the_database_seed_equals_the_code_transition_table(sql: Sql) -> None:
    rows = sql(
        "SELECT from_state, to_state, actor_class, transition_no, chain, event_code "
        "FROM allowed_transitions WHERE machine = 'pair'"
    )
    in_db = {
        (
            r["from_state"],
            r["to_state"],
            r["actor_class"],
            r["transition_no"],
            r["chain"],
            r["event_code"],
        )
        for r in rows
    }
    in_code = {
        (t.from_state.value, t.to_state.value, t.actor.value, t.no, t.chain.value, t.event.value)
        for t in all_transitions()
    }
    assert in_db == in_code and len(rows) == len(in_code)


def test_the_transition_table_is_immutable(sql: Sql) -> None:
    for statement in (
        "UPDATE allowed_transitions SET to_state = 'PAPER_ACTIVE'",
        "DELETE FROM allowed_transitions",
        "TRUNCATE allowed_transitions",
        "DELETE FROM allowed_transitions WHERE machine = 'pair'",
    ):
        with pytest.raises(errors.IntegrityConstraintViolation):
            sql(statement)


# ------------------------------------------------------------------ grants
@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO products (id, product_id, base_currency, quote_currency, product_type, venue, "
        "first_seen_at, last_seen_at) VALUES (gen_random_uuid(), 'X-USDC', 'X', 'USDC', 'SPOT', 'CBE', now(), now())",
        "UPDATE products SET discovered_rank = 1",
        "DELETE FROM products",
        "INSERT INTO product_metadata_current (product_uuid, snapshot_id, last_verified_at) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), now())",
        "UPDATE product_metadata_current SET last_verified_at = now()",
        "DELETE FROM product_metadata_snapshots",
        "DELETE FROM pair_validation_runs",
        "DELETE FROM allowed_transitions",
    ],
)
def test_the_web_role_cannot_write_products_metadata_or_evidence(
    db: TestDb, env: Any, statement: str
) -> None:
    env.discover()
    with connect(db, "td_app") as conn, pytest.raises(errors.InsufficientPrivilege):
        conn.execute(statement)


def test_the_web_role_cannot_insert_validation_runs(db: TestDb, env: Any) -> None:
    pair_id = env.eligible("BTC-USDC")
    run_id = env.get(pair_id).eligible_run_id
    with connect(db, "td_app") as conn:
        assert conn.execute("SELECT count(*) FROM pair_validation_runs").fetchone()[0] == 1  # type: ignore[index]
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO pair_validation_runs SELECT * FROM pair_validation_runs WHERE id = %s",
                (run_id,),
            )


def test_the_host_role_cannot_delete_or_touch_users_or_sessions(db: TestDb, env: Any) -> None:
    with connect(db, "td_ctl") as conn:
        for statement in (
            "DELETE FROM pairs",
            "DELETE FROM products",
            "UPDATE users SET role = 'ADMIN'",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(statement)


def test_only_the_lifecycle_columns_of_a_pair_can_be_updated(db: TestDb, env: Any) -> None:
    env.discover()
    pair_id = env.propose("BTC-USDC")
    for role in ("td_app", "td_ctl"):
        with connect(db, role) as conn:
            for column, value in (
                ("data_basis", "'OWN_BOOK'"),
                ("order_product_id", "'ETH-USDC'"),
                ("product_uuid", "gen_random_uuid()"),
                ("proposed_via", "'HOST'"),
                ("proposed_at", "now()"),
                ("id", "gen_random_uuid()"),
            ):
                with pytest.raises(errors.InsufficientPrivilege):
                    conn.execute(f"UPDATE pairs SET {column} = {value} WHERE id = %s", (pair_id,))  # noqa: S608


# ------------------------------------------------------------------ transition enforcement
def test_a_pair_cannot_skip_states(db: TestDb, env: Any) -> None:
    env.discover()
    pair_id = env.propose("BTC-USDC")
    with connect(db, "td_app") as conn:
        for target in ("PAPER_ACTIVE", "PAPER_ELIGIBLE", "RESEARCH_ONLY", "PAUSED", "ARCHIVED_X"):
            with pytest.raises((errors.IntegrityConstraintViolation, errors.CheckViolation)):
                conn.execute(
                    "UPDATE pairs SET state = %s, version = version + 1 WHERE id = %s",
                    (target, pair_id),
                )
    assert env.state(pair_id) is PairState.PROPOSED


def test_every_update_must_bump_the_version_by_exactly_one_and_change_state(
    db: TestDb, env: Any
) -> None:
    env.discover()
    pair_id = env.propose("BTC-USDC")
    with connect(db, "td_app") as conn:
        for version in ("version", "version + 2", "version - 1"):
            with pytest.raises((errors.IntegrityConstraintViolation, errors.CheckViolation)):
                conn.execute(
                    f"UPDATE pairs SET state = 'VALIDATING', version = {version} WHERE id = %s",  # noqa: S608
                    (pair_id,),
                )
        with pytest.raises(errors.IntegrityConstraintViolation):  # same state
            conn.execute("UPDATE pairs SET version = version + 1 WHERE id = %s", (pair_id,))


def test_the_web_role_cannot_perform_runner_transitions(db: TestDb, env: Any) -> None:
    env.discover()
    pair_id = env.propose("BTC-USDC")
    assert env.queue(pair_id).kind == "ok"  # VALIDATING
    with connect(db, "td_app") as conn:
        for target in ("RESEARCH_ONLY", "PAPER_ELIGIBLE"):
            with pytest.raises(errors.IntegrityConstraintViolation):
                conn.execute(
                    "UPDATE pairs SET state = %s, version = version + 1 WHERE id = %s",
                    (target, pair_id),
                )
    assert env.state(pair_id) is PairState.VALIDATING


def test_the_host_role_cannot_perform_confirmed_admin_transitions(db: TestDb, env: Any) -> None:
    pair_id = env.eligible("BTC-USDC")
    with connect(db, "td_ctl") as conn:
        for target in ("PAPER_ACTIVE", "DISABLED", "ARCHIVED"):
            with pytest.raises(errors.IntegrityConstraintViolation):
                conn.execute(
                    "UPDATE pairs SET state = %s, version = version + 1 WHERE id = %s",
                    (target, pair_id),
                )
    assert env.state(pair_id) is PairState.PAPER_ELIGIBLE


def test_an_active_pair_cannot_be_disabled_or_archived_even_by_raw_sql(
    db: TestDb, env: Any
) -> None:
    pair_id = env.make("BTC-USDC", PairState.PAPER_ACTIVE)
    with connect(db, "td_app") as conn:
        for target in ("DISABLED", "ARCHIVED", "PAPER_ELIGIBLE", "VALIDATING", "PROPOSED"):
            with pytest.raises(errors.IntegrityConstraintViolation):
                conn.execute(
                    "UPDATE pairs SET state = %s, version = version + 1 WHERE id = %s",
                    (target, pair_id),
                )
    assert env.state(pair_id) is PairState.PAPER_ACTIVE


def test_an_archived_pair_can_never_change_again(db: TestDb, env: Any, sql: Sql) -> None:
    pair_id = env.make("BTC-USDC", PairState.ARCHIVED)
    for role in ("td_app", "td_ctl"):
        with connect(db, role) as conn:
            for target in [s.value for s in PairState if s is not PairState.ARCHIVED]:
                with pytest.raises(errors.IntegrityConstraintViolation):
                    conn.execute(
                        "UPDATE pairs SET state = %s, version = version + 1 WHERE id = %s",
                        (target, pair_id),
                    )
    assert env.state(pair_id) is PairState.ARCHIVED


def test_a_pair_starts_only_as_proposed_version_one_and_only_as_its_own_role(
    db: TestDb, env: Any
) -> None:
    env.discover()
    product = env.product_uuid("ETH-USDC")

    def insert(conn: psycopg.Connection[Any], **overrides: Any) -> None:
        values = {
            "state": "PROPOSED",
            "version": 1,
            "ever_active": False,
            "via": "WEB",
            "by": env.admin.user.id,
        }
        values.update(overrides)
        conn.execute(
            "INSERT INTO pairs (id, product_uuid, state, version, order_product_id, data_basis, "
            "proposed_via, proposed_by, proposed_at, state_changed_at, ever_active) "
            "VALUES (%s, %s, %s, %s, 'ETH-USDC', 'UNKNOWN', %s, %s, now(), now(), %s)",
            (
                uuid4(),
                product,
                values["state"],
                values["version"],
                values["via"],
                values["by"],
                values["ever_active"],
            ),
        )

    with connect(db, "td_app") as conn:
        for bad in (
            {"state": "PAPER_ACTIVE"},
            {"state": "VALIDATING"},
            {"version": 2},
            {"ever_active": True},
            {"via": "HOST"},
        ):
            with pytest.raises((errors.IntegrityConstraintViolation, errors.CheckViolation)):
                insert(conn, **bad)


def test_a_pair_change_without_a_history_row_cannot_commit(db: TestDb, env: Any) -> None:
    env.discover()
    pair_id = env.propose("BTC-USDC")
    with connect(db, "td_app", autocommit=False) as conn:
        conn.execute(
            "UPDATE pairs SET state = 'VALIDATING', version = version + 1 WHERE id = %s", (pair_id,)
        )
        with pytest.raises(errors.IntegrityConstraintViolation):
            conn.commit()
    assert env.state(pair_id) is PairState.PROPOSED


def test_history_must_match_the_pairs_real_current_state_and_actor(db: TestDb, env: Any) -> None:
    env.discover()
    pair_id = env.propose("BTC-USDC")
    with connect(db, "td_app") as conn:
        for after, version, actor in (
            ("VALIDATING", 2, "WEB"),  # the pair is still PROPOSED
            ("PROPOSED", 1, "HOST"),  # the row was written by td_app
        ):
            with pytest.raises(errors.IntegrityConstraintViolation):
                conn.execute(
                    "INSERT INTO pair_state_history (pair_id, version_after, state_before, state_after, "
                    "actor_class, transition_no, occurred_at) VALUES (%s, %s, NULL, %s, %s, 2, now())",
                    (pair_id, version, after, actor),
                )


def test_history_runs_and_snapshots_are_append_only_for_every_role(sql: Sql, env: Any) -> None:
    pair_id = env.eligible("BTC-USDC")
    for statement in (
        "UPDATE pair_state_history SET reason_code = 'x'",
        "DELETE FROM pair_state_history",
        "TRUNCATE pair_state_history",
        "UPDATE pair_validation_runs SET outcome = 'PASS'",
        "DELETE FROM pair_validation_runs",
        "TRUNCATE pair_validation_runs",
        "UPDATE product_metadata_snapshots SET status = 'x'",
        "DELETE FROM product_metadata_snapshots",
    ):
        # TRUNCATE of a referenced table is refused by PostgreSQL itself before our trigger runs.
        with pytest.raises((errors.IntegrityConstraintViolation, errors.FeatureNotSupported)):
            sql(statement)
    assert env.state(pair_id) is PairState.PAPER_ELIGIBLE
    assert sql("SELECT count(*) AS n FROM pair_validation_runs")[0]["n"] == 1


def test_pairs_are_never_deleted_or_truncated_even_by_the_owner(sql: Sql, env: Any) -> None:
    env.discover()
    env.propose("BTC-USDC")
    for statement in ("DELETE FROM pairs", "TRUNCATE pairs"):
        with pytest.raises((errors.IntegrityConstraintViolation, errors.FeatureNotSupported)):
            sql(statement)
    assert sql("SELECT count(*) AS n FROM pairs")[0]["n"] == 1


def test_a_product_that_has_a_pair_cannot_be_deleted(sql: Sql, env: Any) -> None:
    env.discover()
    env.propose("BTC-USDC")
    with pytest.raises(errors.ForeignKeyViolation):
        sql("DELETE FROM products WHERE product_id = 'BTC-USDC'")


# ------------------------------------------------------------------ single active pair
def test_the_database_allows_only_one_active_pair_even_for_raw_updates(
    db: TestDb, env: Any
) -> None:
    first = env.make("BTC-USDC", PairState.PAPER_ACTIVE)
    second = env.eligible("ETH-USDC")
    run_id = env.get(second).eligible_run_id
    with connect(db, "td_app", autocommit=False) as conn:
        with pytest.raises(errors.UniqueViolation):
            conn.execute(
                "UPDATE pairs SET state = 'PAPER_ACTIVE', version = version + 1, ever_active = true "
                "WHERE id = %s",
                (second,),
            )
        conn.rollback()
    assert run_id is not None
    assert (
        env.state(first) is PairState.PAPER_ACTIVE and env.state(second) is PairState.PAPER_ELIGIBLE
    )


def test_the_active_pair_limit_is_a_database_trigger_and_states_cannot_widen_silently(
    sql: Sql,
) -> None:
    # DEC-026: the one-active-pair index became a trigger that reads the configured number of pairs
    assert not sql("SELECT 1 FROM pg_indexes WHERE indexname = 'pairs_one_active'")
    assert sql("SELECT 1 FROM pg_trigger WHERE tgname = 'pairs_max_active_trigger'")
    check = sql(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
        "WHERE conrelid = 'pairs'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%state = ANY%'"
    )
    text = " ".join(r["d"] for r in check)
    assert "LIVE" not in text and "PAPER_ACTIVE" in text


def test_live_states_cannot_be_stored(sql: Sql, env: Any) -> None:
    env.discover()
    pair_id = env.propose("BTC-USDC")
    sql("ALTER TABLE pairs DISABLE TRIGGER USER")  # even with the lifecycle trigger out of the way
    try:
        for state in ("LIVE_ELIGIBLE", "LIVE_ACTIVE", "DISCOVERED"):
            with pytest.raises(errors.CheckViolation):
                sql("UPDATE pairs SET state = %s WHERE id = %s", (state, pair_id))
    finally:
        sql("ALTER TABLE pairs ENABLE TRIGGER USER")


def test_activation_needs_a_passing_run_recorded_by_the_runner(
    db: TestDb, env: Any, sql: Sql
) -> None:
    pair_id = env.eligible("BTC-USDC")
    other = env.eligible("ETH-USDC")
    other_run = env.get(other).eligible_run_id
    with connect(db, "td_app") as conn:
        with pytest.raises(errors.IntegrityConstraintViolation):  # the web role cannot set evidence
            conn.execute(
                "UPDATE pairs SET eligible_run_id = %s WHERE id = %s", (other_run, pair_id)
            )
        with pytest.raises(errors.IntegrityConstraintViolation):  # nor use another pair's run
            conn.execute(
                "UPDATE pairs SET state = 'PAPER_ACTIVE', version = version + 1, ever_active = true, "
                "eligible_run_id = %s WHERE id = %s",
                (other_run, pair_id),
            )
    sql("ALTER TABLE pair_validation_runs DISABLE TRIGGER USER")
    try:
        sql("UPDATE pair_validation_runs SET outcome = 'FAIL' WHERE pair_id = %s", (pair_id,))
    finally:
        sql("ALTER TABLE pair_validation_runs ENABLE TRIGGER USER")
    with connect(db, "td_app") as conn, pytest.raises(errors.IntegrityConstraintViolation):
        conn.execute(
            "UPDATE pairs SET state = 'PAPER_ACTIVE', version = version + 1, ever_active = true "
            "WHERE id = %s",
            (pair_id,),
        )
    assert env.state(pair_id) is PairState.PAPER_ELIGIBLE


def test_ever_active_cannot_be_cleared(db: TestDb, env: Any) -> None:
    pair_id = env.make("BTC-USDC", PairState.PAUSED)
    assert env.get(pair_id).ever_active is True
    with connect(db, "td_app") as conn, pytest.raises(errors.IntegrityConstraintViolation):
        conn.execute(
            "UPDATE pairs SET state = 'PAPER_ELIGIBLE', version = version + 1, ever_active = false "
            "WHERE id = %s",
            (pair_id,),
        )


# ------------------------------------------------------------------ products
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("product_id", "'btc-usdc'"),
        ("product_id", "'BTC-USD'"),
        ("product_id", "'" + "A" * 30 + "-USDC'"),
        ("quote_currency", "'USD'"),
        ("product_type", "'FUTURE'"),
        ("venue", "'INTX'"),
        ("discovered_rank", "501"),
        ("discovered_rank", "0"),
    ],
)
def test_product_constraints(sql: Sql, column: str, value: str) -> None:
    values = {
        "product_id": "'BTC-USDC'",
        "base_currency": "'BTC'",
        "quote_currency": "'USDC'",
        "product_type": "'SPOT'",
        "venue": "'CBE'",
        "discovered_rank": "1",
    }
    values[column] = value
    with pytest.raises((errors.CheckViolation, errors.IntegrityConstraintViolation)):
        sql(
            "INSERT INTO products (id, product_id, base_currency, quote_currency, product_type, venue, "
            "discovered_rank, first_seen_at, last_seen_at) VALUES (gen_random_uuid(), "
            f"{values['product_id']}, {values['base_currency']}, {values['quote_currency']}, "  # noqa: S608
            f"{values['product_type']}, {values['venue']}, {values['discovered_rank']}, now(), now())"
        )


def test_metadata_snapshots_hold_static_rules_only(sql: Sql) -> None:
    columns = {
        r["column_name"]
        for r in sql(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'product_metadata_snapshots'"
        )
    }
    assert not {
        c for c in columns if any(w in c for w in ("price", "volume", "spread", "bid", "ask"))
    } - {"price_increment"}
    assert {
        "base_increment",
        "quote_increment",
        "price_increment",
        "base_min_size",
        "alias",
    } <= columns


def test_one_open_pair_per_product_at_the_database_level(db: TestDb, env: Any) -> None:
    env.discover()
    env.propose("BTC-USDC")
    with connect(db, "td_app") as conn, pytest.raises(errors.UniqueViolation):
        conn.execute(
            "INSERT INTO pairs (id, product_uuid, state, version, order_product_id, data_basis, "
            "proposed_via, proposed_by, proposed_at, state_changed_at) "
            "VALUES (%s, %s, 'PROPOSED', 1, 'BTC-USDC', 'UNKNOWN', 'WEB', %s, now(), now())",
            (uuid4(), env.product_uuid("BTC-USDC"), env.admin.user.id),
        )


def test_audit_target_index_exists(sql: Sql) -> None:
    assert sql("SELECT 1 AS x FROM pg_indexes WHERE indexname = 'audit_events_target_idx'")
