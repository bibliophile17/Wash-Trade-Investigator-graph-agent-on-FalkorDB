# Wash-Trade Investigator

An AI agent that investigates crypto tokens for **wash trading** (wallets trading with each other in circles to fake volume).
It reasons over a **FalkorDB** graph, decides, **acts** (flags wallets and opens a case in the graph), and **explains** with the exact subgraph as evidence.

Built for Graph Hacks, Track 1: *Agents That Act on Connected Data*.

## Why a graph?

A table shows trades as separate rows. Wash trading is a *shape*: money going A -> B -> C -> A again and again,
among wallets that were all funded by the same source. That is a multi-hop pattern, which Cypher finds in a few lines.
Remove FalkorDB and the agent has no way to see rings.

## Data model

```
(:Wallet {id, flagged})-[:TRADED {token, amount, ts}]->(:Wallet)
(:Wallet)-[:FUNDED_BY]->(:Funder {id})
(:Case {id, token, reason, created})-[:FLAGGED]->(:Wallet)
```

`seed.py` generates 300 normal wallets with random trades, plus two planted rings
(MOONX: 6 wallets, ZAPT: 4 wallets). The ring membership is **not** stored in the graph; the agent must discover it.

## Agent tools (each is a Cypher query)

| Tool | What it asks the graph |
|---|---|
| `find_trade_cycles(token)` | Round trips A->B->A and triangles A->B->C->A, grouped into clusters |
| `cluster_stats(wallets, token)` | What share of volume stays inside the cluster; how uniform are trade sizes |
| `shared_funders(wallets)` | Which funding wallets paid for several of them |
| `rank_hubs(wallets, token)` | PageRank (falls back to degree) to find the central wallet |
| `flag_wallets(wallets, token, reason)` | **Action:** writes a `Case` node and flags the wallets |

Two modes with the same tools:
- **LLM mode** (set `ANTHROPIC_API_KEY`): Claude chooses which tools to call and writes the verdict.
- **Offline mode** (no key): a fixed playbook calls the same tools with a transparent scoring rule.

## Run it

Prerequisites: Docker, Python 3.10+.

```bash
# 1. start FalkorDB (database on 6379, FalkorDB Browser on http://localhost:3000)
docker compose up -d

# 2. install and load data
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python seed.py

# 3. (optional) use Claude as the agent brain
export ANTHROPIC_API_KEY=sk-ant-...                    # Windows PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."

# 4. start the app
uvicorn app:app --port 8000
```

Open http://localhost:8000, click **MOONX** (or **ZAPT**), then **Investigate**.
Try **NOVA** or **PEPE2** too: the agent should find nothing and flag nothing.

Expected: MOONX flags `R1_0 ... R1_5`, ZAPT flags `R2_0 ... R2_3`.

Look at the graph yourself: open http://localhost:3000, choose graph `washtrade`, and run

```cypher
MATCH (c:Case)-[:FLAGGED]->(w:Wallet) RETURN c, w
```

## Demo script (2-3 minutes)

1. "Fake volume makes tokens look popular. Rows can't see it; a graph can."
2. Show the token table: MOONX has suspiciously high volume.
3. Click Investigate and walk through the tool steps on the left.
4. Show the evidence graph: tight ring, one shared funder.
5. Run NOVA to show it does not cry wolf.
6. Open FalkorDB Browser and show the `Case` node the agent wrote.

## Troubleshooting

- `Connection refused`: FalkorDB is not running. Run `docker compose up -d`.
- `Unknown procedure algo.pageRank`: harmless; the agent falls back to degree ranking.
- Empty token table: run `python seed.py` first.
- Port 6379 taken by another Redis: change the left side of the port mapping in `docker-compose.yml` and set `FALKORDB_PORT`.
- Reset between demo runs: click **Reset cases** in the UI.
