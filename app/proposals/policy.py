"""Policy triage of proposal text: heuristic, deliberately over-inclusive, never a judge of intent.

Every proposal is UNTRUSTED ADVISORY INPUT. This module decides only whether the TEXT asks for, or
implies, something a proposal must never ask for: bypassing risk controls, direct exchange actions,
direct pair-state changes, secret or security-control changes, API or network access changes,
live activation, automatic capital increases, automated ordering, or automatic application of any
change; plus markup, templates, code, SQL, shell, links and paths (proposal text is data, never an
instruction). A hit BLOCKS the proposal. A mention of a forbidden action is a hit even inside a
negation ("never bypass the reserve"): rephrase and import again as a new proposal. The only
exemption is a plain profit-guarantee disclaimer ("no guarantee of profit").

Matching is on normalised text (Unicode NFKC, casefolded, common look-alike letters mapped,
punctuation removed) so trivial spelling tricks do not evade it. Patterns are bounded (no nested
unbounded quantifiers) and inputs are length-capped by the schema.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from re import Pattern
from typing import Final

from app.proposals.schema import Parsed

RULE_TEXT: Final[dict[str, str]] = {
    "RISK_BYPASS": "asks to bypass, relax or remove a risk, reserve, cap, reconciliation or gate control",
    "SECRET_CHANGE": "concerns secrets, credentials, keys, tokens or sessions",
    "SECURITY_CONTROL_CHANGE": "asks to weaken or remove a security control",
    "API_ACCESS_CHANGE": "asks for exchange, API, network or permission access changes",
    "LIVE_ACTIVATION": "asks to enable, activate or approach live trading",
    "PAIR_STATE_CHANGE": "asks to change a pair's state directly",
    "CAPITAL_INCREASE": "asks to increase capital, exposure, size or leverage",
    "AUTOMATED_ORDER": "asks for direct or automated order behaviour",
    "AUTO_APPLICATION": "asks to apply a change automatically or without review",
    "PROFIT_GUARANTEE": "claims guaranteed or risk-free profit",
    "MARKUP_OR_TEMPLATE": "contains markup, entities or template syntax",
    "EXECUTABLE_CONTENT": "contains code, SQL, shell or command text",
    "EXTERNAL_REFERENCE": "contains links, file paths, environment assignments or config syntax",
    "NO_TRADE_NOT_PRESERVED": "does not state that the system stays NO_TRADE until validated",
    "PROFIT_DECLARATION_MISSING": "does not declare that it makes no guarantee of profit",
}

_CONFUSABLES: Final = str.maketrans(
    {
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i",
        "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "α": "a", "ο": "o", "ν": "v", "ρ": "p",
        "τ": "t", "ι": "i", "κ": "k", "β": "b", "ε": "e", "η": "n", "μ": "u", "ѵ": "v",
    }
)  # fmt: skip


def normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).casefold().translate(_CONFUSABLES)
    t = re.sub(r"[^a-z0-9_]+", " ", t)
    return " ".join(t.split())


def _near(a: str, b: str, gap: int = 6) -> Pattern[str]:
    """`a` and `b` within `gap` words of each other, in either order (normalised text)."""
    return re.compile(
        rf"\b(?:{a})\b(?: \w+){{0,{gap}}} \b(?:{b})\b|\b(?:{b})\b(?: \w+){{0,{gap}}} \b(?:{a})\b"
    )


def _any(*patterns: str) -> list[Pattern[str]]:
    return [re.compile(p) for p in patterns]


# Verbs are stems (`disabl\w*` covers disable, disabled, disabling): passive and inflected forms
# must not evade the triage. Nouns list the plural forms that matter.
_RISK_V = (
    r"bypass\w*|circumvent\w*|overrid\w*|overrode|disabl\w*|skip\w*|ignor\w*|remov\w*|"
    r"relax\w*|loosen\w*|weaken\w*|suppress\w*|waiv\w*|lift\w*|turn\w* off|switch\w* off|"
    r"deactivat\w*|widen\w*|exceed\w*|breach\w*|drop\w*|rais\w*|increas\w*|exempt\w*"
)
_RISK_N = (
    r"risk|risks|risk engine|risk checks?|risk limits?|risk controls?|circuit breakers?|"
    r"kill switch|drawdown stop|drawdown limit|stop loss|reserve|protected reserve|deployment cap|"
    r"cap|caps|reconciliation|safeguards?|guardrails?|guard rails?|live gate|safety checks?|"
    r"limits?|max deployment|max_deployment|total capital|total_capital"
)
_SECRET_N = (
    r"secrets?|credentials?|passwords?|passphrases?|api keys?|apikeys?|api secrets?|private keys?|"
    r"tokens?|cookies?|session ids?|jwt|certificates?|dotenv|env files?|keystore|signing keys?|"
    r"access keys?|api_keys?|secret_keys?|private_keys?|access_keys?|\w+_secret|\w+_token|"
    r"\w+_password"
)
_SECRET_V = (
    r"chang\w*|rotat\w*|set|sett\w*|updat\w*|stor\w*|add\w*|replac\w*|expos\w*|print\w*|"
    r"log|logg\w*|send\w*|sent|shar\w*|reveal\w*|past\w*|embed\w*|hardcod\w*|commit\w*|"
    r"read|export\w*|includ\w*|writ\w*|wrote|leak\w*|copy|copied|upload\w*|email\w*|put|"
    r"us\w+|provid\w*|suppl\w*|inject\w*|load\w*|fetch\w*|retriev\w*|disclos\w*"
)
_SEC_V = (
    r"disabl\w*|remov\w*|relax\w*|weaken\w*|bypass\w*|turn\w* off|switch\w* off|loosen\w*|"
    r"skip\w*|drop\w*|widen\w*|whitelist\w*|allowlist\w*"
)
_SEC_N = (
    r"csrf|authentication|authorization|authorisation|mfa|reauth|reauthentication|password checks?|"
    r"audit|audit log|logging|tls|https|hsts|rate limits?|rate limiting|throttling|throttle|"
    r"permissions?|sandbox|input validation|validation|checksums?|verification|sanitiz\w+|"
    r"scanner|firewall|allowlists?|encryption|sessions?|origin checks?"
)
_LIVE_V = (
    r"enabl\w*|activat\w*|start\w*|switch\w*|go|going|goes|went|turn\w* on|mov\w*|promot\w*|"
    r"deploy\w*|begin\w*|began|allow\w*|unlock\w*|flip\w*|launch\w*|unblock\w*|graduat\w*|"
    r"lift\w*|approv\w*|authori[sz]\w*|permit\w*|transition\w*"
)
_LIVE_N = (
    r"live trading|live mode|live gate|live pilot|live activation|live deployment|live orders?|"
    r"live execution|live account|live exchange|real money|real funds|production trading|"
    r"mainnet|live"
)
_PAIR_V = (
    r"activat\w*|deactivat\w*|disabl\w*|enabl\w*|re enabl\w*|reenabl\w*|archiv\w*|delet\w*|"
    r"remov\w*|resum\w*|paus\w*|drop\w*|forc\w*|promot\w*|demot\w*|unarchiv\w*|"
    r"reactivat\w*|halt\w*|stop\w* trading|start\w* trading|transition\w*|set|sett\w*"
)
_PAIR_N = (
    r"pairs?|pair state|pair states|pair lifecycle|watchlist|paper_active|paper_eligible|"
    r"research_only|paused pairs?|active pair|trading pair"
)
_CAP_V = (
    r"increas\w*|rais\w*|grow\w*|grew|scal\w* up|doubl\w*|tripl\w*|top\w* up|topup|"
    r"compound\w*|reinvest\w*|expand\w*|boost\w*|allocat\w* more|fund\w*|leverag\w*|"
    r"borrow\w*|margin|enlarg\w*"
)
_CAP_N = (
    r"capital|deposit|deployment|exposure|position size|position sizes|allocation|allocations|"
    r"budget|funds|bankroll|cap|total_capital|max_deployment|order size|order sizes|notional|"
    r"stake|balance|leverage|margin|cell budget|profits?"
)
_ORDER_V = (
    r"plac\w*|submit\w*|send\w*|sent|creat\w*|cancel\w*|replac\w*|amend\w*|modif\w*|"
    r"execut\w*|fir\w*|issu\w*|post\w*"
)
_AUTO_W = (
    r"automatic\w*|autonomous\w*|unattended|hot\w* patch\w*|hotpatch\w*|hot\w* reload\w*|"
    r"on the fly|without human review|without review|without approval|without confirmation|"
    r"without oversight|without intervention|without operator|without manual|without asking|"
    r"no human"
)
_AUTO_V = (
    r"appl\w*|deploy\w*|merg\w*|push\w*|commit\w*|execut\w*|run|runn\w*|install\w*|"
    r"writ\w*|edit\w*|modif\w*|patch\w*|migrat\w*|updat\w*|chang\w*|reconfigur\w*|"
    r"configur\w*|restart\w*|rewrit\w*|adjust\w*|tun\w*|rebalanc\w*|regrid\w*|activat\w*|"
    r"enabl\w*|disabl\w*|approv\w*|implement\w*|roll\w* out|rollout"
)

# rule id -> patterns over NORMALISED text
NORMALISED_RULES: Final[dict[str, list[Pattern[str]]]] = {
    "RISK_BYPASS": [
        _near(_RISK_V, _RISK_N, 8),
        re.compile(r"\bwithout (?:a |any |the )?(?:risk|reserve|cap|limits?|stop|reconciliation)\b"),
        re.compile(r"\bno (?:risk|reserve|drawdown|deployment) (?:limits?|checks?|controls?|cap)\b"),
    ],
    "SECRET_CHANGE": [_near(_SECRET_V, _SECRET_N, 5)]
    + _any(r"\b(?:begin|end) (?:rsa |ec |openssh )?private key\b"),
    "SECURITY_CONTROL_CHANGE": [_near(_SEC_V, _SEC_N, 4)],
    "API_ACCESS_CHANGE": _any(
        r"\b(?:grant|enable|add|open|allow|widen|expand|extend|change|switch to|connect to|call|"
        r"use|using|obtain|request)\b(?: \w+){0,3} \b(?:private|authenticated|trading|trade|"
        r"withdraw|withdrawal|transfer|write)\b(?: \w+){0,2} \b(?:api|endpoints?|permissions?|"
        r"scopes?|access|keys?)\b",
        r"\b(?:private|authenticated) (?:api|endpoints?|rest|websocket)\b",
        r"\b(?:new|additional|extra|different|another|more) (?:\w+ ){0,2}(?:exchanges?|endpoints?|"
        r"apis?|hosts?|domains?|proxy|egress|network|permissions?)\b",
        r"\b(?:enable|grant|allow|open|add|permit|whitelist|allowlist)\b(?: \w+){0,3} \b(?:"
        r"outbound|egress|internet|network|ports?|firewall|domains?|hosts?|proxy|websockets?|"
        r"web socket)\b",
        r"\bwebsockets?\b|\bweb socket\b",
        r"\bwithdraw\w*\b|\btransfer funds\b",
        r"\bapi (?:access|permissions?|scopes?)\b",
    ),
    "LIVE_ACTIVATION": [
        _near(_LIVE_V, _LIVE_N),
        re.compile(r"\blive_(?:eligible|active)\b|\bmode live\b"),
        re.compile(r"\btrading mode(?: \w+){0,3} live\b"),
    ],
    "PAIR_STATE_CHANGE": [_near(_PAIR_V, _PAIR_N, 4), re.compile(r"\bpair_state\b|\bpair_registry\b")],
    "CAPITAL_INCREASE": [
        _near(_CAP_V, _CAP_N),
        re.compile(r"\b(?:auto|automatic|automatically)\b(?: \w+){0,3} \b(?:compound|reinvest|scale|grow|increase)\b"),
        re.compile(r"\bcapital growth\b|\bgrowth policy\b"),
    ],
    "AUTOMATED_ORDER": [
        _near(_ORDER_V, "orders?|trades?|bids?|asks?", 3),
        re.compile(r"\b(?:orders?|trades?|trading)\b(?: \w+){0,2} \b(?:automatically|auto|directly|immediately|autonomously)\b"),
        re.compile(r"\b(?:auto|automatic|automated|autonomous)\b (?:\w+ )?(?:trading|trades?|orders?|execution|rebalanc\w*|liquidat\w*|regrid\w*|order placement)\b"),
        re.compile(r"\bmarket orders?\b|\bcreate_order\b|\bcancel_order\b|\bpost_only false\b|\bimmediate or cancel\b|\bioc\b"),
        re.compile(r"\b(?:sell|liquidate|flatten|dump|close out|unwind)\b(?: \w+){0,3} \b(?:all|everything|positions?|inventory|holdings|at market)\b"),
    ],
    "AUTO_APPLICATION": [
        _near(_AUTO_W, _AUTO_V, 5),
        re.compile(
            r"\bself (?:appl\w*|updat\w*|modif\w*|deploy\w*|patch\w*|chang\w*|tun\w*|rewrit\w*)\b"
            r"|\bauto (?:appl\w*|deploy\w*|merg\w*|updat\w*|tun\w*|patch\w*)\b|\bautopilot\b"
        ),
        re.compile(
            r"\b(?:edit|modify|change|patch|rewrite|update|overwrite|write to|alter|mutate)\b(?: \w+){0,3} "
            r"\b(?:code|config|configuration|database|db|schema|tables?|files?|policy|yaml|env|settings)\b"
            r"(?: \w+){0,3} \b(?:automatically|directly|live|in place|at runtime|on its own|without)\b"
        ),
        re.compile(r"\bno (?:human|manual) (?:review|approval|step|gate)\b|\bskip (?:the )?(?:review|approval|manual)\b"),
    ],
}  # fmt: skip

_DISCLAIMER: Final = re.compile(
    r"\b(?:no|not|never|cannot|can t|without|does not|do not|doesn t|don t|makes no|make no|"
    r"offers no|offer no|provides no|provide no)\b (?:\w+ ){0,2}(?:guarantee|guarantees|"
    r"guaranteed|guaranteeing|assurance|promise)\b(?: of| that| any| on)?"
    r"|\bnot guaranteed\b|\bno \w+ (?:is|are|can be|will be) guaranteed\b"
)
_PROFIT: Final = re.compile(
    r"\b(?:guarantee|guarantees|guaranteed|guaranteeing|risk free|riskless|certain profit|"
    r"sure profit|surefire|cannot lose|can t lose|no risk|zero risk|always profit\w*|never lose\w*|"
    r"assured|100 (?:win|profit|return)\w*|will (?:make|earn|generate|yield|deliver) (?:a )?"
    r"(?:profit|money|returns?|gains?)|will not lose)\b"
)

# rule id -> patterns over RAW text (markup and code are looked for as written)
RAW_RULES: Final[dict[str, list[Pattern[str]]]] = {
    "MARKUP_OR_TEMPLATE": _any(
        r"<\s*[A-Za-z/!?]",
        r"&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z]{2,10});",
        r"\{\{|\}\}|\{%|%\}|\$\{|<%|%>|#\{|\$\(",
    ),
    "EXECUTABLE_CONTENT": [
        re.compile(r"`|~~~"),
        re.compile(r"(?m)^#!"),
        re.compile(
            r"(?is)\bselect\b.{0,80}\bfrom\b|\binsert\s+into\b|\bupdate\s+\w+\s+set\b|\bdelete\s+from\b"
            r"|\bdrop\s+(?:table|database|schema)\b|\balter\s+table\b|\btruncate\s+table\b"
            r"|\bunion\s+select\b|;\s*--"
        ),
        re.compile(
            r"(?i)\b(?:rm\s+-[rf]+|sudo|chmod|chown|curl|wget|bash|sh\s+-c|powershell|ssh|scp|nc\s+-|"
            r"kill\s+-9|docker\s+(?:run|exec|compose)|kubectl|pip\s+install|npm\s+install|"
            r"apt(?:-get)?\s+install|systemctl|crontab|python\s+-c|git\s+(?:push|commit|reset|"
            r"checkout|apply))\b|\|\s*(?:sh|bash)\b|&&"
        ),
        re.compile(
            r"(?i)\b(?:def\s+\w+\s*\(|class\s+\w+\s*[:(]|function\s*\w*\s*\(|lambda\s|eval\s*\(|"
            r"exec\s*\(|__import__|os\.system|subprocess|require\s*\(|console\.log|print\s*\()|=>"
        ),
    ],
    "EXTERNAL_REFERENCE": [
        re.compile(
            r"(?i)\b(?:javascript|vbscript|file|ftp|ssh|http|https|ws|wss)\s*:|data:(?:text|application|image)"
            r"|://|\bwww\."
        ),
        re.compile(
            r"(?:^|[\s\"'(=])(?:/(?:etc|usr|var|home|root|opt|tmp|bin|dev|proc|srv|mnt|app|data|"
            r"review|proposals)\b|~/|\.\./|[A-Za-z]:\\)"
        ),
        re.compile(r"\b[A-Z][A-Z0-9_]{3,}=\S"),
        re.compile(r"\{\s*\""),
    ],
}  # fmt: skip
_YAML_LINE: Final = re.compile(r"(?m)^[ \t]*[A-Za-z_][\w.-]{1,40}[ \t]*:[ \t]+\S")


@dataclass(frozen=True)
class Finding:
    rule: str
    field: str
    excerpt: str
    severity: str = "BLOCK"

    def as_json(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "field": self.field,
            "excerpt": self.excerpt,
            "severity": self.severity,
        }


def _excerpt(text: str, start: int, end: int) -> str:
    piece = text[max(0, start - 12) : end + 12][:80]
    return "".join(c if c.isprintable() and c not in "<>&\"'" else " " for c in piece).strip()


def _scan_field(field: str, raw: str, out: dict[tuple[str, str], Finding]) -> None:
    norm = normalise(raw)
    disclaimed = _DISCLAIMER.sub(" ", norm)
    for rule, patterns in NORMALISED_RULES.items():
        for pattern in patterns:
            match = pattern.search(norm)
            if match:
                out.setdefault((rule, field), Finding(rule, field, _excerpt(norm, *match.span())))
                break
    match = _PROFIT.search(disclaimed)
    if match:
        out.setdefault(
            ("PROFIT_GUARANTEE", field),
            Finding("PROFIT_GUARANTEE", field, _excerpt(disclaimed, *match.span())),
        )
    for rule, patterns in RAW_RULES.items():
        for pattern in patterns:
            match = pattern.search(raw)
            if match:
                out.setdefault((rule, field), Finding(rule, field, _excerpt(raw, *match.span())))
                break
    if len(_YAML_LINE.findall(raw)) >= 2:
        out.setdefault(
            ("EXTERNAL_REFERENCE", field), Finding("EXTERNAL_REFERENCE", field, "config-like lines")
        )


def evaluate(parsed: Parsed) -> list[Finding]:
    """All policy findings for a schema-valid proposal, in a stable order."""
    found: dict[tuple[str, str], Finding] = {}
    for name, text in parsed.texts.items():
        _scan_field(name, text, found)
    for item in parsed.assumptions:
        _scan_field("assumptions", item, found)
    if not parsed.should_remain_no_trade:
        found[("NO_TRADE_NOT_PRESERVED", "should_remain_no_trade_until_validated")] = Finding(
            "NO_TRADE_NOT_PRESERVED", "should_remain_no_trade_until_validated", "declared false"
        )
    if not parsed.no_profit_guarantee:
        found[("PROFIT_DECLARATION_MISSING", "no_profit_guarantee")] = Finding(
            "PROFIT_DECLARATION_MISSING", "no_profit_guarantee", "declared false"
        )
    return sorted(found.values(), key=lambda f: (f.rule, f.field))
