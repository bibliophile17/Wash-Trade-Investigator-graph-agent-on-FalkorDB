import os

from falkordb import FalkorDB

GRAPH_NAME = os.getenv("GRAPH_NAME", "washtrade")


def get_graph():
    host = os.getenv("FALKORDB_HOST", "localhost").strip()
    port = int(os.getenv("FALKORDB_PORT", "6379"))
    for prefix in ("redis://", "rediss://", "https://", "http://"):
        host = host.replace(prefix, "")
    if ":" in host:
        host, _, p = host.partition(":")
        if p.strip("/").isdigit():
            port = int(p.strip("/"))
    kwargs = {"host": host.strip("/"), "port": port}
    if os.getenv("FALKORDB_USERNAME"):
        kwargs["username"] = os.getenv("FALKORDB_USERNAME")
    if os.getenv("FALKORDB_PASSWORD"):
        kwargs["password"] = os.getenv("FALKORDB_PASSWORD")
    if os.getenv("FALKORDB_SSL") == "1":
        kwargs["ssl"] = True
    return FalkorDB(**kwargs).select_graph(GRAPH_NAME)