import streamlit as st
import polars as pl
import plotly.express as px
import plotly.graph_objects as go
import os
import sys
import json

st.set_page_config(page_title="Reconciliation Ledger", layout="wide", page_icon="▤", initial_sidebar_state="collapsed")

current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)
sys.path.append(os.path.dirname(current_dir))

try:
    sys.path.insert(0, os.path.join(os.path.dirname(current_dir), "src"))
    from qa_agent import ask_question
    from forecaster import (
        forecast_unsettled_receivables,
        aggregate_cash_forecast_by_bucket,
        compute_historical_lags,
        ALL_BUCKETS,
    )
except Exception as e:
    ask_question = None
    forecast_unsettled_receivables = None
    aggregate_cash_forecast_by_bucket = None
    compute_historical_lags = None
    ALL_BUCKETS = ["Next 7 Days", "8-14 Days", "15-30 Days", "30+ Days / Overdue"]
    st.error(f"Failed to load modules: {type(e).__name__}: {e}")

# ==========================================
# DESIGN TOKENS -- warm, professional palette
# (cream paper background, warm charcoal text, amber/terracotta accent,
#  sage for verified/positive, muted rust for exceptions)
# ==========================================
PAPER = "#FBF8F3"
CARD = "#FFFFFF"
INK = "#2A231C"
MUTED = "#8A7D6C"
BORDER = "#E8E0D4"
ACCENT = "#B45309"        # warm amber/terracotta -- primary accent
ACCENT_SOFT = "#FBEEE0"
SAGE = "#4D7C63"          # verified / positive
RUST = "#B7472A"          # exceptions / warnings
STONE = "#A99C89"         # neutral secondary

