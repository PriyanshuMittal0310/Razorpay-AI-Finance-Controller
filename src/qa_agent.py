import os
import time
from collections import deque
import duckdb
import polars as pl
from google import genai
from google.genai import types
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()
from config import QA_AGENT_MODEL

GEMINI_MODEL = QA_AGENT_MODEL  # confirmed working for this project/key
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY not set in environment (check .env is loaded from the right working directory)")
client = genai.Client(api_key=GEMINI_API_KEY)

# ==========================================
# STEP 1: Load the master table into DuckDB
# ==========================================
con = duckdb.connect("data/reconciliation.duckdb")  # persistent file, not :memory:,
                                                       # so Streamlit can reconnect later

master_df = pl.read_parquet("data/master_reconciliation_records.parquet")
con.register("reconciliation_records", master_df)
con.execute("CREATE OR REPLACE TABLE reconciliation_records AS SELECT * FROM reconciliation_records")

print(f"Loaded {len(master_df)} records into DuckDB table 'reconciliation_records'")

# ==========================================
# STEP 2: Give the LLM the REAL schema, not a guess
# ==========================================
schema_info = con.execute("DESCRIBE reconciliation_records").pl()
schema_text = "\n".join(
    f"  {row['column_name']} ({row['column_type']})"
    for row in schema_info.to_dicts()
)

# Also give it the actual distinct values for key categorical columns —
# this is what stops the LLM from inventing status strings that don't exist
distinct_status = con.execute("SELECT DISTINCT status FROM reconciliation_records").pl()["status"].to_list()
distinct_tiers = con.execute("SELECT DISTINCT resolved_at_tier FROM reconciliation_records").pl()["resolved_at_tier"].to_list()

SCHEMA_CONTEXT = f"""
Table: reconciliation_records

Columns:
{schema_text}

Valid values for status: {distinct_status}
Valid values for resolved_at_tier: {distinct_tiers}

Notes:
- record_type is either 'ledger' or 'bank', indicating which side this record is anchored to.
- confidence is a float from 0.0 to 1.0.
- amount_delta is the absolute difference (in currency units) between expected and actual settled amount; NULL where not applicable.
- net_amount can be negative for full-refund cases.
- gross_amount, net_amount, amount_delta are all in INR.
- IMPORTANT: For ledger-side-only records (e.g. status = 'Tier 5: Unsettled/Pending Receivable' or
  'No Settlement Expected'), net_amount is NULL because no bank settlement was ever found — use
  gross_amount instead for these records' monetary value.
- IMPORTANT: For bank-side-only records (e.g. resolved_at_tier = 'Tier 5: Unexplained Bank Credit'),
  gross_amount is NULL since there is no corresponding ledger entry — use net_amount instead.
"""

# ==========================================
# STEP 3: Structured SQL generation (forced JSON, not free text)
# ==========================================
class SQLGeneration(BaseModel):
    sql_query: str
    explanation: str  # what the query does, for transparency

def generate_sql(question: str) -> SQLGeneration:
    prompt = f"""You are a SQL assistant for a payment reconciliation system. Given the schema below,
write a single DuckDB-compatible SELECT query that answers the user's question.

{SCHEMA_CONTEXT}

Rules:
- ONLY generate SELECT statements. Never UPDATE, DELETE, INSERT, DROP, ALTER, or any other statement.
- Use only the exact column names and valid values listed above.
- If the question cannot be answered from this table, return a query that selects nothing
  (e.g. "SELECT 'Question cannot be answered from this data' AS message") rather than guessing.
- Keep the query simple and readable.

User question: {question}
"""
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SQLGeneration,
            ),
        )
        return SQLGeneration.model_validate_json(response.text)
    except Exception as e:
        print(f"    [DEBUG] Full error: {repr(e)}")
        raise

# ==========================================
# STEP 4: Safety gate — never execute anything except a clean SELECT
# ==========================================
FORBIDDEN_KEYWORDS = ["UPDATE", "DELETE", "INSERT", "DROP", "ALTER", "CREATE",
                       "TRUNCATE", "ATTACH", "COPY", "EXPORT", "PRAGMA"]

def is_safe_select(sql: str) -> bool:
    normalized = sql.strip().upper()
    if not normalized.startswith("SELECT") and not normalized.startswith("WITH"):
        return False
    return not any(kw in normalized for kw in FORBIDDEN_KEYWORDS)

# ==========================================
# STEP 5: End-to-end Q&A function with graceful failure
# ==========================================
# Track timestamps of the last N requests to enforce our OWN rate limit,
# proactively, instead of reacting to 429s after the fact.
_request_times = deque(maxlen=5)
RATE_LIMIT_PER_MINUTE = 5
RATE_LIMIT_WINDOW = 60  # seconds

def throttle():
    """Block until it's safe to make another request without exceeding 5/min."""
    now = time.time()
    if len(_request_times) == _request_times.maxlen:
        oldest = _request_times[0]
        elapsed = now - oldest
        if elapsed < RATE_LIMIT_WINDOW:
            wait = RATE_LIMIT_WINDOW - elapsed + 1  # +1s safety margin
            print(f"    Proactively pacing: waiting {wait:.1f}s to stay under {RATE_LIMIT_PER_MINUTE}/min...")
            time.sleep(wait)
    _request_times.append(time.time())


def call_with_retry(fn, max_retries=5, base_delay=15):
    for attempt in range(max_retries):
        throttle()  # proactive check before every single call, not just on retry
        try:
            return fn()
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                if attempt < max_retries - 1:
                    print(f"    Rate limited anyway, waiting {base_delay}s before retry {attempt+2}/{max_retries}...")
                    time.sleep(base_delay)
                    continue
            raise
    raise RuntimeError("Max retries exceeded")

def ask_question(question: str) -> str:
    try:
        gen = call_with_retry(lambda: generate_sql(question))
    except Exception as e:
        return f"Sorry, I couldn't process that question right now ({type(e).__name__}). Try rephrasing it."

    if not is_safe_select(gen.sql_query):
        return "I couldn't generate a safe query for that question. Try rephrasing it as a data lookup."

    try:
        result = con.execute(gen.sql_query).pl()
    except Exception as e:
        return (f"I generated a query but it didn't run successfully "
                f"({type(e).__name__}: {str(e)[:150]}). Try rephrasing the question.")

    if len(result) == 0:
        return "No matching records found for that question."

    summary_prompt = f"""The user asked: "{question}"
The query result (as data) is:
{result.to_pandas().to_string(index=False)}

Give a brief, direct natural-language answer to their question based ONLY on this data.
Do not add any information not present in the result."""

    try:
        summary_response = call_with_retry(
            lambda: client.models.generate_content(model=GEMINI_MODEL, contents=summary_prompt)
        )
        return summary_response.text
    except Exception:
        return f"Here's the result:\n{result}"

# ==========================================
# PRE-TESTED QUESTIONS — verify these work before your demo
# ==========================================
PRETESTED_QUESTIONS = [
    "How much revenue was lost to bank fees across all verified transactions?",
    "How many exceptions are there, broken down by category?",
    "Which tier resolved the most records?",
    "What is the total amount of unsettled receivables?",
    "Show me all records with confidence below 0.5",
]

if __name__ == "__main__":
    print("\n--- Testing pre-verified questions ---")
    for q in PRETESTED_QUESTIONS:
        print(f"\nQ: {q}")
        print(f"A: {ask_question(q)}")
        # throttle() inside call_with_retry already paces requests correctly now,
        # so the manual sleep here is just extra headroom for the demo run
        time.sleep(2)