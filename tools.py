"""Graph tools. Every tool is a thin wrapper around Cypher against FalkorDB.

The agent (LLM or rule-based) only ever sees the world through these functions.
"""
import hashlib
import time

from db import get_graph

g = get_graph()


def q(cypher, params=None):
    return g.query(cypher, params or {}).result_set


# tools


def token_overview():
    """Trade count and total volume for every token."""
    rows = q(
        "MATCH ()-[t:TRADED]->() "
        "RETURN t.token, count(t) AS trades, sum(t.amount) AS volume ORDER BY trades DESC"
    )
    return [{"token": r[0], "trades": r[1], "volume": round(r[2], 2)} for r in rows]


def find_trade_cycles(token: str):
    """Find wallets that trade a token in circles (A->B->A and A->B->C->A).

    Returns the wallets involved, grouped into clusters of connected cycle-members.
    """
    pairs = q(
        "MATCH (a:Wallet)-[x:TRADED]->(b:Wallet)-[y:TRADED]->(a) "
        "WHERE x.token = $t AND y.token = $t AND a.id < b.id "
        "RETURN a.id, b.id, count(x) AS legs",
        {"t": token},
    )
    triples = q(
        "MATCH (a:Wallet)-[x:TRADED]->(b:Wallet)-[y:TRADED]->(c:Wallet)-[z:TRADED]->(a) "
        "WHERE x.token = $t AND y.token = $t AND z.token = $t "
        "AND a.id < b.id AND a.id < c.id "
        "RETURN DISTINCT a.id, b.id, c.id LIMIT 300",
        {"t": token},
    )

    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    for a, b, legs in pairs:
        if legs >= 2:  # one round trip by chance is normal; repeated ones are not
            union(a, b)
    for a, b, c in triples:
        union(a, b)
        union(b, c)

    groups = {}
    for node in list(parent):
        groups.setdefault(find(node), []).append(node)
    clusters = [sorted(v) for v in groups.values() if len(v) >= 3]
    clusters.sort(key=len, reverse=True)
    return {
        "token": token,
        "round_trip_pairs": len(pairs),
        "three_wallet_cycles": len(triples),
        "clusters": clusters,
    }


def shared_funders(wallet_ids: list):
    """Which funding wallets (funders) paid for two or more of the given wallets?"""
    rows = q(
        "MATCH (w:Wallet)-[:FUNDED_BY]->(f:Funder) WHERE w.id IN $ids "
        "RETURN f.id, collect(w.id) AS wallets",
        {"ids": wallet_ids},
    )
    return [{"funder": r[0], "wallets": sorted(r[1])} for r in rows if len(r[1]) >= 2]


def cluster_stats(wallet_ids: list, token: str):
    """How much of this token's trading of these wallets is among themselves?"""
    inside = q(
        "MATCH (a:Wallet)-[t:TRADED]->(b:Wallet) "
        "WHERE t.token = $tok AND a.id IN $ids AND b.id IN $ids "
        "RETURN count(t), sum(t.amount), min(t.amount), max(t.amount)",
        {"ids": wallet_ids, "tok": token},
    )[0]
    total = q(
        "MATCH (a:Wallet)-[t:TRADED]->(b:Wallet) "
        "WHERE t.token = $tok AND (a.id IN $ids OR b.id IN $ids) "
        "RETURN count(t), sum(t.amount)",
        {"ids": wallet_ids, "tok": token},
    )[0]
    n_in, vol_in, lo, hi = inside
    n_all, vol_all = total
    vol_in, vol_all = vol_in or 0, vol_all or 0
    return {
        "internal_trades": n_in,
        "internal_volume": round(vol_in, 2),
        "all_trades_touching_cluster": n_all,
        "internal_share_of_volume": round(vol_in / vol_all, 3) if vol_all else 0,
        "trade_size_min": lo,
        "trade_size_max": hi,
        "trade_size_spread": round((hi - lo) / hi, 3) if hi else None,
    }