# CRITICAL: every rule below sets BOTH background and text color together and
# uses !important. This makes the page immune to a viewer's personal Streamlit
# dark-mode preference (Settings -> Theme), which can override config.toml on
# its own and is what caused the previous "invisible dark-on-dark text" bug --
# text color was set without also pinning the background it sits on.
st.markdown(f"""
<link href="https://fonts.googleapis.com/css2?family=Fraunces:wght@500;600;700&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
    html, body, [class*="css"], .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {{
        font-family: 'Inter', sans-serif !important;
        background-color: {PAPER} !important;
        color: {INK} !important;
    }}
    [data-testid="stHeader"] {{ background-color: {PAPER} !important; }}
    #MainMenu, [data-testid="stSidebar"], [data-testid="collapsedControl"], footer {{ display: none !important; }}
    .block-container {{ padding-top: 2rem !important; max-width: 1400px; }}

    p, span, div, label, li {{ color: {INK}; font-size: 15.5px; }}
    [data-testid="stCaptionContainer"] p {{ font-size: 14.5px !important; }}

    .masthead {{
        display: flex; align-items: baseline; justify-content: space-between;
        padding: 8px 0 20px 0; border-bottom: 2px solid {INK}; margin-bottom: 24px;
    }}
    .masthead-title {{ font-family: 'Fraunces', serif; font-size: 36px; font-weight: 600; color: {INK} !important; letter-spacing: -0.01em; }}
    .masthead-title span {{ color: {ACCENT} !important; }}
    .masthead-sub {{ font-family: 'IBM Plex Mono', monospace; font-size: 13.5px; color: {MUTED} !important; text-transform: uppercase; letter-spacing: 0.08em; margin-top: 8px; }}
    .masthead-meta {{ font-family: 'IBM Plex Mono', monospace; font-size: 13px; color: {MUTED} !important; text-align: right; line-height: 1.8; }}

    .kpi-row {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 14px; margin-bottom: 32px; }}
    .kpi-cell {{ background-color: {CARD} !important; border: 1px solid {BORDER}; border-radius: 14px; padding: 24px 24px; box-shadow: 0 1px 3px rgba(42,35,28,0.05); }}
    .kpi-label {{ font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.08em; color: {MUTED} !important; margin-bottom: 10px; }}
    .kpi-value {{ font-family: 'IBM Plex Mono', monospace; font-size: 30px; font-weight: 600; color: {INK} !important; line-height: 1.1; }}
    .kpi-value.sage {{ color: {SAGE} !important; }}
    .kpi-value.rust {{ color: {RUST} !important; }}
    .kpi-value.accent {{ color: {ACCENT} !important; }}

    .section-eyebrow {{ font-family: 'IBM Plex Mono', monospace; font-size: 12px; text-transform: uppercase; letter-spacing: 0.09em; color: {ACCENT} !important; margin-bottom: 6px; }}
    .section-title {{ font-family: 'Fraunces', serif; font-size: 23px; font-weight: 600; color: {INK} !important; margin-bottom: 18px; }}

    .ledger-card {{ background-color: {CARD} !important; border: 1px solid {BORDER}; border-radius: 14px; padding: 22px 24px; }}
    .tier-row {{ display: flex; align-items: center; padding: 12px 0; border-bottom: 1px solid {BORDER}; }}
    .tier-row:last-child {{ border-bottom: none; }}
    .tier-badge {{ font-family: 'IBM Plex Mono', monospace; font-size: 12px; font-weight: 600; color: {ACCENT} !important; background: {ACCENT_SOFT} !important; border-radius: 6px; padding: 4px 9px; margin-right: 14px; white-space: nowrap; min-width: 46px; text-align: center; }}
    .tier-label {{ flex: 1; font-size: 15px; color: {INK} !important; }}
    .tier-count {{ font-family: 'IBM Plex Mono', monospace; font-size: 16px; font-weight: 600; color: {INK} !important; margin-left: 12px; min-width: 32px; text-align: right; }}

    /* ---- Pipeline Performance Panel ---- */
    .perf-card {{
        background-color: {CARD} !important;
        border: 1px solid {BORDER};
        border-radius: 14px;
        padding: 20px 24px;
        margin-bottom: 28px;
        box-shadow: 0 1px 3px rgba(42,35,28,0.05);
    }}
    .perf-header {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 16px;
    }}
    .perf-badge {{
        font-family: 'IBM Plex Mono', monospace;
        font-size: 13px;
        font-weight: 600;
        color: {SAGE} !important;
        background-color: #EBF3EE !important;
        border: 1px solid #D0E2D7;
        padding: 5px 12px;
        border-radius: 20px;
    }}
    .perf-grid {{
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 14px;
        margin-bottom: 18px;
    }}
    .perf-cell {{
        background: {PAPER};
        border: 1px solid {BORDER};
        border-radius: 10px;
        padding: 14px 16px;
    }}
    .perf-label {{
        font-family: 'IBM Plex Mono', monospace;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: {MUTED} !important;
        margin-bottom: 6px;
    }}
    .perf-val {{
        font-family: 'IBM Plex Mono', monospace;
        font-size: 22px;
        font-weight: 600;
        color: {INK} !important;
        line-height: 1.1;
    }}
    .perf-val.accent {{ color: {ACCENT} !important; }}
    .perf-val.sage {{ color: {SAGE} !important; }}
    .perf-val.rust {{ color: {RUST} !important; }}
    .perf-sub {{
        font-family: 'IBM Plex Mono', monospace;
        font-size: 12px;
        color: {MUTED} !important;
        margin-top: 4px;
    }}
    .perf-bar-wrap {{
        margin-top: 10px;
    }}
    .perf-bar-track {{
        display: flex;
        height: 14px;
        border-radius: 7px;
        overflow: hidden;
        background-color: {BORDER};
        border: 1px solid {BORDER};
    }}
    .perf-bar-det {{
        background-color: {ACCENT};
        height: 100%;
    }}
    .perf-bar-llm {{
        background-color: {RUST};
        height: 100%;
    }}
    .perf-legend {{
        display: flex;
        justify-content: space-between;
        font-family: 'IBM Plex Mono', monospace;
        font-size: 12px;
        color: {MUTED} !important;
        margin-top: 8px;
    }}
    .legend-item {{
        display: inline-flex;
        align-items: center;
        gap: 6px;
    }}
    .dot-det {{
        width: 10px;
        height: 10px;
        border-radius: 50%;
        background-color: {ACCENT};
        display: inline-block;
    }}
    .dot-llm {{
        width: 10px;
        height: 10px;
        border-radius: 50%;
        background-color: {RUST};
        display: inline-block;
    }}

    /* ---- Custom pill tab-bar (replaces st.tabs) ---- */
    div[data-testid="stRadio"] > div[role="radiogroup"] {{
        gap: 32px !important; border-bottom: 1px solid {BORDER} !important;
        padding-bottom: 0 !important; margin-bottom: 8px !important;
    }}
    div[data-testid="stRadio"] label {{
        padding: 10px 2px 14px 2px !important;
    }}
    div[data-testid="stRadio"] label > div:first-child {{
        display: none !important;  /* hide the native radio circle */
    }}
    div[data-testid="stRadio"] label p {{
        font-family: 'Inter', sans-serif !important;
        font-size: 19px !important; font-weight: 600 !important;
        color: {MUTED} !important;
    }}
    div[data-testid="stRadio"] label[data-baseweb="radio"] input:checked ~ div p,
    div[data-testid="stRadio"] label:has(input:checked) p {{
        color: {ACCENT} !important;
        border-bottom: 3px solid {ACCENT};
        padding-bottom: 10px;
    }}

    /* ---- Cards Streamlit renders natively: dataframe, selectbox, chat, buttons ---- */
    [data-testid="stDataFrame"], [data-testid="stTable"] {{ background-color: {CARD} !important; border-radius: 10px; }}
    .stSelectbox [data-baseweb="select"] > div {{ background-color: {CARD} !important; border-color: {BORDER} !important; color: {INK} !important; }}
    [data-testid="stChatMessage"] {{ background-color: {CARD} !important; border: 1px solid {BORDER} !important; border-radius: 10px !important; padding: 14px 16px !important; }}
    [data-testid="stChatMessage"] * {{ color: {INK} !important; font-size: 15.5px !important; }}

    [data-testid="stBottom"], [data-testid="stBottomBlockContainer"],
    div[data-testid="stChatInput"], .stChatInputContainer,
    [data-testid="stChatInput"] > div {{
        background-color: {PAPER} !important;
    }}
    [data-testid="stChatInput"] textarea {{
        background-color: {CARD} !important; color: {INK} !important;
        border: 1px solid {BORDER} !important; font-size: 15px !important;
    }}
    [data-testid="stChatInput"] button {{ background-color: {ACCENT} !important; }}
    [data-testid="stChatInput"] button svg {{ fill: #FFFFFF !important; }}
    .stButton button, .stDownloadButton button {{
        background-color: {ACCENT} !important; color: #FFFFFF !important; border: none !important;
        border-radius: 8px !important; font-weight: 600 !important;
    }}
    .stRadio [role="radiogroup"] label {{ color: {INK} !important; }}
    [data-testid="stCaptionContainer"], .stCaption {{ color: {MUTED} !important; }}
</style>
""", unsafe_allow_html=True)


