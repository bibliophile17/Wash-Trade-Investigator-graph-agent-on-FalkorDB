"""The investigator agent.

Two modes, same tools:
  * LLM mode  - if ANTHROPIC_API_KEY is set, Claude picks which graph tools to call.
  * Offline   - no key needed; a fixed playbook calls the same tools in the same order.
"""
import json
import os

import tools

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

SYSTEM = """You are a market-surveillance analyst agent. You investigate a token for wash trading
(wallets trading with themselves in circles to fake volume) using graph tools backed by FalkorDB.

Process: 1) find_trade_cycles for the token. 2) For each cluster, call cluster_stats, shared_funders
and rank_hubs. 3) Decide. A cluster is wash trading when wallets repeatedly trade among themselves,
most of the volume stays inside the cluster, trade sizes are suspiciously uniform, and/or the wallets
share a funder. 4) If it is wash trading, call flag_wallets once per cluster with a clear reason.
5) Finish with a short plain-English verdict that cites the evidence numbers. Do not flag clusters
that look like normal trading."""

TOOL_SPECS = [
    {
        "name": "find_trade_cycles",
        "description": "Find clusters of wallets that trade a token in circles (A->B->A, A->B->C->A).",
        "input_schema": {
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
        },
    },
    {
        "name": "cluster_stats",
        "description": "Share of a token's volume traded inside a set of wallets, plus trade-size spread.",
        "input_schema": {
            "type": "object",
            "properties": {"wallet_ids": {"type": "array", "items": {"type": "string"}}, "token": {"type": "string"}},
            "required": ["wallet_ids", "token"],
        },
    },
    {
        "name": "shared_funders",
        "description": "Funding wallets that paid for two or more of the given wallets.",
        "input_schema": {
            "type": "object",
            "properties": {"wallet_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["wallet_ids"],
        },
    },
    {
        "name": "rank_hubs",
        "description": "Rank wallets in a cluster by graph importance (PageRank or degree).",
        "input_schema": {
            "type": "object",
            "properties": {"wallet_ids": {"type": "array", "items": {"type": "string"}}, "token": {"type": "string"}},
            "required": ["wallet_ids", "token"],
        },
    },
    {
        "name": "flag_wallets",
        "description": "ACTION: flag wallets as wash trading and open a case in the graph.",
        "input_schema": {
            "type": "object",
            "properties": {
                "wallet_ids": {"type": "array", "items": {"type": "string"}},
                "token": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["wallet_ids", "token", "reason"],
        },
    },
]

DISPATCH = {
    "find_trade_cycles": tools.find_trade_cycles,
    "cluster_stats": tools.cluster_stats,
    "shared_funders": tools.shared_funders,
    "rank_hubs": tools.rank_hubs,
    "flag_wallets": tools.flag_wallets,
}


def _short(obj, n=240):
    s = json.dumps(obj)
    return s if len(s) <= n else s[:n] + "..."


def judge(stats, funders, cycles_count):
    """Transparent scoring used by offline mode (and a good sanity check for the LLM)."""
    score, why = 0, []
    if stats["internal_share_of_volume"] >= 0.7:
        score += 2
        why.append(f"{stats['internal_share_of_volume']:.0%} of the token's volume stays inside the cluster")
    if stats["trade_size_spread"] is not None and stats["trade_size_spread"] <= 0.15:
        score += 1
        why.append(f"trade sizes are almost identical (spread {stats['trade_size_spread']:.0%})")
    if funders:
        score += 2
        why.append("wallets share funder(s): " + ", ".join(f["funder"] for f in funders))
    if cycles_count >= 3:
        score += 1
        why.append("many circular trades")
    return score, why


def run_offline(token, emit):
    cycles = DISPATCH["find_trade_cycles"](token=token)
    emit("find_trade_cycles", {"token": token}, cycles)
    cases, lines = [], []
    for cluster in cycles["clusters"]:
        stats = DISPATCH["cluster_stats"](wallet_ids=cluster, token=token)
        emit("cluster_stats", {"wallet_ids": cluster, "token": token}, stats)
        funders = DISPATCH["shared_funders"](wallet_ids=cluster)
        emit("shared_funders", {"wallet_ids": cluster}, funders)
        hubs = DISPATCH["rank_hubs"](wallet_ids=cluster, token=token)
        emit("rank_hubs", {"wallet_ids": cluster, "token": token}, hubs)
        score, why = judge(stats, funders, cycles["three_wallet_cycles"] + cycles["round_trip_pairs"])
        if score >= 4:
            reason = "Wash trading: " + "; ".join(why)
            res = DISPATCH["flag_wallets"](wallet_ids=cluster, token=token, reason=reason)
            emit("flag_wallets", {"wallet_ids": cluster, "token": token, "reason": reason}, res)
            cases.append(res["case_id"])
            lines.append(f"FLAGGED {len(cluster)} wallets ({', '.join(cluster)}). {reason}. "
                         f"Top hub: {hubs['ranking'][0]['wallet']}.")
        else:
            lines.append(f"Cluster {cluster} looked normal (score {score}); not flagged.")
    if not cycles["clusters"]:
        lines.append(f"No circular-trading clusters found for {token}. Looks clean.")
    return {"verdict": "\n".join(lines), "cases": cases}


def run_llm(token, emit):
    import anthropic

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": f"Investigate the token {token} for wash trading."}]
    cases, verdict = [], ""
    for _ in range(14):
        resp = client.messages.create(
            model=MODEL, max_tokens=1500, system=SYSTEM, tools=TOOL_SPECS, messages=messages
        )
        messages.append({"role": "assistant", "content": resp.content})
        verdict = "".join(b.text for b in resp.content if b.type == "text") or verdict
        if resp.stop_reason != "tool_use":
            break
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            try:
                out = DISPATCH[b.name](**b.input)
            except Exception as e:  # tell the model, let it recover
                out = {"error": str(e)}
            emit(b.name, b.input, out)
            if b.name == "flag_wallets" and "case_id" in out:
                cases.append(out["case_id"])
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out)})
        messages.append({"role": "user", "content": results})
    return {"verdict": verdict.strip(), "cases": cases}


def investigate(token: str):
    steps = []

    def emit(tool, args, result):
        steps.append({"tool": tool, "args": _short(args, 160), "result": _short(result)})

    use_llm = bool(os.getenv("ANTHROPIC_API_KEY"))
    mode = "llm" if use_llm else "offline"
    try:
        out = run_llm(token, emit) if use_llm else run_offline(token, emit)
    except Exception as e:
        if not use_llm:
            raise
        steps.append({"tool": "llm_error", "args": "", "result": f"{e} - falling back to offline mode"})
        mode = "offline (LLM failed)"
        out = run_offline(token, emit)
    return {"token": token, "mode": mode, "steps": steps, **out}
