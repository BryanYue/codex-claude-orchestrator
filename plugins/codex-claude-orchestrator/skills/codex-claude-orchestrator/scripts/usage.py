"""Scope-labelled token and cost figures from the Claude CLI's final result event.

``usage`` on a result covers only the main agent's final result.  ``modelUsage``
and ``total_cost_usd`` are CLI-session cumulative client estimates that include
subagents and, for a resumed session, may include earlier turns.  Every result
event of a run repeats the cumulative values, so only the final event is read
and nothing is added across events, scopes or rounds.
"""
from __future__ import annotations

import math
from typing import Any

TOKEN_FIELDS = (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
                ("cache_read_input_tokens", "cacheReadInputTokens"),
                ("cache_creation_input_tokens", "cacheCreationInputTokens"))
COST_BASIS = "Claude CLI client-side estimate; not an actual charge or remaining subscription quota"


def token_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def cost_amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def main_agent_final(final: dict[str, Any]) -> dict[str, Any]:
    usage = final.get("usage")
    tokens = {key: token_count(usage.get(key)) if isinstance(usage, dict) else None for key, _ in TOKEN_FIELDS}
    return {"scope": "main_agent_final_result", "includes_subagents": False, "available": isinstance(usage, dict),
            "tokens": tokens, "complete": all(value is not None for value in tokens.values())}


def cli_session(final: dict[str, Any], resumed: bool) -> dict[str, Any]:
    raw = final.get("modelUsage")
    models: list[dict[str, Any]] = []
    if raw is None:
        state = "missing"
    elif not isinstance(raw, dict):
        state = "malformed"
    elif not raw:
        state = "empty"
    else:
        state = "present"
        for name in sorted(raw, key=str):
            entry = raw[name]
            valid = isinstance(name, str) and bool(name) and isinstance(entry, dict)
            tokens = {key: token_count(entry.get(source)) if valid else None for key, source in TOKEN_FIELDS}
            cost = cost_amount(entry.get("costUSD")) if valid else None
            models.append({"model": name if isinstance(name, str) and name else None, "tokens": tokens, "cost_usd": cost,
                           "complete": valid and cost is not None and all(value is not None for value in tokens.values())})
    totals = None
    if models:
        # A total is reported only when every model supplied that field.
        totals = {key: (sum(model["tokens"][key] for model in models)
                        if all(model["tokens"][key] is not None for model in models) else None)
                  for key, _ in TOKEN_FIELDS}
    total_cost = cost_amount(final.get("total_cost_usd"))
    complete = (state == "present" and totals is not None and all(value is not None for value in totals.values())
                and all(model["complete"] for model in models) and total_cost is not None)
    return {"scope": "cli_session_cumulative", "includes_subagents": True, "may_include_prior_turns": bool(resumed),
            "model_usage_state": state, "models": models, "totals": totals, "total_cost_usd": total_cost,
            "cost_basis": COST_BASIS, "complete": complete}


def usage_report(final: Any, *, result_events: int, resumed: bool) -> dict[str, Any] | None:
    if not isinstance(final, dict):
        return None
    return {"schema_version": 1, "source": "final_cli_result",
            "result_events": result_events if type(result_events) is int and result_events >= 0 else None,
            "resumed_session": bool(resumed), "cli_session": cli_session(final, resumed),
            "main_agent_final": main_agent_final(final),
            "note": ("cli_session is the CLI's cumulative estimate for the whole session including subagents; "
                     "main_agent_final is only the main agent's final result and is not a task total.")}
