"""Policy triage and risk assessment: what a proposal must never ask for, and what it may."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from app.proposals import policy, risk
from app.proposals.schema import Parsed

PKG = "123e4567-e89b-12d3-a456-426614174000"
SHA = "ab" * 32
BASE = {
    "summary": "Require a wider minimum band before the grid is built.",
    "suggested_change": "Raise the minimum band ratio from 0.02 to 0.03 so cells clear the fees.",
    "expected_benefit": "Fewer fee-negative cycles in the stress scenario.",
    "risk_tradeoffs": "Fewer trading windows and lower activity overall.",
    "required_validation": "Walk-forward backtest on a frozen snapshot, then a paper run.",
    "rollback_plan": "Revert the parameter through a reviewed release and rerun the backtest.",
}


def make(
    *,
    category: str = "strategy",
    evidence: tuple[str, ...] = ("data/backtest_runs.jsonl#L3", "report:" + PKG),
    assumptions: tuple[str, ...] = ("Fees stay at the attested rate.",),
    no_trade: bool = True,
    no_guarantee: bool = True,
    **texts: str,
) -> Parsed:
    return Parsed(
        "prop-001",
        PKG,
        SHA,
        category,
        evidence,
        assumptions,
        {**BASE, **texts},
        no_trade,
        no_guarantee,
    )


def rules(**texts: str) -> set[str]:
    return {f.rule for f in policy.evaluate(make(**texts))}


def rules_in(field: str, text: str) -> set[str]:
    return rules(**{field: text})


FIELDS = (
    "summary",
    "suggested_change",
    "expected_benefit",
    "risk_tradeoffs",
    "required_validation",
    "rollback_plan",
)

# rule -> phrases that must each trigger it
BLOCKED: dict[str, list[str]] = {
    "RISK_BYPASS": [
        "Temporarily disable the reserve check so more capital trades.",
        "Bypass the risk engine when volatility is low.",
        "Ignore the drawdown limit during the first week.",
        "Remove the deployment cap for this pair.",
        "Relax the circuit breaker thresholds substantially.",
        "Skip reconciliation on restart to save time.",
        "Turn off the kill switch during tests.",
        "Loosen the protected reserve to ten USDC.",
        "Override the live gate for a pilot.",
        "Trade without a reserve floor.",
        "There should be no risk limits in paper mode.",
        "Exceed the cap when the signal is strong.",
        "Risk checks should be bypassed here.",
        "Weaken safeguards around the drawdown stop.",
        "Circumvent the limit on deployed capital.",
    ],
    "SECRET_CHANGE": [
        "Rotate the API key and paste the new one into the settings.",
        "Store the exchange credentials in the config file.",
        "Print the session token in the logs for debugging.",
        "Add the private key to the environment file.",
        "Share the password with the analysis tool.",
        "Update the signing key used for cookies.",
        "Export the secrets to a spreadsheet.",
        "Set the jwt secret to a shorter value.",
        "Send the credential to the reviewer by e-mail.",
        "api_key = abc123 should be committed",
        "-----BEGIN PRIVATE KEY----- MIIE",
    ],
    "SECURITY_CONTROL_CHANGE": [
        "Disable CSRF checks for the import form.",
        "Remove the reauthentication step to speed up review.",
        "Weaken the rate limit on login attempts.",
        "Turn off the audit log for proposals.",
        "Skip validation of the uploaded file.",
        "Bypass the scanner for trusted packages.",
        "Loosen the firewall around the batch container.",
        "Disable TLS between the services.",
        "Drop the checksum verification on download.",
        "Whitelist the reviewer's session for downloads.",
    ],
    "API_ACCESS_CHANGE": [
        "Grant the bot private API access to the exchange.",
        "Use an authenticated endpoint to read balances.",
        "Enable outbound network access for the batch container.",
        "Open a new port for the exchange feed.",
        "Add another exchange as a data source.",
        "Connect to the websocket feed for faster candles.",
        "Request trading permissions on the key.",
        "Allow egress to an additional domain.",
        "Add withdrawal permission for treasury moves.",
        "Change the API permissions on the account.",
        "Call the private REST endpoints for order status.",
    ],
    "LIVE_ACTIVATION": [
        "Then go live with real money.",
        "Activate live trading once the backtest passes.",
        "Enable live mode for the BTC pair.",
        "Promote the pair to LIVE_ELIGIBLE.",
        "Start live trading with a small budget.",
        "Switch to live execution next week.",
        "Move to production trading after paper validation.",
        "Set the trading mode to live.",
        "Unlock the live gate for the pilot.",
        "Launch the live pilot on Monday.",
        "live_active after seven days",
    ],
    "PAIR_STATE_CHANGE": [
        "Activate the ETH-USDC pair immediately.",
        "Disable the SOL pair when it underperforms.",
        "Archive the BTC pair after the test.",
        "Delete the watchlist entries for illiquid products.",
        "Resume the paused pair automatically each morning.",
        "Force the pair into PAPER_ACTIVE.",
        "Pause every pair on Sundays.",
        "Re-enable the disabled pair.",
        "Remove the pair from the watchlist.",
        "Set the pair state to research_only.",
        "Promote the candidate pair to paper_active.",
    ],
    "CAPITAL_INCREASE": [
        "Double the deployment cap when profits rise.",
        "Increase the total capital to 100 USDC.",
        "Reinvest profits to grow the position size.",
        "Automatically compound gains into the budget.",
        "Raise exposure by using leverage.",
        "Top up the deposit from profits.",
        "Scale up the allocation as equity grows.",
        "Enable the capital growth policy.",
        "Increase the order size above the current notional.",
        "Borrow funds to enlarge the grid.",
        "Expand the bankroll after each winning week.",
    ],
    "AUTOMATED_ORDER": [
        "Place market orders automatically on each breakout.",
        "Cancel all orders directly when price gaps.",
        "Submit orders immediately after the signal.",
        "Create_order calls should be generated by the analysis tool.",
        "Enable automated trading for the strategy.",
        "Sell all inventory at market when the band breaks.",
        "Flatten the position at the close.",
        "Use market orders for entries.",
        "Send buy orders as soon as the candle closes.",
        "Replace the resting orders on every tick.",
        "Amend orders to chase the price.",
    ],
    "AUTO_APPLICATION": [
        "Apply this change automatically without review.",
        "The system should update its own configuration automatically.",
        "Deploy the new parameters unattended.",
        "Merge the change automatically after tests pass.",
        "Edit the config file directly at runtime.",
        "Hot patch the running bot with the new thresholds.",
        "Roll out the change without approval.",
        "Self update the strategy each night.",
        "There is no human review needed for this change.",
        "Rewrite the database schema automatically.",
        "Auto apply the new grid parameters.",
    ],
    "PROFIT_GUARANTEE": [
        "This guarantees profit every week.",
        "The strategy is risk free.",
        "It will make a profit in any market.",
        "You cannot lose with this grid.",
        "Guaranteed returns of two percent a month.",
        "A sure profit on every cycle.",
        "It never loses money.",
        "Assured gains after fees.",
        "100% profit on the backtest, so it will stay that way.",
        "No risk at all is involved.",
    ],
    "MARKUP_OR_TEMPLATE": [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "Use <b>bold</b> emphasis.",
        "Insert {{ config.secret }} here.",
        "Render {% include 'x' %} in the page.",
        "Value ${HOME} expands.",
        "&#60;script&#62;",
        "&lt;b&gt;",
        "<%= system('id') %>",
        "$(whoami)",
        "<!-- hidden -->",
    ],
    "EXECUTABLE_CONTENT": [
        "Run sudo systemctl restart bot.",
        "Execute rm -rf /var/lib/data.",
        "curl http then pipe to bash",
        "SELECT * FROM proposals WHERE 1=1",
        "DROP TABLE users",
        "insert into audit_events values (1)",
        "UPDATE pairs SET state = 'x'",
        "```python\nprint(1)\n```",
        "def apply(): return 1",
        "import os; os.system('id')",
        "eval(input())",
        "docker exec -it batch sh",
        "pip install evil-package",
        "git push origin main",
        "#!/bin/sh\necho hi",
        "a && b",
        "run `id` now",
    ],
    "EXTERNAL_REFERENCE": [
        "See https://example.com/notes for details.",
        "Fetch ftp://host/file first.",
        "Read /etc/passwd for hints.",
        "Open ~/.ssh/id_rsa",
        "Look at ../../secrets.txt",
        "Check C:\\Windows\\System32",
        "Set TD_SECRET_KEY=abc123 in the environment.",
        'The config is {"key": "value"}.',
        "key: value\nother: value2",
        "www.example.com has the data.",
        "data:text/html;base64,PHNjcmlwdD4=",
    ],
}


@pytest.mark.parametrize(("rule", "phrase"), [(r, p) for r, ps in BLOCKED.items() for p in ps])
def test_forbidden_requests_are_blocked_in_every_text_field(rule: str, phrase: str) -> None:
    for field in FIELDS:
        text = phrase if len(phrase) >= 20 else phrase + " " * (20 - len(phrase)) + "."
        assert rule in rules_in(field, text), (field, phrase)


@pytest.mark.parametrize("rule", sorted(BLOCKED))
def test_every_rule_has_a_fixed_description(rule: str) -> None:
    assert rule in policy.RULE_TEXT and policy.RULE_TEXT[rule]


def test_every_rule_in_the_engine_is_covered_by_the_corpus() -> None:
    engine = set(policy.NORMALISED_RULES) | set(policy.RAW_RULES) | {"PROFIT_GUARANTEE"}
    assert engine <= set(BLOCKED)


def test_assumptions_are_scanned_too() -> None:
    found = policy.evaluate(make(assumptions=("Disable the reserve check for the pilot.",)))
    assert [(f.rule, f.field) for f in found] == [("RISK_BYPASS", "assumptions")]


# ------------------------------------------------------------------ evasion attempts
EVASIONS = [
    ("upper", "DISABLE THE RESERVE CHECK NOW"),
    ("mixed", "DiSaBlE tHe ReSeRvE cHeCk now please"),
    ("spaces", "disable      the\n\n reserve\t check now"),
    ("punctuation", "disable... the (reserve) -- check!! now"),
    ("hyphen", "re-enable the disabled pair"),
    ("fullwidth", "ｄｉｓａｂｌｅ ｔｈｅ ｒｅｓｅｒｖｅ ｃｈｅｃｋ"),
    ("cyrillic-o", "dis\u0430ble the reserve check"),  # Cyrillic 'а'
    ("cyrillic-e", "disabl\u0435 the r\u0435serve check"),
    ("greek", "disable the reserve ch\u03b5ck and the limit"),
    ("words-between", "disable, only for a short while in tests, the reserve"),
    ("reverse-order", "the reserve should be disabled"),
    ("passive", "risk limits are relaxed"),
]


@pytest.mark.parametrize(("label", "phrase"), EVASIONS)
def test_simple_spelling_tricks_do_not_evade_the_triage(label: str, phrase: str) -> None:
    found = rules(suggested_change=phrase + " so that more capital trades over the week.")
    assert found & {"RISK_BYPASS", "PAIR_STATE_CHANGE"}, label


def test_a_negated_mention_is_still_blocked_by_design() -> None:
    found = rules(
        rollback_plan="Never bypass the reserve; revert the parameter via a reviewed release."
    )
    assert "RISK_BYPASS" in found


def test_a_forbidden_request_hidden_after_a_disclaimer_is_still_found() -> None:
    text = "There is no guarantee of profit, but it does guarantee returns of five percent."
    assert "PROFIT_GUARANTEE" in rules(risk_tradeoffs=text)


def test_an_instruction_hidden_in_a_long_benign_field_is_found() -> None:
    filler = "The change narrows the band and keeps the grid inside the range. " * 20
    assert "RISK_BYPASS" in rules(
        expected_benefit=filler + "Also disable the reserve check. " + filler[:200]
    )


# ------------------------------------------------------------------ what is allowed
BENIGN = [
    "Require a wider minimum band before the grid is built.",
    "Raise the minimum band ratio from 0.02 to 0.03 so cells clear the fees.",
    "Reduce the assumed slippage to 4 bps only if the paper fills support it.",
    "Add a data-quality check that flags gaps longer than one hour.",
    "Recommend that the system remains NO_TRADE while the trend filter reports a trend.",
    "Propose ETH-USDC as a research candidate for the pair validation queue.",
    "Add an alert when the newest closed candle is older than fifteen minutes.",
    "Lower the drawdown stop from ten percent to eight percent.",
    "Reduce exposure by using three levels instead of five.",
    "Shorten the maximum order age from seven days to three days.",
    "Update the documentation for the fee model and its assumptions.",
    "Improve the walk-forward report with an out-of-sample table per fold.",
    "The stress scenario should use a higher maker fee before any pair is considered.",
    "Backtest results are historical and there is no guarantee of profit.",
    "Results are not guaranteed and may be negative in trending markets.",
    "No profit is guaranteed by this change.",
    "This change does not guarantee any return.",
    "Keep the reserve and cap exactly as they are.",
    "Compare fills against the volume participation assumption.",
    "Require two consecutive closes beyond the band before stopping the grid.",
    "Add a metric for the number of touched but unfilled orders.",
    "Prefer a longer EMA to reduce false trend signals in ranging markets.",
]


@pytest.mark.parametrize("sentence", BENIGN)
def test_ordinary_research_suggestions_pass_the_triage(sentence: str) -> None:
    padded = sentence if len(sentence) >= 20 else sentence + " Details follow later."
    for field in ("suggested_change", "expected_benefit"):
        assert rules_in(field, padded) == set(), (field, sentence)


def test_the_base_proposal_is_clean_and_medium_risk() -> None:
    parsed = make()
    assert policy.evaluate(parsed) == []
    assessed = risk.assess(parsed, blocked=False)
    assert assessed["level"] in {"MEDIUM", "HIGH"} and assessed["advisory_only"] is True


# ------------------------------------------------------------------ declared booleans
def test_a_proposal_must_declare_no_trade_and_no_guarantee() -> None:
    found = {(f.rule, f.field) for f in policy.evaluate(make(no_trade=False, no_guarantee=False))}
    assert found == {
        ("NO_TRADE_NOT_PRESERVED", "should_remain_no_trade_until_validated"),
        ("PROFIT_DECLARATION_MISSING", "no_profit_guarantee"),
    }


# ------------------------------------------------------------------ findings shape
def test_findings_are_stable_bounded_and_text_safe() -> None:
    found = policy.evaluate(
        make(suggested_change='<script>alert("x")</script> disable the reserve check now')
    )
    assert found == sorted(found, key=lambda f: (f.rule, f.field))
    for finding in found:
        assert len(finding.excerpt) <= 80 and finding.severity == "BLOCK"
        assert not any(c in finding.excerpt for c in "<>&\"'")  # markup characters are stripped
        assert set(finding.as_json()) == {"rule", "field", "excerpt", "severity"}


def test_one_finding_per_rule_and_field() -> None:
    text = "Disable the reserve. Bypass the risk engine. Ignore the drawdown limit. Remove the cap."
    found = [f for f in policy.evaluate(make(suggested_change=text)) if f.rule == "RISK_BYPASS"]
    assert len(found) == 1


# ------------------------------------------------------------------ robustness
def test_triage_is_fast_on_adversarial_text() -> None:
    nasty = [
        "disable " * 300,
        "a " * 1000,
        ("bypass the " * 150) + "reserve",
        "x" * 2000,
        "<" * 2000,
        "key: v\n" * 200,
        "guarantee " * 200,
        "the " * 500 + "cap",
    ]
    started = time.perf_counter()
    for text in nasty:
        policy.evaluate(make(suggested_change=text[:2000]))
    assert time.perf_counter() - started < 2.0


def test_normalise_maps_lookalikes_and_strips_punctuation() -> None:
    assert policy.normalise("D\u0456sabl\u0435 THE, reserve!!") == "disable the reserve"
    assert policy.normalise("ｒｅｓｅｒｖｅ") == "reserve"
    assert policy.normalise("  a\t\nb  ") == "a b"


# ------------------------------------------------------------------ risk assessment
def test_risk_levels_follow_the_documented_factors() -> None:
    low = make(
        category="documentation",
        evidence=("manifest.json", "efficiency_summary.json"),
        summary="Update the wording of the limitations section for clarity.",
        suggested_change="Clarify the limitations text in the documentation for readers.",
        expected_benefit="Clearer documentation for reviewers of packages.",
        risk_tradeoffs="Negligible: text only, no behaviour changes at all.",
        required_validation="A second person reads the updated wording carefully.",
        rollback_plan="Revert the documentation commit if the wording is worse.",
    )
    assert risk.assess(low, blocked=False)["level"] == "LOW"
    high = make(
        category="risk",
        evidence=("manifest.json",),
        suggested_change=(
            "Change the band, fee, spread, slippage, level, breakout and order parameters."
        ),
        required_validation="Backtest.",
        rollback_plan="Revert.",
    )
    result = risk.assess(high, blocked=False)
    assert result["level"] == "HIGH"
    assert {
        "CATEGORY_HIGH_IMPACT",
        "MENTIONS_MANY_PARAMETERS",
        "NO_BACKTEST_EVIDENCE",
        "THIN_VALIDATION_PLAN",
        "THIN_ROLLBACK_PLAN",
        "THIN_EVIDENCE",
    } <= set(result["factors"])


def test_a_blocked_proposal_is_assessed_as_blocked() -> None:
    result = risk.assess(make(), blocked=True)
    assert result["level"] == "BLOCKED" and "POLICY_BLOCKED" in result["factors"]
    assert result["review_depth"] == risk.REVIEW_DEPTH["BLOCKED"]


def test_risk_output_is_fixed_vocabulary_only() -> None:
    parsed = replace(make(), assumptions=("<script>x</script>",))
    result = risk.assess(parsed, blocked=False)
    assert set(result) == {"level", "score", "factors", "review_depth", "advisory_only"}
    assert all(f in risk.FACTOR_TEXT for f in result["factors"])
    assert "<script" not in str(result)