@st.cache_data(ttl=5)
def load_data():
    df = pl.read_parquet("data/master_reconciliation_records.parquet")
    df = df.with_columns(pl.col("amount_delta").fill_null(0.0))
    return df

@st.cache_data(ttl=5)
def load_raw_datasets():
    out = {}
    for label, fname in [
        ("Merchant Ledger", "data/merchant_ledger.csv"),
        ("Bank Settlement", "data/bank_settlement.csv"),
    ]:
        if os.path.exists(fname):
            out[label] = pl.read_csv(fname, try_parse_dates=True)
    return out

@st.cache_data(ttl=5)
def load_pipeline_timing():
    for fname in ["data/pipeline_timing.json", "src/data/pipeline_timing.json"]:
        if os.path.exists(fname):
            try:
                with open(fname, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return None

master_df = load_data()
raw_datasets = load_raw_datasets()
timing_data = load_pipeline_timing()

total_records = len(master_df)
verified_df = master_df.filter(pl.col("status") == "Verified")
total_verified = len(verified_df)
total_exceptions = len(master_df.filter(pl.col("status") == "Exception"))
total_no_settlement = len(master_df.filter(pl.col("status") == "No Settlement Expected"))
verified_gross = verified_df["gross_amount"].sum() if "gross_amount" in verified_df.columns else 0.0
matchable_records = total_records - total_no_settlement
match_rate = (total_verified / matchable_records) * 100 if matchable_records > 0 else 0

TIER_ORDER = [
    "Tier 1: Exact",
    "Tier 1: Exact ID, Amount Mismatch",
    "Tier 2: Fuzzy",
    "Tier 3: Fuzzy+Math Verified",
    "Tier 3: Math-Only",
    "Exception: Math Match - Identity Mismatch",
    "Tier 4: AI Bundle",
    "Tier 5: No Settlement Expected",
    "Tier 5: Unsettled/Pending Receivable",
    "Tier 5: Unexplained Bank Credit",
]

# ==========================================
# MASTHEAD
# ==========================================
st.markdown(f"""
<div class="masthead">
    <div>
        <div class="masthead-title">▤ Reconciliation <span>Ledger</span></div>
        <div class="masthead-sub">Tiered settlement matching &nbsp;·&nbsp; AI Finance Controller</div>
    </div>
    <div class="masthead-meta">
        BATCH SIZE&nbsp;&nbsp;{total_records} RECORDS<br>
        MATCH RATE&nbsp;&nbsp;{match_rate:.1f}%
    </div>
</div>
""", unsafe_allow_html=True)

# ==========================================
# KPI ROW
# ==========================================
st.markdown(f"""
<div class="kpi-row">
    <div class="kpi-cell"><div class="kpi-label">Total Records</div><div class="kpi-value">{total_records:,}</div></div>
    <div class="kpi-cell"><div class="kpi-label">Verified Gross</div><div class="kpi-value">₹{verified_gross:,.2f}</div></div>
    <div class="kpi-cell"><div class="kpi-label">Verified Count</div><div class="kpi-value sage">{total_verified:,}</div></div>
    <div class="kpi-cell"><div class="kpi-label">Exceptions</div><div class="kpi-value rust">{total_exceptions:,}</div></div>
    <div class="kpi-cell"><div class="kpi-label">Match Rate</div><div class="kpi-value accent">{match_rate:.1f}%</div></div>
</div>
""", unsafe_allow_html=True)

nav_choice = st.radio(
    "Navigate", ["Dashboard", "Audit Trail", "Cash Forecast", "Settlement Q&A", "Datasets"],
    horizontal=True, label_visibility="collapsed", key="main_nav",
)
st.write("")  # small spacer so content doesn't hug the nav bar's bottom border

# ==========================================
# TAB 1 -- DASHBOARD
# ==========================================
if nav_choice == "Dashboard":
    # Pipeline Performance Panel (if timing metrics exist)
    if timing_data:
        tot_dur = timing_data.get("total_duration_sec", 0.0)
        thr = timing_data.get("throughput_records_per_sec", 0.0)
        det_sec = timing_data.get("deterministic_time_sec", 0.0)
        det_pct = timing_data.get("deterministic_pct", 0.0)
        llm_sec = timing_data.get("llm_time_sec", 0.0)
        llm_pct = timing_data.get("llm_pct", 0.0)

        st.markdown(f"""
        <div class="perf-card">
            <div class="perf-header">
                <div>
                    <div class="section-eyebrow">Efficiency & Throughput</div>
                    <div class="section-title" style="margin-bottom: 0px;">Pipeline Performance</div>
                </div>
                <div class="perf-badge">⚡ {thr:,.1f} records/sec</div>
            </div>
            <div class="perf-grid">
                <div class="perf-cell">
                    <div class="perf-label">Total Runtime</div>
                    <div class="perf-val">{tot_dur:.2f}s</div>
                    <div class="perf-sub">Wall-clock time</div>
                </div>
                <div class="perf-cell">
                    <div class="perf-label">Throughput</div>
                    <div class="perf-val sage">{thr:,.1f}</div>
                    <div class="perf-sub">Records / second</div>
                </div>
                <div class="perf-cell">
                    <div class="perf-label">Deterministic Time</div>
                    <div class="perf-val accent">{det_sec:.3f}s</div>
                    <div class="perf-sub">{det_pct:.1f}% of total run</div>
                </div>
                <div class="perf-cell">
                    <div class="perf-label">LLM / AI Reasoning</div>
                    <div class="perf-val rust">{llm_sec:.3f}s</div>
                    <div class="perf-sub">{llm_pct:.1f}% of total run</div>
                </div>
            </div>
            <div class="perf-bar-wrap">
                <div class="perf-bar-track">
                    <div class="perf-bar-det" style="width: {det_pct}%;" title="Deterministic: {det_sec:.2f}s ({det_pct:.1f}%)"></div>
                    <div class="perf-bar-llm" style="width: {llm_pct}%;" title="LLM: {llm_sec:.2f}s ({llm_pct:.1f}%)"></div>
                </div>
                <div class="perf-legend">
                    <span class="legend-item"><span class="dot-det"></span> <b>Deterministic Stages</b>: {det_sec:.2f}s ({det_pct:.1f}%)</span>
                    <span class="legend-item"><span class="dot-llm"></span> <b>LLM & Bundle Reasoning</b>: {llm_sec:.2f}s ({llm_pct:.1f}%)</span>
                </div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    col_left, col_right = st.columns([1.35, 1])

    with col_left:
        st.markdown('<div class="section-eyebrow">Deterministic-first</div>', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Resolution by Tier</div>', unsafe_allow_html=True)


        tier_counts = (
            master_df.group_by("resolved_at_tier").len()
            .to_pandas().set_index("resolved_at_tier")["len"].to_dict()
        )
        ordered_tiers = [t for t in TIER_ORDER if t in tier_counts]
        counts = [tier_counts[t] for t in ordered_tiers]
        short_labels = [t.split(": ", 1)[-1] if ": " in t else t for t in ordered_tiers]
        ai_touched = {"Exception: Math Match - Identity Mismatch", "Tier 4: AI Bundle"}
        bar_colors = [RUST if t in ai_touched else ACCENT for t in ordered_tiers]

        fig = go.Figure(go.Bar(
            x=counts, y=short_labels, orientation="h",
            marker=dict(color=bar_colors), text=counts, textposition="outside",
            textfont=dict(family="IBM Plex Mono", size=12, color=INK),
        ))
        fig.update_layout(
            height=380, margin=dict(l=0, r=30, t=10, b=10),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            font=dict(family="IBM Plex Mono", size=11.5, color=INK), showlegend=False,
            yaxis=dict(autorange="reversed", showgrid=False, tickfont=dict(family="IBM Plex Mono", size=11.5, color=INK)),
            xaxis=dict(showgrid=True, gridcolor=BORDER, zeroline=False, title=dict(text="Records", font=dict(family="IBM Plex Mono", size=11.5, color=INK)), tickfont=dict(family="IBM Plex Mono", size=11.5, color=INK)),
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption("🟤 Deterministic tiers &nbsp;&nbsp; 🔴 AI-touched tiers — deterministic tiers "
                   "resolve most records before any LLM call is made.")

    with col_right:
        st.markdown('<div class="section-eyebrow">Honest exception list</div>', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Where the gaps are</div>', unsafe_allow_html=True)

        exc_counts = master_df.filter(pl.col("status") != "Verified").group_by("resolved_at_tier").len().sort("len", descending=True)
        if len(exc_counts) > 0:
            fig_exc = px.pie(
                exc_counts.to_pandas(), values='len', names='resolved_at_tier', hole=0.6,
                color_discrete_sequence=[RUST, ACCENT, SAGE, STONE, "#D9CDBA", "#C9A66B", "#8B5E3C"]
            )
            fig_exc.update_traces(textposition='inside', textinfo='value', textfont=dict(color="white", size=12))
            fig_exc.update_layout(margin=dict(l=0, r=0, t=0, b=20), height=200, showlegend=False, paper_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig_exc, use_container_width=True)

        rows_html = ""
        for t in ordered_tiers:
            n = tier_counts[t]
            short = t.split(": ", 1)[-1] if ": " in t else t
            tier_num = t.split(":")[0].replace("Tier ", "T") if "Tier" in t else "EXC"
            rows_html += f'<div class="tier-row"><div class="tier-badge">{tier_num}</div><div class="tier-label">{short}</div><div class="tier-count">{n}</div></div>'
        st.markdown(f'<div class="ledger-card">{rows_html}</div>', unsafe_allow_html=True)

# ==========================================
# TAB 2 -- AUDIT TRAIL
# ==========================================
elif nav_choice == "Audit Trail":
    st.markdown('<div class="section-eyebrow">Every decision, explained</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Audit Trail — Exceptions & Manual Review</div>', unsafe_allow_html=True)

    exceptions_df = master_df.filter(pl.col("status") != "Verified")
    unique_tiers = ["All"] + sorted(exceptions_df.select("resolved_at_tier").unique().to_series().to_list())
    selected_tier = st.selectbox("Filter by Resolution Tier:", unique_tiers)
    if selected_tier != "All":
        exceptions_df = exceptions_df.filter(pl.col("resolved_at_tier") == selected_tier)

    display_cols = ["record_type", "customer_name", "resolved_at_tier", "status", "confidence", "amount_delta", "resolution_method"]
    valid_cols = [c for c in display_cols if c in exceptions_df.columns]
    exc_pandas = exceptions_df.select(valid_cols).to_pandas()

    st.dataframe(
        exc_pandas, use_container_width=True, height=560,
        column_config={
            "record_type": st.column_config.TextColumn("Side", width="small"),
            "customer_name": st.column_config.TextColumn("Entity", width="medium"),
            "resolved_at_tier": st.column_config.TextColumn("Tier", width="medium"),
            "status": st.column_config.TextColumn("Status", width="medium"),
            "confidence": st.column_config.ProgressColumn("Confidence", format="%.2f", min_value=0.0, max_value=1.0, width="small"),
            "amount_delta": st.column_config.NumberColumn("Δ Amount (₹)", format="₹ %+.2f", width="medium"),
            "resolution_method": st.column_config.TextColumn("Resolution Logic", width="large"),
        },
        hide_index=True,
    )
    st.download_button(
        "Download exceptions as CSV",
        exc_pandas.to_csv(index=False).encode("utf-8"),
        file_name="audit_trail_exceptions.csv",
        mime="text/csv",
    )

# ==========================================
# TAB 3 -- CASH FORECAST
# ==========================================
elif nav_choice == "Cash Forecast":
    st.markdown('<div class="section-eyebrow">Forward-looking liquidity</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Forward Cash Forecaster</div>', unsafe_allow_html=True)
    st.caption("Derived projection of unsettled receivables based on empirical gateway settlement turnaround (T+2 lag).")

    if forecast_unsettled_receivables is None:
        st.warning("Forecaster module not loaded. Check `src/forecaster.py`.")
    else:
        # Lag analysis summary
        lag_stats = compute_historical_lags(master_df)
        med_lag = lag_stats["overall_median_lag"]
        sample_cnt = lag_stats["sample_count"]

        # Date reference selection
        col_ref1, col_ref2 = st.columns([1.5, 2.5])
        with col_ref1:
            ref_mode = st.radio(
                "Forecast As-Of Reference:",
                ["Today (Real-Time Overdue Tracking)", "Batch Evaluation Cutoff (2026-08-12)"],
                horizontal=False,
                key="fc_ref_mode",
            )

        import datetime
        as_of = datetime.date.today() if "Today" in ref_mode else datetime.date(2026, 8, 12)

        forecast_df = forecast_unsettled_receivables(master_df, as_of_date=as_of)
        agg_df = aggregate_cash_forecast_by_bucket(forecast_df)

        tot_unsettled_amt = forecast_df["gross_amount"].sum() if len(forecast_df) > 0 else 0.0
        tot_unsettled_cnt = len(forecast_df)
        overdue_cnt = forecast_df.filter(pl.col("is_overdue")).shape[0] if len(forecast_df) > 0 else 0
        overdue_amt = forecast_df.filter(pl.col("is_overdue"))["gross_amount"].sum() if overdue_cnt > 0 else 0.0

        with col_ref2:
            st.markdown(f"""
            <div style="background-color: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; padding: 14px 18px; margin-top: 6px;">
                <div style="font-family: 'IBM Plex Mono', monospace; font-size: 11px; text-transform: uppercase; color: {MUTED};">Historical Baseline</div>
                <div style="font-size: 14.5px; color: {INK}; margin-top: 4px;">
                    Observed median settlement lag: <b>{med_lag:.0f} days</b> across <b>{sample_cnt} verified transactions</b>.
                    Turnaround conforms to standard Razorpay T+2 clearing cycles.
                </div>
            </div>
            """, unsafe_allow_html=True)

        st.write("")

        # Mini KPI summary
        st.markdown(f"""
        <div class="kpi-row" style="grid-template-columns: repeat(4, 1fr); margin-bottom: 24px;">
            <div class="kpi-cell"><div class="kpi-label">Unsettled Receivables</div><div class="kpi-value rust">₹{tot_unsettled_amt:,.2f}</div></div>
            <div class="kpi-cell"><div class="kpi-label">Pending Invoices</div><div class="kpi-value">{tot_unsettled_cnt}</div></div>
            <div class="kpi-cell"><div class="kpi-label">Historical Turnaround</div><div class="kpi-value accent">T+{med_lag:.0f} d</div></div>
            <div class="kpi-cell"><div class="kpi-label">Items Past Due</div><div class="kpi-value {'rust' if overdue_cnt > 0 else 'sage'}">{overdue_cnt} ({'₹' + f'{overdue_amt:,.2f}' if overdue_cnt > 0 else '₹0.00'})</div></div>
        </div>
        """, unsafe_allow_html=True)

        # Plotly Bar Chart of Buckets
        st.markdown('<div class="section-eyebrow">Projected Inflow Timing</div>', unsafe_allow_html=True)
        st.markdown('<div class="section-title" style="font-size: 20px;">Cash Inflow by Aging Bucket</div>', unsafe_allow_html=True)

        agg_pandas = agg_df.to_pandas()
        bucket_colors = {
            "Next 7 Days": SAGE,
            "8-14 Days": ACCENT,
            "15-30 Days": STONE,
            "30+ Days / Overdue": RUST,
        }
        colors = [bucket_colors.get(b, ACCENT) for b in agg_pandas["bucket"]]

        fig_fc = go.Figure(go.Bar(
            x=agg_pandas["bucket"],
            y=agg_pandas["amount"],
            text=[f"₹{amt:,.0f}<br>({cnt} inv)" if amt > 0 else "₹0" for amt, cnt in zip(agg_pandas["amount"], agg_pandas["count"])],
            textposition="outside",
            marker=dict(color=colors, line=dict(color=BORDER, width=1)),
            textfont=dict(family="IBM Plex Mono", size=12, color=INK),
        ))

        max_val = agg_pandas["amount"].max()
        y_max = (max_val * 1.25) if max_val > 0 else 1000

        fig_fc.update_layout(
            height=340,
            margin=dict(l=10, r=20, t=25, b=10),
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            font=dict(family="IBM Plex Mono", size=11.5, color=INK),
            showlegend=False,
            xaxis=dict(
                showgrid=False,
                tickfont=dict(family="IBM Plex Mono", size=12, color=INK),
            ),
            yaxis=dict(
                showgrid=True,
                gridcolor=BORDER,
                zeroline=False,
                range=[0, y_max],
                title=dict(text="Expected Inflow (₹)", font=dict(family="IBM Plex Mono", size=11.5, color=INK)),
                tickfont=dict(family="IBM Plex Mono", size=11.5, color=INK),
            ),
        )
        st.plotly_chart(fig_fc, use_container_width=True)

        st.markdown(f"""
        <div style="font-family: 'Inter', sans-serif; font-size: 13.5px; color: {MUTED}; background: {PAPER}; border-left: 3px solid {ACCENT}; padding: 8px 14px; margin: 12px 0 20px 0;">
            <b>Operational Estimator:</b> This forecast applies observed historical settlement lag (median {med_lag:.0f} days) to Tier 5 unsettled receivables.
            It is an operational liquidity projection, not a complex predictive model.
        </div>
        """, unsafe_allow_html=True)

        # Underlying receivables table
        st.markdown('<div class="section-eyebrow">Underlying receivables</div>', unsafe_allow_html=True)
        st.markdown('<div class="section-title" style="font-size: 20px;">Pending Receivables Schedule</div>', unsafe_allow_html=True)

        fc_pandas = forecast_df.to_pandas()
        st.dataframe(
            fc_pandas,
            use_container_width=True,
            height=360,
            column_config={
                "ledger_payment_id": st.column_config.TextColumn("Payment ID", width="medium"),
                "customer_name": st.column_config.TextColumn("Customer Entity", width="medium"),
                "gross_amount": st.column_config.NumberColumn("Amount (₹)", format="₹ %.2f", width="medium"),
                "payment_date": st.column_config.DateColumn("Payment Date", width="small"),
                "applied_lag_days": st.column_config.NumberColumn("Lag (Days)", format="%d d", width="small"),
                "lag_source": st.column_config.TextColumn("Lag Method", width="medium"),
                "projected_settlement_date": st.column_config.DateColumn("Expected Settlement", width="small"),
                "days_until_settlement": st.column_config.NumberColumn("Days Remaining", format="%d d", width="small"),
                "bucket": st.column_config.TextColumn("Forecast Bucket", width="medium"),
                "is_overdue": st.column_config.CheckboxColumn("Past Due", width="small"),
            },
            hide_index=True,
        )

        st.download_button(
            "Download cash forecast schedule as CSV",
            fc_pandas.to_csv(index=False).encode("utf-8"),
            file_name=f"cash_forecast_{as_of.isoformat()}.csv",
            mime="text/csv",
        )

# ==========================================
# TAB 4 -- Q&A
# ==========================================
elif nav_choice == "Settlement Q&A":
    st.markdown('<div class="section-eyebrow">Ask the ledger</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Settlement Q&A</div>', unsafe_allow_html=True)
    st.caption("Natural language → SQL over the reconciliation table. Grounded in the data above — nothing is answered from memory.")

    if ask_question is None:
        st.warning("Q&A agent module not loaded. Check `src/qa_agent.py`.")
    else:
        if "messages" not in st.session_state:
            st.session_state.messages = [
                {"role": "assistant", "content": "I've indexed this batch's reconciliation results. Ask about exceptions, tiers, or amounts."}
            ]

        # Height scales a little with message count instead of always
        # reserving a large fixed box that sits mostly empty on a fresh chat.
        n_msgs = len(st.session_state.messages)
        chat_height = min(440, max(160, 90 + n_msgs * 90))
        chat_box = st.container(height=chat_height, border=True)
        for message in st.session_state.messages:
            with chat_box:
                with st.chat_message(message["role"]):
                    st.markdown(message["content"])

        prompt = st.chat_input("e.g. How much revenue was lost to bank fees?")
        if prompt:
            st.session_state.messages.append({"role": "user", "content": prompt})
            with chat_box:
                with st.chat_message("user"):
                    st.markdown(prompt)
                with st.chat_message("assistant"):
                    with st.spinner("Querying ledger..."):
                        try:
                            response = ask_question(prompt)
                        except Exception as e:
                            response = f"Couldn't process that question ({type(e).__name__}). Try rephrasing it."
                    st.markdown(response)
            st.session_state.messages.append({"role": "assistant", "content": response})
            st.rerun()

# ==========================================
# TAB 5 -- DATASETS
# ==========================================
elif nav_choice == "Datasets":
    st.markdown('<div class="section-eyebrow">Raw inputs & final output</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Datasets</div>', unsafe_allow_html=True)
    st.caption("The two source files that go in, and the master reconciliation table that comes out.")

    dataset_names = list(raw_datasets.keys()) + ["Master Reconciliation Table"]
    if not dataset_names:
        st.info("No datasets found under `data/`.")
    else:
        picked = st.radio("View dataset:", dataset_names, horizontal=True)

        if picked == "Master Reconciliation Table":
            view_df = master_df.to_pandas()
        else:
            view_df = raw_datasets[picked].to_pandas()

        st.markdown(f"**{len(view_df):,} rows &nbsp;·&nbsp; {len(view_df.columns)} columns**")
        st.dataframe(view_df, use_container_width=True, height=520, hide_index=True)
        st.download_button(
            f"Download {picked} as CSV",
            view_df.to_csv(index=False).encode("utf-8"),
            file_name=f"{picked.lower().replace(' ', '_')}.csv",
            mime="text/csv",
        )