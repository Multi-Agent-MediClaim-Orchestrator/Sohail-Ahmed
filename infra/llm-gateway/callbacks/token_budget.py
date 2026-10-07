"""Re-export of the daily token budget (cloud aliases only). Wired inside ``pii_guard.ProxyHandler`` so a single hook enforces
metadata -> allow-list -> PII -> budget in that order (04-04 §7.1)."""

from .core import TokenBudget, budget_key  # noqa: F401
