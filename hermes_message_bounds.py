"""Bounded projections of an agent message list.

One truncation rule for tool traffic, shared by the Responses writer and the settlement result
cache: keep every row's role, ids and ``tool_calls``, cut only the payload TEXT, and mark how
much was cut the way the Responses truncation rule does.  Both callers need it for the same
reason -- a stored copy of a turn's messages is written whole and readers only ever need the
shape plus the head of each tool output:

* ``gateway.platforms.api_server_openai_routes``: a stored snapshot embeds the whole cumulative
  history, so a few large tool outputs pushed one response_store write to ~677 KB (#82513).
* ``hermes_state_terminal``: a settled-result row is a replay cache with a hard per-row budget,
  and the current-turn tool in/out items must survive the bound (``/v1/responses`` output,
  ``run.completed`` transcript).
"""
import json
from typing import Any


def cap_text(text: str, keep: int) -> str:
    """Head of ``text`` plus a marker saying how much was cut (the Responses truncation rule)."""
    return text[:keep] + "...[" + str(len(text) - keep) + " more chars]"


def _cap_tool_payloads(history: list[Any], max_chars: int, capped: list[int]) -> list[Any]:
    """Copy of ``history`` with tool outputs and string tool-call arguments longer than
    ``max_chars`` cut down.  Only tool rows and ``tool_calls`` blobs change; user/assistant text
    is left alone, and the agent's own transcript rows are never mutated (rows are copied).
    ``capped`` accumulates the number of payloads that were cut."""
    if max_chars <= 0:
        return history
    out: list[Any] = []
    for msg in history:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        content = msg.get("content")
        if msg.get("role") == "tool" and isinstance(content, str) and len(content) > max_chars:
            msg = {**msg, "content": cap_text(content, max_chars)}
            capped[0] += 1
        tool_calls = msg.get("tool_calls")
        if msg.get("role") == "assistant" and isinstance(tool_calls, list):
            capped_calls = []
            for call in tool_calls:
                if not isinstance(call, dict):
                    capped_calls.append(call)
                    continue
                fn = call.get("function")
                if isinstance(fn, dict):
                    raw = fn.get("arguments")
                    if isinstance(raw, str) and len(raw) > max_chars:
                        try:
                            args = json.loads(raw)
                        except ValueError:
                            args = None
                        if isinstance(args, dict):
                            for k, v in args.items():
                                if isinstance(v, str) and len(v) > max_chars:
                                    args[k] = cap_text(v, max_chars)
                                    capped[0] += 1
                            call = {**call, "function": {**fn, "arguments": json.dumps(args)}}
                capped_calls.append(call)
            msg = {**msg, "tool_calls": capped_calls}
        out.append(msg)
    return out


def cap_tool_payloads(history: list[Any], max_chars: int) -> list[Any]:
    """``history`` copied with every tool payload longer than ``max_chars`` cut down."""
    return _cap_tool_payloads(history, max_chars, [0])


def cap_tool_payloads_counted(history: list[Any], max_chars: int) -> tuple[list[Any], int]:
    """Same as :func:`cap_tool_payloads`, also returning how many payloads were cut."""
    capped = [0]
    out = _cap_tool_payloads(history, max_chars, capped)
    return out, capped[0]
