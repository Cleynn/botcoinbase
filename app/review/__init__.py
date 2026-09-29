"""Read-only LLM review packages (Phase 6).

A package is a sanitized, historical, checksummed ZIP an ADMIN may hand to an LLM chat by hand. It
carries no secret, no private payload and nothing that can act. Nothing in this package calls an
LLM, imports a proposal or changes bot, pair, risk, order, configuration or live state.
"""