def rank_hubs(wallet_ids: list, token: str):
    """Rank wallets in a cluster by importance. Uses FalkorDB PageRank, falls back to degree."""
    method = "pagerank"
    scores = {}
    try:
        rows = q("CALL algo.pageRank('Wallet', 'TRADED') YIELD node, score RETURN node.id, score")
        scores = {r[0]: r[1] for r in rows if r[0] in set(wallet_ids)}
    except Exception:
        scores = {}
    if not scores:
        method = "degree"
        rows = q(
            "MATCH (a:Wallet)-[t:TRADED]-(b:Wallet) "
            "WHERE t.token = $tok AND a.id IN $ids AND b.id IN $ids "
            "RETURN a.id, count(t)",
            {"ids": wallet_ids, "tok": token},
        )
        scores = {r[0]: r[1] for r in rows}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return {"method": method, "ranking": [{"wallet": w, "score": round(s, 4)} for w, s in ranked]}


def flag_wallets(wallet_ids: list, token: str, reason: str):
    """ACTION: flag the wallets, and write a Case node linked to them in the graph."""
    cid = "CASE-" + token + "-" + hashlib.md5(",".join(sorted(wallet_ids)).encode()).hexdigest()[:6]
    q(
        "MERGE (c:Case {id: $cid}) SET c.token = $tok, c.reason = $reason, c.created = $now "
        "WITH c UNWIND $ids AS i MATCH (w:Wallet {id: i}) "
        "SET w.flagged = true MERGE (c)-[:FLAGGED]->(w)",
        {"cid": cid, "tok": token, "reason": reason, "now": int(time.time()), "ids": wallet_ids},
    )
    return {"case_id": cid, "flagged": len(wallet_ids)}


# UI helpers


def case_graph(case_id: str):
    """Nodes and edges behind a case, for the evidence picture."""
    case = q("MATCH (c:Case {id: $c}) RETURN c.token, c.reason", {"c": case_id})
    if not case:
        return {"nodes": [], "edges": []}
    token, reason = case[0]
    ids = [r[0] for r in q("MATCH (:Case {id: $c})-[:FLAGGED]->(w:Wallet) RETURN w.id", {"c": case_id})]
    trades = q(
        "MATCH (a:Wallet)-[t:TRADED]->(b:Wallet) "
        "WHERE t.token = $tok AND a.id IN $ids AND b.id IN $ids "
        "RETURN a.id, b.id, count(t), sum(t.amount)",
        {"ids": ids, "tok": token},
    )
    funding = q(
        "MATCH (w:Wallet)-[:FUNDED_BY]->(f:Funder) WHERE w.id IN $ids RETURN w.id, f.id",
        {"ids": ids},
    )
    shared = {}
    for w, f in funding:
        shared.setdefault(f, []).append(w)
    nodes = [{"id": w, "label": w, "group": "wallet"} for w in ids]
    edges = [
        {"from": a, "to": b, "label": f"{n}x", "value": n, "title": f"{n} trades, {round(v)} total"}
        for a, b, n, v in trades
    ]
    for f, ws in shared.items():
        if len(ws) >= 2:
            nodes.append({"id": f, "label": f, "group": "funder"})
            edges.extend({"from": f, "to": w, "dashes": True, "label": "funded"} for w in ws)
    return {"case_id": case_id, "token": token, "reason": reason, "nodes": nodes, "edges": edges}


def list_cases():
    return [
        {"case_id": r[0], "token": r[1], "reason": r[2]}
        for r in q("MATCH (c:Case) RETURN c.id, c.token, c.reason ORDER BY c.created DESC")
    ]

def reset_cases():
    n_cases = q("MATCH (c:Case) RETURN count(c)")[0][0]
    n_flagged = q("MATCH (w:Wallet) WHERE w.flagged = true RETURN count(w)")[0][0]
    q("MATCH (c:Case) DETACH DELETE c")
    q("MATCH (w:Wallet) WHERE w.flagged = true REMOVE w.flagged")
    return {"ok": True, "cases_deleted": n_cases, "wallets_unflagged": n_flagged}

