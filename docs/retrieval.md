# Retrieval plan (ideal, concrete & deterministic)

## Environment toggles (add these to your runtime)

* `ENABLE_METADATA_CHUNKS` (`true` / `false`) — default: `false`
  When `true` run a payload keyword search (Qdrant MatchText) and include that ranked list in first-stage RRF. Controlled size: `MAX_METADATA_CHUNKS`.
* `MAX_METADATA_CHUNKS` (integer) — default: `50`
  Max results to return from payload/metadata keyword search.
* `ENABLE_CROSS_ENCODER` (`true` / `false`) — default: `true`
  If `true` run cross-encoder (Serve) on top candidates and use its scores to re-rank before selecting LLM context. If `false` skip cross-encoder and pass deduped ordered chunks to LLM.

(You may also keep the other envs we discussed: `TOP_VECTOR_CHUNKS`, `TOP_BM25_CHUNKS`, `FIRST_STAGE_RRF_K`, `MAX_CHUNKS_FOR_GRAPH_EXPANSION`, `GRAPH_EXPANSION_HOPS`, `SECOND_STAGE_RRF_K`, `MAX_CHUNKS_TO_CROSSENCODER`, `MAX_CHUNKS_TO_LLM`, `INFERENCE_EMBEDDER_MAX_TOKENS`, `CROSS_ENCODER_MAX_TOKENS` etc.)

---

## High-level summary (1 sentence)

Embed query → fetch candidate lists (vectors, BM25, optional metadata) → **first-stage RRF** fuse → dedupe → seed graph expansion → assemble candidate details → **second-stage RRF** fuse → optional cross-encoder re-rank → choose final top chunks for LLM.

---

## Step-by-step concrete workflow

### 0) Pre-reqs / constants (recommended defaults)

* `TOP_VECTOR_CHUNKS = 200` (ANN fetch window)
* `TOP_BM25_CHUNKS = 100`
* `ENABLE_METADATA_CHUNKS = true|false`
* `MAX_METADATA_CHUNKS = 50` (only used if `ENABLE_METADATA_CHUNKS=true`)
* `FIRST_STAGE_RRF_K = 60`
* `MAX_CHUNKS_FOR_GRAPH_EXPANSION = 20`
* `GRAPH_EXPANSION_HOPS = 1`
* `SECOND_STAGE_RRF_K = 60`
* `ENABLE_CROSS_ENCODER = true|false` 
* `MAX_CHUNKS_TO_CROSSENCODER = 64 (only used if `ENABLE_CROSS_ENCODER = true`)`
* `MAX_CHUNKS_TO_LLM = 8`


### 1) Embed query (single call)

* Call embedder with `INFERENCE_EMBEDDER_MAX_TOKENS` (e.g. 60).
* Produce `q_vec` (normalized).

### 2) Retrieve raw ranked lists (parallel)

Run these **in parallel** to minimize latency:

A. **Vector ANN fetch** (Qdrant) — get top `TOP_VECTOR_CHUNKS` candidate IDs & (preferably) their stored vectors.

* Immediately **fetch vectors** for those candidates and compute exact cosine similarity locally with `q_vec` (this yields the *bi-encoder rescored vector ranking* — important step).

B. **Neo4j fulltext (BM25-style)** — `CALL db.index.fulltext.queryNodes(...)` to return top `TOP_BM25_CHUNKS` candidate chunk IDs with Lucene scores.

C. **Optional: payload keyword search (metadata)** — *only if* `ENABLE_METADATA_CHUNKS=true`: run Qdrant payload MatchText/MATCH on `text` (or other metadata) and return up to `MAX_METADATA_CHUNKS`. This is treated as a third ranked list (fall-back/metadata).

Outputs: three ordered lists of chunk IDs with scores:

* `vec_rank` (by exact cosine on fetched vectors)
* `bm25_rank` (Neo4j Lucene scores)
* `kw_rank` (optional metadata/payload search)

