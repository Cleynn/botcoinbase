"""Read-only observability: internal metrics, health snapshot and alert attention items.

Nothing here can change bot, exchange, pair or configuration state, and nothing here calls out
of the process. Monitoring failures must never affect the application (see docs/monitoring.md).
"""
