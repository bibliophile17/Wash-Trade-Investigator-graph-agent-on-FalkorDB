"""Generate synthetic trading data (with planted wash-trading rings) and load it into FalkorDB.

Graph model
  (:Wallet {id})-[:TRADED {token, amount, ts}]->(:Wallet)
  (:Wallet)-[:FUNDED_BY]->(:Funder {id})

The ground truth (which wallets are in a ring) is NOT stored in the graph.
The agent has to discover the rings using graph queries only.
"""
import random
import time

from db import get_graph

random.seed(7)

TOKENS = ["PEPE2", "MOONX", "ZAPT", "BLAZE", "NOVA"]
N_NORMAL = 300
N_FUNDERS = 60
N_NORMAL_TRADES = 2600

# token -> (ring size, number of circular trades, shared funders)
RINGS = {
    "MOONX": (6, 70, ["FX-ALPHA"]),
    "ZAPT": (4, 36, ["FX-BETA", "FX-GAMMA"]),
}


def batches(rows, n=500):
    for i in range(0, len(rows), n):
        yield rows[i : i + n]


def build():
    wallets, funded, trades = [], [], []
    now = int(time.time())

    normal = [f"W{i:03d}" for i in range(N_NORMAL)]
    funders = [f"F{i:02d}" for i in range(N_FUNDERS)]
    wallets += normal
    for w in normal:
        funded.append({"w": w, "f": random.choice(funders)})

    # normal market activity: random pairs, random tokens, lognormal sizes
    for _ in range(N_NORMAL_TRADES):
        a, b = random.sample(normal, 2)
        trades.append(
            {
                "a": a,
                "b": b,
                "token": random.choice(TOKENS),
                "amount": round(random.lognormvariate(5.5, 1.0), 2),
                "ts": now - random.randint(0, 7 * 24 * 3600),
            }
        )

    # planted wash-trading rings
    planted = {}
    for idx, (token, (size, n_trades, ring_funders)) in enumerate(RINGS.items(), start=1):
        ring = [f"R{idx}_{i}" for i in range(size)]
        planted[token] = ring
        wallets += ring
        for w in ring:
            funded.append({"w": w, "f": random.choice(ring_funders)})
        base = random.choice([900, 1500])
        for _ in range(n_trades):
            a, b = random.sample(ring, 2)
            trades.append(
                {
                    "a": a,
                    "b": b,
                    "token": token,
                    "amount": round(base * random.uniform(0.97, 1.03), 2),
                    "ts": now - random.randint(0, 36 * 3600),
                }
            )
        # a few innocent outsiders trade with the ring (camouflage)
        for _ in range(12):
            outsider = random.choice(normal)
            r = random.choice(ring)
            a, b = random.sample([outsider, r], 2)
            trades.append(
                {
                    "a": a,
                    "b": b,
                    "token": token,
                    "amount": round(random.lognormvariate(5.0, 0.8), 2),
                    "ts": now - random.randint(0, 5 * 24 * 3600),
                }
            )
        # a ring wallet also holds a few normal-looking trades in other tokens
        for _ in range(8):
            r = random.choice(ring)
            o = random.choice(normal)
            other = random.choice([t for t in TOKENS if t != token])
            trades.append(
                {"a": r, "b": o, "token": other, "amount": round(random.lognormvariate(5, 1), 2),
                 "ts": now - random.randint(0, 7 * 24 * 3600)}
            )

    return wallets, funders + [f for r in RINGS.values() for f in r[2]], funded, trades, planted


def main():
    g = get_graph()
    try:
        g.delete()
    except Exception:
        pass
    g = get_graph()

    wallets, funder_ids, funded, trades, planted = build()
    funder_ids = sorted(set(funder_ids))

    for q in ("CREATE INDEX FOR (w:Wallet) ON (w.id)", "CREATE INDEX FOR (f:Funder) ON (f.id)"):
        try:
            g.query(q)
        except Exception as e:  # index may already exist
            print("index note:", e)

    for chunk in batches([{"id": w} for w in wallets]):
        g.query("UNWIND $rows AS r CREATE (:Wallet {id: r.id})", {"rows": chunk})
    for chunk in batches([{"id": f} for f in funder_ids]):
        g.query("UNWIND $rows AS r CREATE (:Funder {id: r.id})", {"rows": chunk})
    for chunk in batches(funded):
        g.query(
            "UNWIND $rows AS r MATCH (w:Wallet {id: r.w}), (f:Funder {id: r.f}) "
            "CREATE (w)-[:FUNDED_BY]->(f)",
            {"rows": chunk},
        )
    for chunk in batches(trades):
        g.query(
            "UNWIND $rows AS r MATCH (a:Wallet {id: r.a}), (b:Wallet {id: r.b}) "
            "CREATE (a)-[:TRADED {token: r.token, amount: r.amount, ts: r.ts}]->(b)",
            {"rows": chunk},
        )

    print(f"Loaded {len(wallets)} wallets, {len(funder_ids)} funders, {len(trades)} trades.")
    print("Planted rings (hidden from the agent - use to check your results):")
    for token, ring in planted.items():
        print(f"  {token}: {', '.join(ring)}")


if __name__ == "__main__":
    main()