> Note: **Do not** use raw ANN distance ordering for RRF — always do local rescoring (cosine) on the ANN window.

### 3) First-stage fusion (RRF)

* Input: `vec_rank`, `bm25_rank`, optional `kw_rank`.
* Use Reciprocal Rank Fusion: for each ranked list, for an item at rank `r` add score `1/(k + r)` to that item, with `k = FIRST_STAGE_RRF_K`. Sum across lists.
* Sort by fused score descending → `fused_list`.

### 4) Deduplicate fused list (important)

* Remove duplicate chunk IDs while keeping **first occurrence order** (stable). Call this `deduped_fused`.
* Rationale: avoid expanding same chunk multiple times and keep highest-first-stage precedence.

### 5) Choose seeds and Graph expansion

* Seeds = `deduped_fused[:MAX_CHUNKS_FOR_GRAPH_EXPANSION]`.
* Run graph expansion in Neo4j for `GRAPH_EXPANSION_HOPS` hops. **Expansion logic:** for each seed chunk, collect neighbor chunks according to relationships you care about (Document→Chunk neighbors, citations, same-author, etc.). Limit per-seed neighbors (e.g. `max_additional_per_start = RERANK_TOP`).
* Add expanded neighbor chunk IDs to pool, preserving uniqueness. Call result `combined_unique_candidates`.

### 6) Assemble detailed candidate records

For each chunk in `combined_unique_candidates`:

* Fetch payload (text, document_id, token_count) and vector from Qdrant (`with_vector=True`) — this provides text + vector + any metadata.
* If BM25 produced a score for that chunk, include it in a `bm25_map`. If not present, treat bm25=0.
* If metadata kw list produced score, include in `kw_map`.

You now have for each candidate: `{chunk_id, text, vector, bm25_score, kw_score, doc_id, token_count}`.

### 7) Compute fresh vector similarity ranking

* Compute exact cosine similarity between `q_vec` and each candidate vector. Produce `vec_map` and `vec_rank2` (descending).

### 8) Second-stage fusion (RRF) over assembled candidates

* Build ranked lists over the candidate pool:

  * `vec_rank2` (by computed cosine)
  * `bm25_rank2` (by `bm25_map`)
  * `kw_rank2` (if enabled, by `kw_map`)
  * Optionally, `graph_rank` (score by number-of-seed-connections or inverse distance)
* Fuse with RRF with `SECOND_STAGE_RRF_K`. Sort -> `final_fused_order`.
* Deduplicate again (stable).

### 9) Cross-encoder re-ranking (conditional)

* If `ENABLE_CROSS_ENCODER=true`:

  * Take top `MAX_CHUNKS_TO_CROSSENCODER` from `final_fused_order` (or fewer if list shorter).
  * Call cross-encoder with `max_length = CROSS_ENCODER_MAX_TOKENS` (truncate/pad as needed). Provide pairs `(query, chunk_text)`.
  * Receive dense relevance scores; combine with second-stage fused scores: e.g. `combined_score = w_cross * cross_score + (1 - w_cross) * base_score` (suggest `w_cross=0.8` by default). Sort by `combined_score`.
* If `ENABLE_CROSS_ENCODER=false`:

  * Skip cross-encoder and use `final_fused_order` directly.

### 10) Final selection → LLM prompt assembly

* Choose top `MAX_CHUNKS_TO_LLM` chunks after cross-encoder (or after final fused if cross off).
* **Optional token budget enforcement**: iterate chunks in order, accumulate tokens (use `token_count` from payload or estimate via tokenizer) and stop when `SUM(tokens) + estimated_prompt_tokens >= MAX_PROMPT_TOKENS`. This prevents wasted compute & exceeds LLM limits.
* Assemble prompt using selected chunks, include provenance (document_id, chunk_id, score).

---
# Hybrid-RAGOps-AWS
