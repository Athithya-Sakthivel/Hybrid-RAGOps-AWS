




Compact runtime flow — step-by-step (what runs when a query arrives):

1. **Embed the query (1 call)**

   * Call the embed model (Serve handle) → single vector `q_vec`.
   * Cost: model latency (tens–hundreds ms).
   * Example: `q_vec = embed_text("How to install...")`.

2. **Retrieve raw ranked lists from 3 sources (parallel where possible)**
   a. **Vector search (Qdrant)** — top `TOP_VECTOR_CHUNKS` by nearest-vector score.
   b. **Neo4j full-text (BM25-style)** — top `TOP_BM25_CHUNKS` by Lucene score.
   c. **Payload keyword search (Qdrant fallback)** — top `TOP_BM25_CHUNKS` matching `text` field.

   * Each returns an ordered list of chunk IDs + score + text payload.
   * Cost: Qdrant RPC + Neo4j fulltext call.

3. **First-stage fusion (RRF)**

   * Fuse the three ranked lists using Reciprocal Rank Fusion (or similar).
   * Produces a single fused ranked list of chunk IDs (higher fused score = better).

4. **Deduplicate fused list**

   * Remove duplicate chunk IDs while preserving fused order.
   * Rationale: avoid expanding the same chunk twice and limit graph cost.

5. **Select seeds for graph expansion**

   * Take top `MAX_CHUNKS_FOR_GRAPH_EXPANSION` of the deduped fused list.
   * Run graph expansion in Neo4j for `GRAPH_EXPANSION_HOPS` hops to collect neighbor chunks (e.g., same Document neighbors, citation edges, metadata links).
   * Add expanded chunk IDs to the candidate pool (keeping uniqueness).

6. **Assemble detailed records**

   * For the combined unique candidate IDs, fetch text + vector (from Qdrant) and bm25/kw scores (maps from step 2).
   * Compute fresh **vector similarity** (cosine) between `q_vec` and each candidate vector if needed.

7. **Second-stage fusion (RRF)**

   * Build ranked lists (vector ranking, BM25 ranking, keyword ranking) over the assembled candidates.
   * Fuse with RRF (`SECOND_STAGE_RRF_K`) → new fused ranking.
   * Deduplicate again (preserve order).

8. **Optional cross-encoder re-ranking**

   * Take top `MAX_CHUNKS_TO_CROSSENCODER` (from second-stage fused list), run the cross-encoder (Serve handle) to produce dense relevance scores.
   * Combine cross-encoder scores with base fused scores (weighted) and re-sort.

9. **Final selection & LLM prompt**

   * Choose top `MAX_CHUNKS_TO_LLM` chunks as context for the LLM.
   * Build prompt with context pieces + provenance (document_id, chunk_id, score).
   * Return prompt and provenance to caller.

10. **Return / consume**

* The application or LLM consumes the prompt; provenance used for citations or follow-ups.

---

Performance hotspots & tuning (compact):

* **Embedding latency** — reduce embed sub-batch size or use faster model.
* **Qdrant vector search** — tune `TOP_VECTOR_CHUNKS` and index config (HNSW ef).
* **Neo4j fulltext** — ensure fulltext index exists; tune queries and limit `TOP_BM25_CHUNKS`.
* **Graph expansion** — expensive if many seeds or many hops; limit with `MAX_CHUNKS_FOR_GRAPH_EXPANSION` and per-seed limits.
* **Cross-encoder** — costly; keep `MAX_CHUNKS_TO_CROSSENCODER` modest (e.g., 32–64).

Short example with numbers:

* `TOP_VECTOR_CHUNKS=100`, `TOP_BM25_CHUNKS=100`, fuse → take top 60 (`FIRST_STAGE_RRF_K=60`), dedupe → expand max 20 seeds with 1 hop → assemble ~120 candidates → second RRF → keep 64 for cross-encoder → final 8 chunks to LLM.

Why dedupe **before** graph expansion?

* Prevents redundant expansion work and reduces graph traversal cost while keeping the strongest unique seeds.

Why RRF early + later?

* **First RRF** fuses broad signals to pick good, diverse seed chunks for graph expansion. 
* **Second RRF** re-fuses after expansion and exact vector scores to refine rankings before the expensive cross-encoder step.


