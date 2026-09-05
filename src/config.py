"""
Central model configuration for the reconciliation pipeline and Q&A agent.

Maintains a single source of truth for all Gemini model identifiers used across
Tier 0 narrative extraction, Tier 4 AI bundle resolution, and the Settlement Q&A agent.
"""

# Tier 0: LLM narrative extraction (used in 3_reconciliation_pipeline.py)
TIER0_EXTRACTION_MODEL = "gemini-3.6-flash"

# Tier 4: Reasoning LLM for bundle ranking and confirmation (used in reasoning.py)
TIER4_REASONING_MODEL = "gemini-3.6-flash"

# Settlement Q&A Agent: NL-to-SQL query generation and summarization (used in qa_agent.py)
QA_AGENT_MODEL = "gemini-3.5-flash"
