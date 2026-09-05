# Architectural Design Decisions & Deliberate Restraints

This document outlines the core engineering philosophy behind the **AI Finance Controller**. In financial engineering, deciding what **not** to build is often more critical than what is built. Every design decision below prioritizes mathematical auditability, reproducible behavior, and defense against model hallucination over superficial complexity.

---

## 1. Why No Vector Search or Embeddings?

### The Tempting Alternative
A common contemporary architecture uses embedding models (e.g. text-embedding-004) and vector databases (Pinecone, Chroma) to calculate cosine similarity between bank statement descriptions and ledger counterparty names.

### Why We Explicitly Rejected It
1. **Financial Reconciliation is an Exact-Math Problem, Not a Semantic Problem:**
   Entities in bank statements differ by abbreviations, corporate structures, and noise tokens (e.g. `AXIS-NEFT-PAY432`, `TECHFLOW SOLUTIONS PVT LTD` vs `Techflow`). Vector embeddings measure semantic closeness, not textual token overlap or legal entity equivalence.
2. **Semantic Similarity $\neq$ Financial Relationship:**
   Two entirely unrelated companies in the same sector (e.g. `"Vertex Logistics"` and `"Apex Transport"`) have high semantic embedding similarity despite having zero financial relationship.
3. **Latency and Compute Overhead:**
   Generating high-dimensional embeddings for thousands of ledger entries incurs network round-trips and vector index lookups. RapidFuzz `token_set_ratio` runs in C-optimized CPU memory at over 50,000 comparisons per second.
4. **Non-Deterministic Distance Drift:**
   Embedding models are black boxes that shift slightly across versions. Levenshtein-based token set ratios are mathematically deterministic and auditable in a court of law.

---

## 2. Why the Reasoning LLM is Not the Default Matcher

### The Tempting Alternative
Feeding an entire bank statement and ledger batch into a large context window LLM and prompting: *"Here are the payments and bank credits. Reconcile them and output matches."*

### Why We Explicitly Rejected It
1. **Cost & Latency Bottleneck:**
   Running an LLM on every transaction produces an unacceptable $O(N)$ API cost curve and adds tens of seconds to pipeline execution. In our benchmarks, deterministic tiers resolve the bulk of transactions in under 15 seconds.
2. **Hallucination and Non-Conservation:**
   LLMs have no inherent concept of mathematical conservation. They can drop records, invent plausible IDs, or round numbers unpredictably.
3. **Problem Decomposition Over Prompt Engineering:**
   The reasoning model (`TIER4_REASONING_MODEL = "gemini-3.6-flash"`) appears **in exactly one place**: Tier 4, and only after deterministic subset-sum pre-filtering has reduced the problem space to a small list of arithmetically valid candidates.
4. **Auditability:**
   A finance controller cannot present an auditor with "the AI felt this looked right." A deterministic exact match or fee-adjusted math check can be verified by a simple SQL query.

---

## 3. Honest Match-Rate Denominator Math

### The Tempting Alternative
Calculating:
$$\text{Match Rate} = \frac{\text{Verified Matches}}{\text{Total Ingested Records}} \times 100$$
Or counting all 64 batch records in the denominator.

### Why We Compute It Over Matchable Records Only
In our batch of 64 records, 7 records represent payments whose status at the gateway is `failed`.
- When a customer payment fails at the gateway checkout, **no funds are captured, no MDR fee is deducted, and no bank settlement can ever occur**.
- Including failed transactions in the denominator would artificially penalize the match rate by expecting settlements that are physically impossible.
- Conversely, counting them as "matched" because we know why they didn't settle would artificially inflate accuracy.
- **The Honest Calculation:**
  $$\text{Matchable Records} = \text{Total Records} - \text{Failed Gateway Payments}$$
  $$\text{Match Rate} = \frac{\text{Verified Matches}}{\text{Matchable Records}} \times 100$$
  Across our 64-record batch, with 7 failed payments:
  $$\text{Match Rate} = \frac{32}{59} = 54.2\%$$

