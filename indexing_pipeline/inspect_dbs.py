#!/usr/bin/env python3
import os
import json
from neo4j import GraphDatabase

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

QDRANT_URL = os.getenv("QDRANT_URL")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
COLLECTION = os.getenv("QDRANT_COLLECTION", "my_collection")

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")


# ----------------------------------------------------------------------
# UNIVERSAL QDRANT SCROLLER (FINAL VERSION)
# ----------------------------------------------------------------------

def safe_scroll_all(client, collection: str):
    """
    Fully version-safe Qdrant scroll reader.
    Supports:
      - NEW SDK: returns list[dict]
      - OLD SDK: returns (points, next_offset)
      - HYBRID: object with .points / .next_page_offset
    Returns dict: {point_id: payload}
    """
    points_out = {}
    offset = None

    while True:
        try:
            result = client.scroll(
                collection_name=collection,
                limit=200,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as e:
            print("qdrant scroll failed:", e)
            return points_out

        # ------------------------------
        # Case 1: New SDK (~1.10+) → list returned, not tuple
        # ------------------------------
        if isinstance(result, list):
            for p in result:
                pid = str(p.get("id"))
                payload = p.get("payload", {})
                points_out[pid] = payload
            break

        # ------------------------------
        # Case 2: Old SDK → (points, next_offset)
        # ------------------------------
        if isinstance(result, tuple) and len(result) == 2:
            pts, next_offset = result
            for p in pts:
                if isinstance(p, dict):
                    pid = str(p.get("id"))
                    payload = p.get("payload", {})
                else:
                    pid = str(getattr(p, "id"))
                    payload = getattr(p, "payload", {})
                points_out[pid] = payload

            if next_offset is None:
                break

            offset = next_offset
            continue

        # ------------------------------
        # Case 3: Namespace / Struct formats
        # ------------------------------
        try:
            pts = result.points
            next_offset = getattr(result, "next_page_offset", None)

            for p in pts:
                if isinstance(p, dict):
                    pid = str(p.get("id"))
                    payload = p.get("payload", {})
                else:
                    pid = str(getattr(p, "id"))
                    payload = getattr(p, "payload", {})
                points_out[pid] = payload

            if not next_offset:
                break

            offset = next_offset
            continue

        except Exception:
            print("qdrant scroll unsupported format:", type(result))
            break

    return points_out


# ----------------------------------------------------------------------
# QDRANT CHECK
# ----------------------------------------------------------------------

def check_qdrant():
    try:
        from qdrant_client import QdrantClient
    except Exception:
        print("Missing qdrant-client (pip install qdrant-client)")
        return None

    try:
        client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    except Exception as e:
        print("Failed connecting Qdrant:", e)
        return None

    try:
        col_info = client.get_collection(collection_name=COLLECTION)
    except Exception as e:
        print("get_collection failed:", e)
        col_info = None

    points = safe_scroll_all(client, COLLECTION)

    return {
        "info": col_info,
        "points": points,
        "count": len(points),
    }


# ----------------------------------------------------------------------
# NEO4J CHECK
# ----------------------------------------------------------------------

def check_neo4j():
    drv = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    q = """
    MATCH (c:Chunk)
    RETURN c.chunk_id AS chunk_id, c.qdrant_id AS qdrant_id
    """
    rows = []
    with drv.session() as s:
        for r in s.run(q):
            rows.append((r["chunk_id"], r["qdrant_id"]))
    return rows


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

if __name__ == "__main__":

    print("=== Neo4j summary ===")
    neo = check_neo4j()
    print("Chunk nodes:", len(neo))
    print("Sample:", neo[:5])

    print("\n=== Qdrant summary ===")
    qd = check_qdrant()
    if qd:
        print("Collection info:", qd["info"])
        print("Points discovered:", qd["count"])
        sample_ids = list(qd["points"].keys())[:5]
        print("Sample IDs:", sample_ids)
        for sid in sample_ids:
            print(f" {sid} -> {qd['points'][sid]}")

    # ------------------------------
    # Cross-check
    # ------------------------------
    print("\n=== Cross-checks ===")

    qdr_ids = set(qd["points"].keys()) if qd else set()
    neo_ids = set([q for _, q in neo if q is not None])

    missing = sorted(neo_ids - qdr_ids)
    extra = sorted(qdr_ids - neo_ids)

    print("Neo4j chunks:", len(neo_ids))
    print("Qdrant points:", len(qdr_ids))
    print("Missing in Qdrant:", len(missing))
    print("Extra in Qdrant:", len(extra))
    print("Missing sample:", missing[:10])

    print("\nDone.")
