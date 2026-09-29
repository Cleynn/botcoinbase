# Review package and proposal specification

Status: **review packages: implemented (Phase 6, see `docs/review-packages.md`, DEC-017).
Proposals: NOT implemented.** DEC-000 is unacknowledged.

Source of truth: `baseline/TRADINGDOTS_HANDOFF.md`, Sections 4.3 and 4.4; Master Contract Part 1.

## Review packages
The lifecycle, schema (`tradingdots.review-package/1`), sanitizer, integrity checks, storage,
protected download, retention and metrics are specified in `docs/review-packages.md` and enforced
by `app/review/*`, `app/storage/migrations/0004_review.sql` and the tests named there.

## Proposals
Proposal import, strict validation, policy evaluation, review and the manual change-request
workflow (baseline 4.4, Phase 8) are **not built**. Nothing reads a reply from an AI assistant, and
the review prompt in each package asks for a written advisory reply that no system imports.
To be written in the phase that introduces the behavior. Do not paste unverified claims here.