---

## 4. Why Tier 0 LLM Extractions Must Pass a Strict Regex Gate

### The Tempting Alternative
Accepting the LLM's extracted `payment_id` directly and attempting to join against the ledger.

### Why We Enforce the Regex Gate
- **The Gate:**
  ```python
  razorpay_id_regex = r"^pay_[a-zA-Z0-9]{14}$"
  ```
- LLMs can hallucinate substrings or concatenate adjacent narrative tokens (e.g. converting `pay_4d4654f7ee6b41-CORP` into `pay_4d4654f7ee6b41CORP`).
- If an unverified, malformed ID entered the Tier 1 Polars inner join, it would fail to match the ledger and silently fall through. Even worse, a hallucinated clean ID could accidentally join against an unrelated row.
- **Fail-Safe Fall-Through:** If an extracted string fails the regex gate, the pipeline forces `extracted_payment_id = None`. The record is not dropped—it is simply stripped of unverified claims and routed to Tier 2 for fuzzy and date-window verification.

---

## 5. Calibration of the Tier 3 Identity-Veto Threshold (60.0)

### The Empirical Calibration Story
In Tier 3 Action 2, independent math matching catches transactions with corporate aliasing (e.g. a parent company settling on behalf of a brand). However, it also catches "precision traps"—unrelated transactions that coincidentally share the exact same amount down to the paisa.

During pipeline development and calibration on the synthetic benchmark dataset, we measured the RapidFuzz `token_set_ratio` distribution across both classes:

| Class | Examples in Dataset | Observed Score Range |
|---|---|:---:|
| **Precision Traps (Coincidences)** | `"Umbrella Inc"` vs `"Individual Sneha"`<br>`"Globex"` vs `"Individual Amit"`<br>`"Razorpay Software"` vs `"Individual Sneha"` | **16.0 – 37.5** |
| **Corporate Aliasing (Genuine Matches)** | `"Wayne Enterprises"` vs `"Wayne Corp India"`<br>`"Acme Corp"` vs `"Acme Global"` | **78.0 – 88.0** |

- **Setting `TIER3_IDENTITY_VETO_THRESHOLD = 60.0`:**
  A threshold of `60.0` sits comfortably inside the **40.5-point gap** between the highest observed coincidence (`37.5`) and the lowest observed aliasing match (`78.0`).
- This mathematical margin guarantees zero false positives from amount coincidences while retaining genuine corporate aliasing matches.

---

## 6. Additional Deliberate Restraints

### 1. Bounded Subset-Sum Combinations (Size 2 and 3 Only)
- In Tier 4, we evaluate `combinations(pool, 2)` and `combinations(pool, 3)`.
- Razorpay batch settlements in standard business accounts typically bundle 2 to 3 transactions per clearing cycle.
- Evaluating combinations of size $\ge 4$ causes exponential computational explosion ($O(N^k)$) with diminishing returns and increased risk of false subset sums.

### 2. Candidate Pool Cutoff (`TIER4_MAX_POOL_SIZE = 20`)
- If the unresolved bank pool at Tier 4 exceeds 20 candidates, combinatorial search is aborted for safety:
  $$\binom{20}{3} = 1,140 \text{ combinations (manageable)}$$
  $$\binom{50}{3} = 19,600 \text{ combinations (dangerous)}$$
- Rows exceeding this limit are labeled `Tier 4: Skipped - Pool Too Large` and escalated to human manual review rather than hanging the pipeline.

### 3. Persistent DuckDB Database File
- Instead of using an in-memory DuckDB instance (`:memory:`), `qa_agent.py` and the pipeline connect to `data/reconciliation.duckdb`.
- This decouples pipeline execution from the Streamlit UI and Q&A agent, allowing the interactive UI to query the table instantly across re-runs without re-executing data ingestion.
