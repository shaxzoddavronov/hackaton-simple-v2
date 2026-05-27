"""Prometheus metric singletons for the QueryMind agent.

The default HTTP histogram + counter come from
``prometheus-fastapi-instrumentator``. This module owns the
*agent-level* metrics — things you can't infer from HTTP status codes:

  * Which intent the coordinator picked.
  * Which dialect ran (and whether the SQL succeeded).
  * How long the whole chat turn took end-to-end.

Counters / histograms are module-level so any node can ``inc()`` /
``observe()`` without dragging request state through every function
signature.
"""
from __future__ import annotations

from prometheus_client import Counter, Histogram


# How many chat turns finished, by intent + outcome.
chat_turns_total = Counter(
    "qm_chat_turns_total",
    "Total chat turns served, labeled by intent and outcome",
    labelnames=("intent", "status"),  # status: ok | error | empty
)

# How many SQL queries the agent executed, by dialect + outcome.
query_history_total = Counter(
    "qm_query_history_total",
    "Total SQL executions performed by the agent",
    labelnames=("dialect", "status"),  # status: ok | rejected | error | timeout
)

# How many calls we made to the LLM (vLLM endpoint), per node + outcome.
llm_calls_total = Counter(
    "qm_llm_calls_total",
    "Total LLM structured() calls",
    labelnames=("node", "outcome"),  # outcome: ok | repair | failed
)

# End-to-end latency of a chat turn (excluding SSE-keepalive overhead).
chat_duration_seconds = Histogram(
    "qm_chat_duration_seconds",
    "Wall-clock latency of /chat from request hit to final SSE event",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
