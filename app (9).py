"""
Booked Profit Dashboard — Streamlit version (premium dark theme).

Runs entirely in the cloud (Streamlit Community Cloud, free): logs into
Angel One, holds a live WebSocket price feed in a background thread, and
renders the same open/closed positions view as the original local HTML
dashboard — but reachable from any device, with your PC turned off.

Local files reused unchanged from the original project:
    positions_builder.py   — FIFO matching (open + closed positions)
    token_resolver.py      — Angel One instrument-master token lookup

Secrets required (set in Streamlit Cloud's "Secrets" panel, never in code):
    APP_PASSWORD        — shared password gate for viewers
    ANGEL_API_KEY
    ANGEL_CLIENT_CODE
    ANGEL_PASSWORD
    ANGEL_TOTP_SECRET

The trade ledger is fetched live as JSON from a Google Apps Script Web App
bound to the Sheet (see Code.gs — deploy it, paste the /exec URL into the
sidebar, or default it via st.secrets["APPS_SCRIPT_URL"]) rather than an
uploaded/local Excel file or a CSV export link. This means: no "Anyone with
the link" sharing requirement (the script runs under your own account), no
CSV parsing/format guessing, and today's ledger updates without a redeploy
— just edit the sheet and hit "Restart feed / refetch sheet".

LIVE UPDATES
    Instead of refreshing the whole page every few seconds (old
    st_autorefresh approach — flickers, resets scroll position, reruns
    everything including the sidebar), the KPI cards + tables now live
    inside an @st.fragment(run_every=...) block. Only that fragment reruns
    on its own clock, reading whatever ticks have landed in the background
    WebSocket thread since the last run — so the screen updates in near
    real time, tick by tick, without touching the rest of the app.
    Requires streamlit >= 1.33.
"""
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import pyotp
import requests
import streamlit as st
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

from positions_builder import load_trade_ledger, load_trade_ledger_from_records, build_positions
from token_resolver import resolve_all
from dealer_api import DealerAPIClient, trades_to_ledger_rows

IST = ZoneInfo("Asia/Kolkata")


def _fetch_ledger_records(apps_script_url: str, sheet_name: str | None = None):
    """Hits the Apps Script Web App's /exec URL and returns its JSON body
    (a list of row-dicts) directly — no CSV parsing involved. See Code.gs
    for the doGet handler this talks to.
    """
    params = {"sheet": sheet_name} if sheet_name else None

    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(apps_script_url, params=params, timeout=45)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "error" in data:
                raise ValueError(f"Apps Script error: {data['error']}")
            return data
        except requests.exceptions.ReadTimeout as e:
            last_err = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))  # 2s, then 4s backoff
                continue
            raise TimeoutError(
                "The ledger service (Google Apps Script) didn't respond in time "
                "after 3 attempts. It may be cold-starting or the sheet is large — "
                "please try refreshing in a moment."
            ) from e
    raise last_err


@st.cache_resource(show_spinner=False)
def get_dealer_client() -> DealerAPIClient | None:
    """One shared dealer-API login for the whole app (all clients' trade
    books are fetched under this single dealer session). Returns None if
    the dealer secrets aren't configured, so the app can fall back cleanly
    to the Apps Script / Google Sheet path for clients without them.
    """
    host = st.secrets.get("DEALER_HOST_URL", "")
    api_key = st.secrets.get("DEALER_API_KEY", "")
    user_id = st.secrets.get("DEALER_USER_ID", "")
    password = st.secrets.get("DEALER_PASSWORD", "")
    if not (host and api_key and user_id and password):
        return None
    client = DealerAPIClient(host_url=host, api_key=api_key, user_id=user_id, password=password)
    client.login()  # fail fast here rather than on first trade-book call
    return client


def _fetch_dealer_ledger_rows(dealer_client_id: str):
    """Pull the live Trade Book for one client straight from the broker's
    dealer API and reshape it into positions_builder's ledger-row format —
    the direct-integration replacement for _fetch_ledger_records()."""
    client = get_dealer_client()
    if client is None:
        raise RuntimeError(
            "Dealer API isn't configured (DEALER_HOST_URL / DEALER_API_KEY / "
            "DEALER_USER_ID / DEALER_PASSWORD missing from secrets)."
        )
    trades = client.get_trade_book(client_id=dealer_client_id)
    return trades_to_ledger_rows(trades)


st.set_page_config(
    page_title="Booked Profit Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

MASTER_CACHE_PATH = "instrument_master_cache.json"
MASTER_CACHE_MAX_AGE_HOURS = 20
SUBSCRIBE_MODE = 2
MAX_TOKENS_PER_SUBSCRIBE = 1000
TICK_REFRESH_SECONDS = 1  # how often the live fragment re-renders


# ── Premium theme ──────────────────────────────────────────────────────────
def inject_theme():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Carlito:ital,wght@0,400;0,500;0,600;0,700;1,400&display=swap');

        :root {
            --bg: #070b14;
            --panel: #10161f;
            --panel-2: #131b27;
            --border: #1e2733;
            --accent: #34d5c8;
            --accent-2: #7c5cff;
            --pos: #2ee6a6;
            --neg: #ff5c7a;
            --muted: #7f8ba3;
            --text: #eaf0f7;
        }

        html, body, [class*="css"]  { font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif; }

        .stApp {
            background:
                radial-gradient(circle at 15% 0%, rgba(124,92,255,0.10), transparent 40%),
                radial-gradient(circle at 85% 10%, rgba(52,213,200,0.08), transparent 35%),
                var(--bg);
            color: var(--text);
        }

        section[data-testid="stSidebar"] {
            background: var(--panel);
            border-right: 1px solid var(--border);
        }

        div.block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1300px; }

        /* Header */
        .db-header {
            display: flex; align-items: center; justify-content: space-between;
            padding-bottom: 6px; margin-bottom: 22px;
            border-bottom: 1px solid var(--border);
        }
        .db-title { display: flex; align-items: center; gap: 12px; }
        .db-title h1 {
            font-size: 1.65rem; font-weight: 800; letter-spacing: -0.02em; margin: 0;
            background: linear-gradient(90deg, #ffffff, #b9c4d6);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }
        .db-icon {
            width: 40px; height: 40px; border-radius: 12px;
            background: linear-gradient(135deg, var(--accent), var(--accent-2));
            display: flex; align-items: center; justify-content: center;
            font-size: 1.15rem; box-shadow: 0 0 24px rgba(52,213,200,0.35);
        }

        .status-pill {
            display: inline-flex; align-items: center; gap: 8px;
            padding: 6px 14px; border-radius: 999px; font-size: 0.78rem; font-weight: 600;
            border: 1px solid var(--border); background: var(--panel-2); color: var(--muted);
        }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
        .status-live .status-dot { background: var(--pos); box-shadow: 0 0 8px var(--pos); animation: pulse 1.4s ease-in-out infinite; }
        .status-live { color: var(--pos); border-color: rgba(46,230,166,0.25); }
        .status-error .status-dot { background: var(--neg); }
        .status-error { color: var(--neg); border-color: rgba(255,92,122,0.25); }
        .status-warn .status-dot { background: #f5b942; }
        .status-warn { color: #f5b942; border-color: rgba(245,185,66,0.25); }
        @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }

        /* KPI cards */
        .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-bottom: 26px; }
        .kpi-card {
            background: linear-gradient(180deg, var(--panel-2), var(--panel));
            border: 1px solid var(--border); border-radius: 16px; padding: 18px 20px;
            position: relative;
            container-type: inline-size; /* lets kpi-value size off THIS card's actual width */
        }
        .kpi-card::before {
            content: ""; position: absolute; top: 0; left: 0; right: 0; height: 3px;
            background: linear-gradient(90deg, var(--accent), var(--accent-2)); opacity: 0.85;
            border-radius: 16px 16px 0 0; /* rounds the bar itself now that the card no longer clips overflow */
        }
        .kpi-label { font-size: 0.74rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin-bottom: 8px; white-space: nowrap; }
        /* cqw = % of this card's own width, so the value shrinks exactly as much as its
           card needs regardless of how many KPI cards share the row. No overflow/ellipsis
           here on purpose — a clipped digit on a money figure is worse than a smaller font. */
        .kpi-value { font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif; font-size: clamp(0.72rem, 8.5cqw, 1.55rem); font-weight: 700; letter-spacing: -0.01em; white-space: nowrap; display: block; }
        .kpi-pos { color: var(--pos); }
        .kpi-neg { color: var(--neg); }
        .kpi-sub { font-size: 0.72rem; color: var(--muted); margin-top: 6px; font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif; }

        /* Section headers */
        .section-label {
            display: flex; align-items: center; gap: 8px;
            font-size: 0.95rem; font-weight: 700; margin: 6px 0 10px 0; color: var(--text);
        }
        .section-label .badge {
            font-size: 0.68rem; font-weight: 600; padding: 2px 9px; border-radius: 999px;
            background: var(--panel-2); border: 1px solid var(--border); color: var(--muted);
        }

        /* Tabs */
        .stTabs [data-baseweb="tab-list"] { gap: 4px; border-bottom: 1px solid var(--border); }
        .stTabs [data-baseweb="tab"] {
            background: transparent; color: var(--muted); font-weight: 600; padding: 10px 18px;
        }
        .stTabs [aria-selected="true"] {
            color: var(--accent) !important; border-bottom: 2px solid var(--accent) !important;
        }

        /* Top status + live clock bar */
        .top-bar {
            display: flex; align-items: center; justify-content: space-between;
            background: var(--panel-2); border: 1px solid var(--border); border-radius: 12px;
            padding: 10px 18px; margin-bottom: 20px;
        }
        .top-clock {
            font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif; font-size: 0.82rem; color: var(--text);
            display: flex; align-items: center; gap: 10px;
        }
        .top-clock .date-part { color: var(--muted); }
        .top-clock .tz-badge {
            font-size: 0.65rem; font-weight: 700; color: var(--accent);
            background: rgba(52,213,200,0.1); border: 1px solid rgba(52,213,200,0.25);
            padding: 1px 7px; border-radius: 999px;
        }

        /* Segment-scoped one-line summary (shown inside each open/closed
           segment tab — reflects ONLY that segment, not the whole book). */
        .seg-summary {
            display: flex; flex-wrap: wrap; align-items: center; gap: 22px;
            background: var(--panel-2); border: 1px solid var(--border); border-radius: 12px;
            padding: 10px 18px; margin-bottom: 16px;
        }
        .seg-stat { display: flex; align-items: center; gap: 8px; }
        .seg-stat-label {
            font-size: 0.68rem; font-weight: 600; text-transform: uppercase;
            letter-spacing: 0.05em; color: var(--muted);
        }
        .seg-stat-value {
            font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif;
            font-size: 0.88rem; font-weight: 700;
        }

        /* Position cards */
        .pos-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-bottom: 8px; }
        @media (max-width: 640px) { .pos-grid { grid-template-columns: 1fr; } }
        .pos-card {
            background: linear-gradient(180deg, var(--panel-2), var(--panel));
            border: 1px solid var(--border); border-radius: 14px; padding: 14px 16px;
            transition: border-color 0.2s ease;
        }
        .pos-card:hover { border-color: rgba(52,213,200,0.35); }

        /* Highest daily gain card — animated RGB glow so it pops out of the grid.
           Recomputed every refresh, so this always follows whichever position
           currently has the top daily gain rather than sitting on one symbol. */
        .pos-card.top-gain {
            position: relative;
            border-color: transparent;
            background:
                linear-gradient(180deg, var(--panel-2), var(--panel)) padding-box,
                conic-gradient(from var(--rgb-angle, 0deg), #ff3cac, #784ba0, #2b86c5, #2ee6a6, #f5b942, #ff3cac) border-box;
            border: 2px solid transparent;
            animation: rgb-spin 4s linear infinite;
            box-shadow: 0 0 22px rgba(124,92,255,0.35), 0 0 42px rgba(52,213,200,0.18);
        }
        @property --rgb-angle {
            syntax: '<angle>'; inherits: false; initial-value: 0deg;
        }
        @keyframes rgb-spin {
            to { --rgb-angle: 360deg; }
        }
        .top-gain-badge {
            position: absolute; top: -10px; right: 14px;
            font-size: 0.6rem; font-weight: 800; letter-spacing: 0.04em;
            padding: 2px 9px; border-radius: 999px; text-transform: uppercase;
            background: linear-gradient(90deg, #ff3cac, #784ba0, #2b86c5, #2ee6a6);
            background-size: 300% 100%; animation: rgb-shift 3s linear infinite;
            color: #05070d; box-shadow: 0 0 10px rgba(124,92,255,0.5);
        }
        @keyframes rgb-shift {
            0% { background-position: 0% 50%; }
            100% { background-position: 300% 50%; }
        }
        .pos-top { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; }
        .pos-symbol { font-weight: 700; font-size: 0.98rem; letter-spacing: -0.01em; }
        .pos-tags { display: flex; gap: 5px; margin-top: 4px; }
        .pos-chip {
            font-size: 0.62rem; font-weight: 700; padding: 1px 7px; border-radius: 999px;
            background: var(--panel); border: 1px solid var(--border); color: var(--muted);
            text-transform: uppercase; letter-spacing: 0.03em;
        }
        .pos-chip.long { color: var(--pos); border-color: rgba(46,230,166,0.3); }
        .pos-chip.short { color: var(--neg); border-color: rgba(255,92,122,0.3); }
        .pos-daychg {
            font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif; font-size: 0.72rem; font-weight: 700;
            padding: 3px 9px; border-radius: 999px; white-space: nowrap;
        }
        .day-pos { background: rgba(46,230,166,0.12); color: var(--pos); }
        .day-neg { background: rgba(255,92,122,0.12); color: var(--neg); }
        .pos-rows { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 14px; margin-bottom: 10px; }
        .pos-row-label { font-size: 0.68rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }
        .pos-row-value { font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif; font-size: 0.82rem; font-weight: 600; }
        .pos-mtm-block {
            display: flex; justify-content: space-between; align-items: center;
            border-top: 1px solid var(--border); padding-top: 10px;
        }
        .pos-mtm-label { font-size: 0.68rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }
        .pos-mtm-value { font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif; font-size: 1.02rem; font-weight: 700; }
        .pos-tick { font-size: 0.66rem; color: var(--muted); font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif; }

        /* Closed position cards */
        .closed-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-bottom: 20px; }
        @media (max-width: 640px) { .closed-grid { grid-template-columns: 1fr; } }
        .closed-card {
            background: linear-gradient(180deg, var(--panel-2), var(--panel));
            border: 1px solid var(--border); border-radius: 14px; padding: 13px 15px;
        }
        .closed-top { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; }
        .closed-symbol { font-weight: 700; font-size: 0.92rem; }
        .closed-badge {
            font-size: 0.62rem; font-weight: 700; padding: 1px 7px; border-radius: 999px;
            background: var(--panel); border: 1px solid var(--border); color: var(--muted);
        }
        .closed-rows { display: flex; justify-content: space-between; font-size: 0.72rem; color: var(--muted); margin-bottom: 4px; font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif; }
        .closed-pnl { font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif; font-weight: 700; font-size: 0.95rem; margin-top: 8px; }

        /* Position table — borderless rows, just a thin separator line
           between each position, replacing the boxed-card layout. */
        .pos-table-wrap { overflow-x: auto; margin-bottom: 20px; }
        .pos-table { width: 100%; min-width: 760px; border-collapse: collapse; }
        .pos-table-head, .pos-table-row {
            display: grid;
            align-items: center;
            column-gap: 14px;
        }
        .pos-table-head.cols-open, .pos-table-row.cols-open {
            grid-template-columns: 1.5fr 0.8fr 0.8fr 1fr 1fr 1fr 1.2fr 1fr;
        }
        .pos-table-head.cols-closed, .pos-table-row.cols-closed {
            grid-template-columns: 1.5fr 0.8fr 0.8fr 1fr 1fr 1fr 1.2fr;
        }
        .pos-table-head {
            padding: 6px 6px 10px 6px;
            border-bottom: 1px solid var(--border);
        }
        .pos-table-head > div {
            font-size: 0.66rem; font-weight: 700; text-transform: uppercase;
            letter-spacing: 0.05em; color: var(--muted);
        }
        .pos-table-row {
            padding: 13px 6px;
            border-bottom: 1px solid var(--border);
            transition: background 0.15s ease;
        }
        .pos-table-row:hover { background: rgba(255,255,255,0.025); }
        .pos-table-row:last-child { border-bottom: none; }
        .pt-symbol { display: flex; align-items: baseline; gap: 8px; }
        .pt-symbol-name { font-weight: 700; font-size: 0.9rem; letter-spacing: -0.01em; }
        .pt-tag {
            font-size: 0.58rem; font-weight: 700; padding: 1px 6px; border-radius: 999px;
            border: 1px solid var(--border); color: var(--muted); text-transform: uppercase;
        }
        .pt-tag.long { color: var(--pos); border-color: rgba(46,230,166,0.3); }
        .pt-tag.short { color: var(--neg); border-color: rgba(255,92,122,0.3); }
        .pt-cell { font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif; font-size: 0.85rem; font-weight: 600; }
        .pt-cell.muted { color: var(--muted); font-weight: 500; font-size: 0.78rem; }
        .pt-cell.pos { color: var(--pos); }
        .pt-cell.neg { color: var(--neg); }
        .pt-arrow { font-size: 0.72rem; margin-right: 2px; }

        /* Leader row (top gain / least loss) — no box, just a soft glowing
           left accent bar + tinted background so it stands out among plain
           rows without reintroducing card borders. */
        .pos-table-row.top-gain-row {
            position: relative;
            background: linear-gradient(90deg, rgba(124,92,255,0.10), transparent 60%);
            border-left: 3px solid;
            border-image: linear-gradient(180deg, #ff3cac, #784ba0, #2b86c5, #2ee6a6) 1;
        }
        .pt-leader-badge {
            display: inline-flex; align-items: center; gap: 4px; margin-left: 8px;
            font-size: 0.58rem; font-weight: 800; letter-spacing: 0.04em;
            padding: 1px 8px; border-radius: 999px; text-transform: uppercase;
            background: linear-gradient(90deg, #ff3cac, #784ba0, #2b86c5, #2ee6a6);
            background-size: 300% 100%; animation: rgb-shift 3s linear infinite;
            color: #05070d; white-space: nowrap; flex-shrink: 0;
        }

        /* Chart cards: st.markdown('<div class="chart-card">') + st.altair_chart(...) +
           st.markdown('</div>') used to be 3 SEPARATE calls — Streamlit renders each call
           as its own DOM node, so that div opened and closed empty and the real chart sat
           outside it, unstyled. Charts now render inside `with st.container(border=True):`,
           which is a genuine parent element, and we restyle Streamlit's own wrapper for it
           below so it matches the rest of the theme instead of Streamlit's default grey box. */
        div[data-testid="stVerticalBlockBorderWrapper"] {
            background: var(--panel-2) !important; border: 1px solid var(--border) !important;
            border-radius: 14px !important; margin-bottom: 18px !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"] > div { border-radius: 14px !important; }

        /* Dataframe polish (still used where a raw table makes sense) */
        div[data-testid="stDataFrame"] {
            border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
        }

        div[data-testid="stMetric"] { display: none; }  /* using custom KPI cards instead */

        /* Hero MTM block — big headline number + status pill, used for the
           top-of-dashboard "Live position MTM" and the closed-tab "Booked
           ledger" summary. */
        .hero-block {
            display: flex; justify-content: space-between; align-items: flex-start;
            flex-wrap: wrap; gap: 14px; margin-bottom: 18px;
        }
        .hero-label {
            display: flex; align-items: center; gap: 8px;
            font-size: 0.72rem; font-weight: 700; text-transform: uppercase;
            letter-spacing: 0.08em; color: var(--muted); margin-bottom: 10px;
        }
        .hero-label .dot { width: 6px; height: 6px; border-radius: 50%; }
        .hero-label .dot.positive { background: var(--pos); box-shadow: 0 0 8px var(--pos); }
        .hero-label .dot.negative { background: var(--neg); box-shadow: 0 0 8px var(--neg); }
        .hero-value {
            font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif;
            font-size: clamp(1.9rem, 4.2vw, 3rem); font-weight: 800; letter-spacing: -0.02em;
            line-height: 1.1;
        }
        .hero-value.positive { color: var(--pos); }
        .hero-value.negative { color: var(--neg); }
        .hero-sub { font-size: 0.82rem; color: var(--muted); margin-top: 6px; }
        .hero-pill {
            display: inline-flex; align-items: center; gap: 6px;
            padding: 7px 16px; border-radius: 999px; font-size: 0.72rem; font-weight: 700;
            letter-spacing: 0.04em; text-transform: uppercase; border: 1px solid; white-space: nowrap;
        }
        .hero-pill.positive { color: var(--pos); border-color: rgba(46,230,166,0.4); background: rgba(46,230,166,0.08); }
        .hero-pill.negative { color: var(--neg); border-color: rgba(255,92,122,0.4); background: rgba(255,92,122,0.08); }

        /* Plain bordered stat tiles under a hero block. Always ONE row:
           auto-fit + minmax used to wrap a card to a second row once the
           strip ran out of horizontal space (e.g. 7 tiles at 170px min each
           needs ~1260px). grid-auto-flow: column instead lays every tile
           into the same row and, if the strip is still too narrow (small
           screens), lets it scroll horizontally rather than wrap. */
        .hero-tiles {
            display: grid; grid-auto-flow: column; grid-auto-columns: minmax(130px, 1fr);
            gap: 10px; margin-bottom: 26px; overflow-x: auto; padding-bottom: 2px;
        }
        .hero-tile {
            background: var(--panel-2); border: 1px solid var(--border); border-radius: 12px;
            padding: 11px 14px; min-width: 130px;
        }
        .hero-tile-label {
            font-size: 0.58rem; font-weight: 700; text-transform: uppercase;
            letter-spacing: 0.04em; color: var(--muted); margin-bottom: 6px;
            white-space: nowrap;
        }
        .hero-tile-value {
            font-family: 'Calibri', 'Carlito', 'Segoe UI', sans-serif;
            font-size: 0.88rem; font-weight: 700; white-space: nowrap;
        }
        .hero-tile-value.positive { color: var(--pos); }
        .hero-tile-value.negative { color: var(--neg); }

        .news-item {
            padding: 12px 10px 12px 12px; border-bottom: 1px solid var(--border);
            border-left: 3px solid transparent; transition: background 0.15s;
        }
        .news-item:last-child { border-bottom: none; }
        /* Most recent (<24h) gets the strongest highlight, "recent" (1-3
           days) a subtler one, and 3-7 days old is left plain — reusing
           the app's existing accent/accent-2 colors rather than a new hue. */
        .news-item.news-new { border-left-color: var(--accent); background: rgba(52,213,200,0.07); }
        .news-item.news-recent { border-left-color: var(--accent-2); background: rgba(124,92,255,0.05); }
        .news-title {
            color: var(--text); font-weight: 600; font-size: 0.92rem;
            text-decoration: none; line-height: 1.4;
        }
        .news-title:hover { color: var(--accent, #4da3ff); text-decoration: underline; }
        .news-badge {
            display: inline-block; font-size: 0.58rem; font-weight: 800;
            letter-spacing: 0.04em; text-transform: uppercase; margin-left: 8px;
            padding: 1px 7px; border-radius: 999px; vertical-align: middle; white-space: nowrap;
        }
        .news-badge-new {
            background: rgba(52,213,200,0.15); color: var(--accent);
            border: 1px solid rgba(52,213,200,0.4);
        }
        .news-badge-recent {
            background: rgba(124,92,255,0.15); color: var(--accent-2);
            border: 1px solid rgba(124,92,255,0.4);
        }
        .news-badge-stock {
            background: var(--panel-2); color: var(--muted);
            border: 1px solid var(--border); font-weight: 700;
        }
        .news-meta {
            color: var(--muted); font-size: 0.74rem; margin-top: 4px;
        }
        </style>
        <style>
        /* Thin, unobtrusive scrollbar for .hero-tiles when it does need to scroll. */
        .hero-tiles::-webkit-scrollbar { height: 4px; }
        .hero-tiles::-webkit-scrollbar-thumb { background: var(--border); border-radius: 999px; }
        """,
        unsafe_allow_html=True,
    )


def status_pill(state: str, label: str) -> str:
    cls = {"live": "status-live", "error": "status-error", "warn": "status-warn"}.get(state, "")
    return f'<span class="status-pill {cls}"><span class="status-dot"></span>{label}</span>'


def flat(html: str) -> str:
    """Collapse a multi-line, indented HTML f-string to a single line.

    Streamlit's markdown renderer runs HTML through a CommonMark parser
    before allowing it through. An HTML block continues being treated as
    raw HTML only until a blank line; after that, any line indented 4+
    spaces (which our nested Python f-strings produce naturally) is read
    as an *indented code block* and shown as literal text instead of being
    rendered — this is what caused the closed-position cards to print
    "<div class=..." as visible text after the first card. Stripping all
    newlines/indentation removes any chance of a stray blank line or deep
    indentation confusing the parser, regardless of how deeply the
    generating Python code is nested.
    """
    return re.sub(r"\s*\n\s*", "", html.strip())


def kpi_card(label, value, positive=None, sub=None):
    cls = "kpi-pos" if positive is True else ("kpi-neg" if positive is False else "")
    sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    return flat(f"""
        <div class="kpi-card">
            <div class="kpi-label">{label}</div>
            <div class="kpi-value {cls}">{value}</div>
            {sub_html}
        </div>
    """)


def seg_summary_line(items):
    """items: list of (label, value_html, extra_css_class) tuples — a single
    horizontal strip of stats scoped to whichever segment tab it's rendered
    inside, so it never mixes numbers across segments."""
    stats = "".join(
        f'<div class="seg-stat"><span class="seg-stat-label">{lbl}</span>'
        f'<span class="seg-stat-value {cls}">{val}</span></div>'
        for lbl, val, cls in items
    )
    return flat(f'<div class="seg-summary">{stats}</div>')


def hero_block(label, value, sub, is_positive, pill_text):
    sign_cls = "positive" if is_positive else "negative"
    return flat(f"""
        <div class="hero-block">
            <div>
                <div class="hero-label"><span class="dot {sign_cls}"></span>{label}</div>
                <div class="hero-value {sign_cls}">{value}</div>
                <div class="hero-sub">{sub}</div>
            </div>
            <div class="hero-pill {sign_cls}">{'▲' if is_positive else '▼'} {pill_text}</div>
        </div>
    """)


def hero_tiles(items):
    """items: list of (label, value_html, extra_css_class) tuples rendered
    as plain bordered stat boxes under a hero_block."""
    tiles = "".join(
        f'<div class="hero-tile"><div class="hero-tile-label">{lbl}</div>'
        f'<div class="hero-tile-value {cls}">{val}</div></div>'
        for lbl, val, cls in items
    )
    return flat(f'<div class="hero-tiles">{tiles}</div>')


# ── Access log ─────────────────────────────────────────────────────────────
# Shared across every visitor's session via st.cache_resource (a
# session_state list would only be visible to that one visitor's own
# browser tab). In-memory only: resets whenever the app restarts/redeploys,
# same lifetime as get_engine's cache. Capped so it can't grow unbounded
# over a long-running deployment.
_ACCESS_LOG_MAX = 500


@st.cache_resource
def _access_log_store():
    return {"lock": threading.Lock(), "entries": []}


def _record_access(name):
    store = _access_log_store()
    with store["lock"]:
        store["entries"].append({"name": name, "ts": datetime.now(IST)})
        if len(store["entries"]) > _ACCESS_LOG_MAX:
            del store["entries"][: len(store["entries"]) - _ACCESS_LOG_MAX]


def _get_access_log():
    store = _access_log_store()
    with store["lock"]:
        return list(store["entries"])


# ── Client-wise login ─────────────────────────────────────────────────────
#
# Secrets format (set in Streamlit Cloud → Secrets):
#
# [clients.ROHITH]
# password       = "secret123"
# display_name   = "Rohith Sir"
# apps_script_url = "https://script.google.com/macros/s/ABC.../exec"
# sheet_name     = "Rohith"          # optional — sheet tab name
#
# [clients.PRIYA]
# password       = "priya456"
# display_name   = "Priya"
# apps_script_url = "https://script.google.com/macros/s/XYZ.../exec"
# sheet_name     = "Priya"
#
# Optionally, one admin client sees ALL positions concatenated:
# [clients.ADMIN]
# password        = "adminpass"
# display_name    = "Admin"
# is_admin        = true              # sees all clients' data merged
# apps_script_url = "..."
#
# Falls back to the old flat APP_PASSWORD / APPS_SCRIPT_URL secrets if no
# [clients] table is defined (backward-compatible).
#
# DIRECT BROKER FEED (no manual sheet at all) — set this instead of/alongside
# apps_script_url on any client to pull their trade book straight from the
# dealer API (see dealer_api.py):
# [clients.ROHITH]
# password          = "secret123"
# display_name      = "Rohith Sir"
# dealer_client_id  = "VISHAL1"      # this client's ID as known to the dealer
#
# ...plus these dealer-wide secrets (one login covers every mapped client):
# DEALER_HOST_URL = "https://<your-broker-host>"
# DEALER_API_KEY  = "..."
# DEALER_USER_ID  = "..."
# DEALER_PASSWORD = "..."

def _get_clients():
    """Return dict of client_id -> config dict from st.secrets."""
    try:
        raw = st.secrets.get("clients", {})
        if not raw:
            # Backward-compat: single shared password
            return {
                "DEFAULT": {
                    "password":        st.secrets.get("APP_PASSWORD", ""),
                    "display_name":    "User",
                    "apps_script_url": st.secrets.get("APPS_SCRIPT_URL", ""),
                    "sheet_name":      st.secrets.get("APPS_SCRIPT_SHEET_NAME", ""),
                    "is_admin":        False,
                }
            }
        default_url = st.secrets.get("APPS_SCRIPT_URL", "")
        clients = {}
        for k, v in raw.items():
            cfg = dict(v)
            cfg.setdefault("apps_script_url", default_url)
            if not cfg.get("apps_script_url"):
                cfg["apps_script_url"] = default_url
            clients[k.upper()] = cfg
        return clients
    except Exception:
        return {}


def check_password():
    """Show login form. On success stores client config in session_state.
    Returns True only when a valid client is logged in."""

    if st.session_state.get("_client_ok"):
        return True

    inject_theme()
    st.markdown(
        flat("""
        <div class="db-title" style="justify-content:center; margin: 60px 0 24px 0;">
            <div class="db-icon">📊</div>
            <h1>Booked Profit Dashboard</h1>
        </div>
        """),
        unsafe_allow_html=True,
    )

    clients = _get_clients()

    def _submit():
        cid   = st.session_state.get("_login_id", "").strip().upper()
        pw    = st.session_state.get("_login_pw", "")
        cfg   = clients.get(cid)
        if cfg and pw == cfg.get("password", ""):
            st.session_state["_client_ok"]     = True
            st.session_state["_client_id"]     = cid
            st.session_state["_client_cfg"]    = cfg
            st.session_state["_client_name"]   = cfg.get("display_name", cid)
            _record_access(cfg.get("display_name", cid))
        else:
            st.session_state["_client_ok"]    = False
            st.session_state["_login_failed"] = True

    c1, c2, c3 = st.columns([1, 1.1, 1])
    with c2:
        with st.container(border=True):
            st.markdown(
                "<p style='text-align:center;color:var(--muted);font-size:.85rem;"
                "margin-bottom:18px;'>Sign in to view your portfolio</p>",
                unsafe_allow_html=True,
            )
            st.text_input("Client ID", key="_login_id",
                          placeholder="e.g. ROHITH",
                          help="Your unique client code — ask your broker for this.")
            st.text_input("Password", type="password", key="_login_pw",
                          on_change=_submit,
                          placeholder="Enter your password")
            st.button("Sign in →", on_click=_submit, use_container_width=True, type="primary")

            if st.session_state.get("_login_failed") and not st.session_state.get("_client_ok"):
                st.error("Invalid Client ID or password.")

    return False


def current_client_cfg() -> dict:
    """Return the logged-in client's config dict."""
    return st.session_state.get("_client_cfg", {})


def current_client_name() -> str:
    return st.session_state.get("_client_name", "")


def is_admin() -> bool:
    return current_client_cfg().get("is_admin", False)


# ── Background engine: login + FIFO build + token resolve + live WS feed ──
class LiveEngine:
    def __init__(self, ledger_rows):
        self.lock = threading.Lock()
        self.latest_prices = {}       # token -> tick dict
        self.token_to_symbol = {}     # token -> meta
        self.closed_positions = []
        self.booked_mtm_total = 0.0
        self.status = "starting"
        self.error = None
        self.last_tick_ts = None      # datetime of most recently received tick
        # Positions/token-resolution/login are all network calls and can take
        # several seconds. Do them in a background thread so get_engine()
        # (and therefore the page) returns to the browser immediately instead
        # of blocking behind Streamlit's "connecting" spinner. The UI polls
        # self.status via the live fragment until this flips to "live".
        threading.Thread(target=self._start, args=(ledger_rows,), daemon=True).start()

    def _start(self, ledger_rows):
        try:
            open_positions, closed_positions = build_positions(ledger_rows, verbose=False)
            self.closed_positions = closed_positions
            self.booked_mtm_total = round(sum(c["BookedPnL"] for c in closed_positions), 2)

            resolved, unresolved = resolve_all(open_positions, MASTER_CACHE_PATH, MASTER_CACHE_MAX_AGE_HOURS)
            if not resolved:
                self.status = "error"
                self.error = "Nothing resolved from the ledger — check symbol/exchange/expiry spelling."
                return

            for r in resolved:
                # Match back to the original open_positions entry using
                # Exchange + Qty + AvgPrice rather than Symbol. Angel One's
                # instrument-master resolution returns the FULL contract
                # tradingsymbol for F&O (e.g. "NIFTY28APR2622500CE"), which
                # never equals the bare underlying symbol ("NIFTY") that
                # positions_builder.py stores — so a Symbol-based match
                # always missed for F&O and silently blanked out Expiry,
                # Segment, and PositionType for every F&O row. Qty/AvgPrice
                # are copied through resolve_all unchanged from the input
                # position, so they're a reliable join key; Symbol is kept
                # as a secondary check only for Equity, where it's still
                # accurate and helps disambiguate ties.
                def _matches(p, r=r):
                    same_exch = p["Exchange"] == r["exchange"]
                    same_qty = abs(float(p.get("Qty") or 0)) == abs(float(r.get("qty") or 0))
                    avg_p, avg_r = p.get("AvgPrice"), r.get("avgPrice")
                    same_avg = avg_p is not None and avg_r is not None and round(float(avg_p), 2) == round(float(avg_r), 2)
                    return same_exch and same_qty and same_avg

                src = next((p for p in open_positions if _matches(p)), {})
                if not src:
                    # Fall back to the old Symbol+Exchange match (covers
                    # Equity, where symbol formats do line up).
                    src = next((p for p in open_positions
                                if p["Symbol"] == r["symbol"] and p["Exchange"] == r["exchange"]), {})
                self.token_to_symbol[r["token"]] = {
                    "symbol": r["symbol"], "exchange": r["exchange"],
                    "qty": r.get("qty"), "avgPrice": r.get("avgPrice"),
                    "segment": src.get("Segment", "Other"),
                    "positionType": src.get("PositionType", "LONG"),
                    # Expiry may come back from the instrument-master lookup
                    # (F&O tokens resolve with an expiry) or from the ledger
                    # row itself, depending on which one has it.
                    "expiry": r.get("expiry") or src.get("Expiry") or src.get("ExpiryDate") or "",
                }

            totp = pyotp.TOTP(st.secrets["ANGEL_TOTP_SECRET"]).now()
            sc = SmartConnect(api_key=st.secrets["ANGEL_API_KEY"])
            data = sc.generateSession(st.secrets["ANGEL_CLIENT_CODE"], st.secrets["ANGEL_PASSWORD"], totp)
            if not data.get("status"):
                self.status = "error"
                self.error = f"Angel One login failed: {data}"
                return
            jwt_token = data["data"]["jwtToken"]
            feed_token = data["data"]["feedToken"]

            self._start_ws(jwt_token, feed_token, resolved)
            self.status = "live"
        except Exception as e:
            self.status = "error"
            self.error = str(e)

    def _chunk(self, resolved):
        by_exch = {}
        for r in resolved:
            by_exch.setdefault(r["exchangeType"], []).append(r["token"])
        batches, current, current_count = [], [], 0
        for exch_type, tokens in by_exch.items():
            for i in range(0, len(tokens), 200):
                chunk = tokens[i:i + 200]
                if current_count + len(chunk) > MAX_TOKENS_PER_SUBSCRIBE:
                    batches.append(current)
                    current, current_count = [], 0
                current.append({"exchangeType": exch_type, "tokens": chunk})
                current_count += len(chunk)
        if current:
            batches.append(current)
        return batches

    def _start_ws(self, jwt_token, feed_token, resolved):
        sws = SmartWebSocketV2(
            auth_token=jwt_token, api_key=st.secrets["ANGEL_API_KEY"],
            client_code=st.secrets["ANGEL_CLIENT_CODE"], feed_token=feed_token,
        )
        batches = self._chunk(resolved)

        def on_open(wsapp):
            for i, token_list in enumerate(batches):
                sws.subscribe(f"feed_{i}", SUBSCRIBE_MODE, token_list)
                time.sleep(0.3)

        def on_data(wsapp, message):
            token = str(message.get("token"))
            meta = self.token_to_symbol.get(token, {})
            with self.lock:
                prev = self.latest_prices.get(token, {})
                ltp = message.get("last_traded_price", 0) / 100.0
                qty = meta.get("qty")
                avg_price = meta.get("avgPrice")
                now = datetime.now(IST)
                tick = {
                    "token": token, "symbol": meta.get("symbol", token),
                    "exchange": meta.get("exchange", ""), "segment": meta.get("segment", "Other"),
                    "positionType": meta.get("positionType", "LONG"), "expiry": meta.get("expiry", ""), "ltp": ltp,
                    "prev_ltp": prev.get("ltp"),
                    "close": (message.get("closed_price", 0) / 100.0 if message.get("closed_price") else prev.get("close")),
                    "open": (message.get("open_price_of_the_day", 0) / 100.0 if message.get("open_price_of_the_day") else prev.get("open")),
                    "volume": message.get("volume_trade_for_the_day"), "qty": qty, "avgPrice": avg_price,
                    "mtm": round((ltp - avg_price) * qty, 2) if (qty is not None and avg_price is not None) else None,
                    "ts": now.isoformat(timespec="seconds"),
                }
                self.latest_prices[token] = tick
                self.last_tick_ts = now

        def on_error(wsapp, error):
            with self.lock:
                self.status = "error"
                self.error = f"WebSocket error: {error}"

        def on_close(wsapp, *a):
            with self.lock:
                self.status = "disconnected"

        sws.on_open = on_open
        sws.on_data = on_data
        sws.on_error = on_error
        sws.on_close = on_close
        threading.Thread(target=sws.connect, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return list(self.latest_prices.values()), self.last_tick_ts


@st.cache_resource(show_spinner="Fetching trade data...")
def get_engine(source_key: str, dealer_client_id: str | None, apps_script_url: str, sheet_name: str | None):
    """source_key is only part of the cache key (so switching client/source
    reruns this); dealer_client_id takes priority when present.
    """
    if dealer_client_id:
        rows = _fetch_dealer_ledger_rows(dealer_client_id)
    else:
        records = _fetch_ledger_records(apps_script_url, sheet_name)
        rows = load_trade_ledger_from_records(records)
    return LiveEngine(rows)


# ── News ─────────────────────────────────────────────────────────────────
# No single free API covers NSE + BSE + Mint + Business Standard + Times of
# India + more with one key, so instead this queries Google News' public RSS
# search (no key/auth needed), restricted to those publishers. Google News
# indexes each outlet's full archive, not just today, so this also surfaces
# older ("past") coverage for a symbol, not only breaking news.
#
# IMPORTANT: this deliberately does ONE separate query PER source rather
# than one query with all sources OR'd together. A single query like
# "SYMBOL stock (site:a.com OR site:b.com OR ... 9 sites)" makes Google
# News quietly loosen/drop terms it can't satisfy well across that many
# OR'd site: filters — which is how unrelated pages (a generic BSE listing
# page, an F1 racing article, an "Option Chain" static page) were slipping
# into results. Splitting into 9 narrow per-source queries — each requiring
# the ticker literally in the headline via intitle: — avoids that entirely.
NEWS_SOURCES = {
    "NSE": "nseindia.com",
    "BSE": "bseindia.com",
    "Mint": "livemint.com",
    "Business Standard": "business-standard.com",
    "Times of India": "timesofindia.indiatimes.com",
    "Economic Times": "economictimes.indiatimes.com",
    "Moneycontrol": "moneycontrol.com",
    "CNBC-TV18": "cnbctv18.com",
    "Reuters": "reuters.com",
}

# Several outlets (Economic Times especially) auto-publish a templated
# "<Stock> Share Price Today, <Stock> Stock Price Live NSE/BSE Updates"
# page per stock, regenerated daily — not an actual news story. Filtered
# both in the query itself (-intitle: exclusions) and again client-side as
# a safety net, since Google doesn't reliably honor every negative filter.
NEWS_TITLE_BLOCKLIST = (
    "share price today", "stock price live", "share price live",
    "nse/bse updates", "share price nse", "stock price nse",
)

# Broad market-recap headlines. These only ever leak in for index symbols
# (NIFTY, BANKNIFTY, SENSEX, FINNIFTY, ...) since intitle:"<symbol>" happily
# matches "Nifty ends lower", "Sensex settles ... points" daily-wrap stories
# that aren't actually news about a specific holding — they're the general
# market-close roundup that runs every trading day regardless. Applied only
# when the symbol itself is an index, so it never touches real stock tickers.
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "NIFTY50", "NIFTY 50"}
MARKET_WRAP_BLOCKLIST = (
    "closing bell", "sensex settles", "sensex falls", "sensex ends",
    "sensex closes", "stock market today", "market today", "nifty ends",
    "nifty trades", "nifty settles", "nifty closes", "taking stock",
    "opening bell", "market wrap", "sensex, nifty", "nifty, sensex",
)
NEWS_MAX_AGE_DAYS = 7


def _parse_news_rss(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        source_el = item.find("source")
        source = source_el.text.strip() if source_el is not None and source_el.text else ""
        # Google News titles are formatted "Headline - Source". Strip that
        # trailing " - Source" for a clean headline regardless of whether
        # <source> was present (normal case) or we have to fall back to
        # splitting the title itself (source tag missing).
        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)].strip()
        elif not source and " - " in title:
            title, source = title.rsplit(" - ", 1)
        try:
            # Google always sends this in GMT; strptime's %Z doesn't attach
            # real tzinfo, so dt comes back naive but is UTC-equivalent —
            # fine as long as we compare it against another naive UTC value
            # (see the utcnow() cutoff below), never against IST/aware time.
            dt = datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %Z")
        except ValueError:
            dt = None
        items.append({"title": title, "source": source, "link": link, "dt": dt, "pub_date_raw": pub_date})
    return items


def _fetch_one_source(symbol, domain, max_items):
    # intitle: forces the ticker to literally appear in the headline —
    # this is what actually keeps results on-topic. The -intitle: terms
    # knock out the auto-generated "Share Price Today" template pages at
    # the source, before they even count against max_items.
    exclusions = " ".join(f'-intitle:"{p}"' for p in ("Share Price Today", "Stock Price Live"))
    query = f'intitle:"{symbol}" {exclusions} site:{domain}'
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return _parse_news_rss(resp.content)[:max_items]


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_stock_news(symbol: str, per_source_max: int = 8, total_max: int = 25):
    """Latest coverage for one symbol from the past NEWS_MAX_AGE_DAYS days
    only, queried separately per source in NEWS_SOURCES (see module note
    above for why), merged and sorted newest-first. Cached 30 min per
    symbol so switching tabs or the live-tick refresh loop doesn't re-hit
    Google News on every rerun. Returns a list of
    {title, source, link, published, recency} dicts — recency is one of
    "new" (<24h old), "recent" (1-3 days), or "week" (3-7 days), computed
    at fetch time; since the cache TTL is 30 min, a bucket can drift stale
    by at most that much, which isn't visible at day-scale bucket sizes.
    """
    merged = []
    with ThreadPoolExecutor(max_workers=len(NEWS_SOURCES)) as pool:
        futures = {
            pool.submit(_fetch_one_source, symbol, domain, per_source_max): name
            for name, domain in NEWS_SOURCES.items()
        }
        for fut in as_completed(futures):
            try:
                merged.extend(fut.result())
            except requests.RequestException:
                continue  # one source failing shouldn't sink the whole fetch

    now = datetime.utcnow()
    cutoff = now - timedelta(days=NEWS_MAX_AGE_DAYS)
    is_index = symbol.strip().upper() in INDEX_SYMBOLS
    filtered = []
    seen_keys = set()  # (normalized title, link) — Google News RSS can hand
                        # back the same story twice for one source query
                        # (syndication/pagination overlap); drop the repeat.
    for it in merged:
        # Strict 7-day window: an item we can't date can't be verified as
        # within it, so — unlike a "best effort" feed — it gets dropped
        # rather than kept.
        if it["dt"] is None or it["dt"] < cutoff:
            continue
        low = it["title"].lower()
        if any(p in low for p in NEWS_TITLE_BLOCKLIST):
            continue
        if is_index and any(p in low for p in MARKET_WRAP_BLOCKLIST):
            continue
        dedup_key = (re.sub(r"\s+", " ", low).strip(), it["link"])
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        filtered.append(it)

    filtered.sort(key=lambda it: it["dt"], reverse=True)

    out = []
    for it in filtered[:total_max]:
        age = now - it["dt"]
        if age <= timedelta(hours=24):
            recency = "new"
        elif age <= timedelta(days=3):
            recency = "recent"
        else:
            recency = "week"
        out.append({
            "title": it["title"], "source": it["source"], "link": it["link"],
            "published": it["dt"].strftime("%d %b %Y, %I:%M %p"), "recency": recency,
            "dt": it["dt"],  # kept (not just the formatted string) so the "All
                             # stocks" view can merge+re-sort across symbols
        })
    return out


ALL_STOCKS_OPTION = "🌐 All my stocks"


def fetch_all_stock_news(symbols: list[str], per_symbol_max: int = 6, total_max: int = 60):
    """Merges fetch_stock_news across every symbol into one recency-sorted
    feed, each item tagged with which stock it's about. Bounded to
    per_symbol_max items per stock before merging, so one heavily-covered
    stock can't crowd out everything else. Runs the per-symbol fetches
    concurrently (each of which is itself already cached 30 min and
    internally parallel across sources) capped to a modest worker count —
    fetch_stock_news's own internal pool already opens up to 9 connections
    per symbol, so fanning out too many symbols at once would multiply
    that unnecessarily.
    """
    merged = []
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(symbols)))) as pool:
        futures = {pool.submit(fetch_stock_news, sym): sym for sym in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                items = fut.result()
            except Exception:
                continue  # one stock failing shouldn't sink the whole feed
            for it in items[:per_symbol_max]:
                merged.append({**it, "symbol": sym})

    merged.sort(key=lambda it: it["dt"], reverse=True)
    return merged[:total_max]


def render_news_section(symbols: list[str], *, scope_key: str, scope_noun: str):
    """Renders one news feed (selectbox + headline list) scoped to `symbols`.

    scope_key   — unique suffix for widget keys, so the Open-positions and
                  Closed-positions news pickers (rendered in separate tabs)
                  don't collide in Streamlit's session state.
    scope_noun  — used in empty-state / caption copy, e.g. "open positions".
    """
    if not symbols:
        st.caption(f"No {scope_noun} to show news for yet.")
        return
    options = [ALL_STOCKS_OPTION] + symbols
    picked = st.selectbox("Stock", options=options, key=f"_news_symbol_{scope_key}")
    if not picked:
        return

    show_all = picked == ALL_STOCKS_OPTION
    with st.spinner(f"Fetching news for {'all your ' + scope_noun if show_all else picked}..."):
        try:
            items = fetch_all_stock_news(symbols) if show_all else fetch_stock_news(picked)
        except Exception as exc:
            st.error(f"Couldn't fetch news: {exc}")
            return

    if not items:
        scope = f"your {scope_noun}" if show_all else f"\"{picked}\""
        st.caption(f"No headlines with {scope} in the title from the last "
                   f"{NEWS_MAX_AGE_DAYS} days across the tracked sources.")
        return

    RECENCY_BADGE = {
        "new": '<span class="news-badge news-badge-new">🟢 New</span>',
        "recent": '<span class="news-badge news-badge-recent">🕒 Recent</span>',
        "week": "",
    }
    for it in items:
        meta = it["source"] + (" · " + it["published"] if it["published"] else "")
        badge = RECENCY_BADGE.get(it["recency"], "")
        stock_badge = f'<span class="news-badge news-badge-stock">{it["symbol"]}</span>' if show_all else ""
        st.markdown(
            flat(f"""
            <div class="news-item news-{it['recency']}">
                <a href="{it['link']}" target="_blank" rel="noopener noreferrer" class="news-title">{it['title']}</a>{stock_badge}{badge}
                <div class="news-meta">{meta}</div>
            </div>
            """),
            unsafe_allow_html=True,
        )

    scope_text = f"across all {len(symbols)} of your {scope_noun}" if show_all else f"with \"{picked}\" in the title"
    st.caption(f"Headlines {scope_text}, past {NEWS_MAX_AGE_DAYS} days, across NSE, BSE, "
               "Mint, Business Standard, Times of India, Economic Times, Moneycontrol, CNBC-TV18 and "
               "Reuters — excluding auto-generated price-update pages. Cached 30 min per stock.")



def fmt_money(x):
    if x is None:
        return "-"
    return f"(₹{abs(x):,.2f})" if x < 0 else f"₹{x:,.2f}"


def fmt_qty(qty):
    if qty is None:
        return "-"
    return f"{abs(round(qty)):,}"


def fmt_time(ts):
    """ts is an ISO datetime string (possibly tz-aware); show HH:MM:SS only."""
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return ts


def fmt_datetime(ts):
    """ts is an ISO datetime string (possibly tz-aware); show date + time."""
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%d %b, %H:%M:%S")
    except ValueError:
        return ts


def fmt_sell_date(d):
    """d may be a date/datetime object or an ISO-ish string; show a short date."""
    if not d:
        return "-"
    if isinstance(d, str):
        try:
            d = datetime.fromisoformat(d)
        except ValueError:
            return d
    try:
        return d.strftime("%d %b %Y")
    except AttributeError:
        return str(d)


def fmt_expiry(e):
    """e may be a date/datetime object or a string like '25AUG2026'/'2026-08-25'/
    '2026-08-25 00:00:00' (the last of these is what positions_builder's
    _norm() produces for CSV-sourced — i.e. Google Sheets — ledger rows,
    since it str()s a full Timestamp rather than a bare date)."""
    if not e:
        return "-"
    if isinstance(e, str):
        for pattern in ("%d%b%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(e, pattern).strftime("%d %b %Y")
            except ValueError:
                continue
        # Last resort: let pandas' looser parser have a go before giving up.
        parsed = pd.to_datetime(e, errors="coerce")
        if pd.notna(parsed):
            return parsed.strftime("%d %b %Y")
        return e  # unrecognized format — show as-is rather than hide it
    try:
        return e.strftime("%d %b %Y")
    except AttributeError:
        return str(e)


def alt_dark(chart):
    """Apply a shared dark, transparent-background theme to an Altair chart."""
    return (
        chart.configure_view(strokeWidth=0)
        .configure_axis(
            gridColor="#1e2733", domainColor="#1e2733", tickColor="#1e2733",
            labelColor="#7f8ba3", titleColor="#7f8ba3", labelFontSize=10.5, titleFontSize=11,
        )
        .configure_legend(labelColor="#7f8ba3", titleColor="#7f8ba3")
        .properties(background="transparent")
    )


def style_pnl_table(df, cols):
    """Return a pandas Styler that colors P&L-type columns green/red."""
    def _color(v):
        if pd.isna(v):
            return ""
        return "color: #2ee6a6; font-weight:600;" if v >= 0 else "color: #ff5c7a; font-weight:600;"

    fmt = {c: "₹{:,.2f}".format for c in cols if c in df.columns}
    styler = df.style.format(fmt)
    # pandas >=2.1 renamed Styler.applymap -> Styler.map (and removed
    # applymap entirely in pandas 3.x), so pick whichever exists at runtime.
    color_fn = styler.map if hasattr(styler, "map") else styler.applymap
    for c in cols:
        if c in df.columns:
            styler = color_fn(_color, subset=[c])
            color_fn = styler.map if hasattr(styler, "map") else styler.applymap
    return styler


# ── Live fragment: KPI cards + open/closed tables, refreshes on its own ───
@st.fragment(run_every=TICK_REFRESH_SECONDS)
def render_live(engine: "LiveEngine"):
    if engine.status == "error":
        st.markdown(status_pill("error", "Feed error"), unsafe_allow_html=True)
        st.error(f"Feed failed to start: {engine.error}")
        return
    elif engine.status == "starting":
        st.markdown(status_pill("warn", "Starting up..."), unsafe_allow_html=True)
        st.caption("Resolving instrument tokens and logging into Angel One — this can take a few seconds on a cold start.")
        return

    ticks, last_tick_ts = engine.snapshot()

    now_ist = datetime.now(IST)
    if engine.status == "disconnected":
        status_html = status_pill("error", "Disconnected — restart feed in sidebar")
    else:
        age = f"{(now_ist - last_tick_ts).seconds}s ago" if last_tick_ts else "waiting for first tick..."
        status_html = status_pill("live", f"Live • last tick {age}")

    st.markdown(
        flat(f"""
        <div class="top-bar">
            {status_html}
            <div class="top-clock">
                <span class="date-part">{now_ist.strftime("%A, %d %b %Y")}</span>
                <span>{now_ist.strftime("%H:%M:%S")}</span>
                <span class="tz-badge">IST</span>
            </div>
        </div>
        """),
        unsafe_allow_html=True,
    )

    equity = [t for t in ticks if t.get("segment") == "Equity"]
    fo = [t for t in ticks if t.get("segment") == "F&O"]
    other = [t for t in ticks if t.get("segment") not in ("Equity", "F&O")]

    def seg_totals(rows):
        # Capital deployed must use abs(qty): a short position has a
        # negative qty, and summing signed qty*avgPrice was silently
        # *subtracting* short positions' cost basis from the total instead
        # of adding it — that was the "wrong Investment Value" bug.
        buy_value = sum(abs(r["qty"] or 0) * (r["avgPrice"] or 0) for r in rows)
        mtm = sum(r["mtm"] for r in rows if r["mtm"] is not None)
        # Today's move only (ltp vs previous close), signed qty on purpose:
        # for a short, a falling price is a gain, and (ltp-close) is
        # negative while qty is negative too, so the product comes out
        # positive automatically.
        day_pnl = sum(
            (r["ltp"] - r["close"]) * r["qty"]
            for r in rows if r.get("close") not in (None, 0) and r.get("qty") is not None
        )
        return buy_value, mtm, day_pnl

    eq_buy, eq_mtm, eq_day = seg_totals(equity)
    fo_buy, fo_mtm, fo_day = seg_totals(fo)
    other_buy, other_mtm, other_day = seg_totals(other)
    seg_open_totals = {
        "Equity": (eq_buy, eq_mtm, eq_day),
        "F&O": (fo_buy, fo_mtm, fo_day),
        "Other": (other_buy, other_mtm, other_day),
    }
    investment_value = eq_buy + fo_buy
    current_mtm = eq_mtm + fo_mtm
    total_mtm = engine.booked_mtm_total + current_mtm
    day_pnl_total = eq_day + fo_day
    day_pnl_pct = (day_pnl_total / investment_value * 100) if investment_value else 0.0

    st.markdown(
        hero_block(
            "Live Position MTM",
            fmt_money(total_mtm),
            "current MTM — booked plus open",
            is_positive=total_mtm >= 0,
            pill_text="Profit" if total_mtm >= 0 else "Loss",
        ),
        unsafe_allow_html=True,
    )
    st.markdown(
        hero_tiles([
            ("Investment Value", fmt_money(investment_value), ""),
            (
                "Today's P&L",
                f"{fmt_money(day_pnl_total)} ({'+' if day_pnl_pct >= 0 else ''}{day_pnl_pct:.2f}%)",
                "positive" if day_pnl_total >= 0 else "negative",
            ),
            ("Booked MTM", fmt_money(engine.booked_mtm_total), "positive" if engine.booked_mtm_total >= 0 else "negative"),
            ("Open MTM", fmt_money(current_mtm), "positive" if current_mtm >= 0 else "negative"),
            ("Current MTM", fmt_money(total_mtm), "positive" if total_mtm >= 0 else "negative"),
        ]),
        unsafe_allow_html=True,
    )

    tab_open, tab_closed, tab_news = st.tabs(["📈 Open positions", "✅ Closed positions", "📰 News"])

    with tab_open:
        open_segments = [("Equity", equity), ("F&O", fo)]
        if other:
            open_segments.append(("Other", other))
        # One sub-tab per segment so picking "F&O" shows only F&O, not every
        # segment stacked one after another.
        open_seg_tabs = st.tabs([f"{label} ({len(rows)})" for label, rows in open_segments])
        for (label, rows), seg_tab in zip(open_segments, open_seg_tabs):
          with seg_tab:
            count_badge = f'<span class="badge">{len(rows)}</span>'
            st.markdown(f'<div class="section-label">{label} {count_badge}</div>', unsafe_allow_html=True)

            # One-line summary scoped to THIS segment only — switching to the
            # F&O tab shows F&O's own investment/running-P&L/today's-P&L,
            # not the combined book total.
            seg_buy, seg_mtm, seg_day = seg_open_totals.get(label, (0.0, 0.0, 0.0))
            seg_mtm_pct = (seg_mtm / seg_buy * 100) if seg_buy else 0.0
            seg_day_pct = (seg_day / seg_buy * 100) if seg_buy else 0.0
            st.markdown(
                seg_summary_line([
                    (f"{label} Investment", fmt_money(seg_buy), ""),
                    (
                        "Running P&L",
                        f"{fmt_money(seg_mtm)} ({'+' if seg_mtm >= 0 else ''}{seg_mtm_pct:.2f}%)",
                        "kpi-pos" if seg_mtm >= 0 else "kpi-neg",
                    ),
                    (
                        "Today's P&L",
                        f"{fmt_money(seg_day)} ({'+' if seg_day >= 0 else ''}{seg_day_pct:.2f}%)",
                        "kpi-pos" if seg_day >= 0 else "kpi-neg",
                    ),
                ]),
                unsafe_allow_html=True,
            )

            if not rows:
                st.caption("No open positions in this segment.")
                continue

            def _daily_gain_pct(x):
                # Price-move % (ltp vs prev close), then flipped for shorts:
                # a short position LOSES when the price rises, so its
                # "daily gain" is the mirror image of the raw price move.
                close = x.get("close")
                if not close:
                    return None
                raw_pct = (x["ltp"] - close) / close * 100
                pos_type = (x.get("positionType") or "LONG").upper()
                return raw_pct if pos_type == "LONG" else -raw_pct

            # F&O positions care about expiry more than exchange (it's
            # always NFO/BFO); Equity/Other show exchange as before.
            second_col_label = "Expiry" if label == "F&O" else "Exchange"

            # Highest daily gain first, always — never a fixed/pinned order.
            # Positions with no price yet (None) sort to the bottom.
            rows_html = []
            sorted_rows = sorted(
                rows,
                key=lambda x: (_daily_gain_pct(x) is None, -(_daily_gain_pct(x) or 0)),
            )
            for idx, r in enumerate(sorted_rows):
                pos_type = (r.get("positionType") or "LONG").upper()
                type_cls = "long" if pos_type == "LONG" else "short"
                close = r.get("close")
                day_pct = _daily_gain_pct(r)  # this position's actual gain/loss %, short-adjusted
                day_cls = "pt-cell pos" if (day_pct or 0) >= 0 else "pt-cell neg"
                day_txt = f"{'+' if (day_pct or 0) >= 0 else ''}{day_pct:.2f}%" if day_pct is not None else "–"
                # The single best-performing row in this segment always gets
                # the glow — if it's a genuine gain it reads "Top Gain"; if
                # every position in the segment is red today, the least-bad
                # one still gets marked so there's always a clear leader.
                is_top = idx == 0 and day_pct is not None
                row_cls = "pos-table-row cols-open top-gain-row" if is_top else "pos-table-row cols-open"
                if is_top and day_pct > 0:
                    leader_badge = f'<span class="pt-leader-badge">🔥 Top Gain</span>'
                elif is_top:
                    leader_badge = f'<span class="pt-leader-badge">🛡️ Least Loss</span>'
                else:
                    leader_badge = ""
                mtm = r.get("mtm")
                mtm_cls = "pt-cell pos" if (mtm or 0) >= 0 else "pt-cell neg"
                mtm_arrow = "▲" if (mtm or 0) >= 0 else "▼"
                second_col_value = fmt_expiry(r.get("expiry")) if label == "F&O" else r.get("exchange", "-")
                rows_html.append(flat(f"""
                    <div class="{row_cls}">
                        <div class="pt-symbol">
                            <span class="pt-symbol-name">{r.get('symbol', '-')}</span>
                            <span class="pt-tag {type_cls}">{pos_type}</span>
                            {leader_badge}
                        </div>
                        <div class="pt-cell muted">{second_col_value}</div>
                        <div class="pt-cell">{fmt_qty(r.get('qty'))}</div>
                        <div class="pt-cell">{fmt_money(r.get('avgPrice'))}</div>
                        <div class="pt-cell">{fmt_money(r.get('ltp'))}</div>
                        <div class="{day_cls}">{day_txt}</div>
                        <div class="{mtm_cls}">{mtm_arrow} {fmt_money(mtm)}</div>
                        <div class="pt-cell muted">{fmt_datetime(r.get('ts'))}</div>
                    </div>
                """))
            table_html = flat(f"""
                <div class="pos-table-wrap">
                    <div class="pos-table">
                        <div class="pos-table-head cols-open">
                            <div>Symbol</div><div>{second_col_label}</div><div>Qty</div>
                            <div>Avg Price</div><div>CMP</div><div>Day Chg %</div>
                            <div>MTM P&amp;L</div><div>Last Tick</div>
                        </div>
                        {"".join(rows_html)}
                    </div>
                </div>
            """)
            st.markdown(table_html, unsafe_allow_html=True)

    with tab_closed:
        if not engine.closed_positions:
            st.caption("No closed positions in this ledger.")
        else:
            closed = engine.closed_positions
            wins = sum(1 for c in closed if c["BookedPnL"] >= 0)
            losses = len(closed) - wins
            win_rate = wins / len(closed) * 100 if closed else 0

            # All realized FIFO trade legs across every closed position —
            # used for the leg count, best/worst single trade, and the
            # average P&L per realized trade below.
            all_legs = [
                (t.get("Pnl", 0.0), p.get("Symbol", "-"))
                for p in closed for t in p.get("Trades", [])
            ]
            realized_trades = len(all_legs)
            if all_legs:
                best_pnl, best_symbol = max(all_legs, key=lambda x: x[0])
                worst_pnl, worst_symbol = min(all_legs, key=lambda x: x[0])
            else:
                best_pnl = worst_pnl = 0.0
                best_symbol = worst_symbol = "-"
            avg_pnl_trade = (engine.booked_mtm_total / realized_trades) if realized_trades else 0.0

            st.markdown(
                hero_block(
                    "Booked Ledger — from trade ledger FIFO",
                    fmt_money(engine.booked_mtm_total),
                    "total booked profit",
                    is_positive=engine.booked_mtm_total >= 0,
                    pill_text="Booked",
                ),
                unsafe_allow_html=True,
            )
            st.markdown(
                hero_tiles([
                    ("Closed Positions", str(len(closed)), ""),
                    ("Realized Trades (FIFO Legs)", str(realized_trades), ""),
                    ("Win Rate", f"{win_rate:.0f}%", ""),
                    ("Best Trade", f"{best_symbol} · {fmt_money(best_pnl)}", "positive" if best_pnl >= 0 else "negative"),
                    ("Worst Trade", f"{worst_symbol} · {fmt_money(worst_pnl)}", "positive" if worst_pnl >= 0 else "negative"),
                    ("Avg P&L / Trade", fmt_money(avg_pnl_trade), "positive" if avg_pnl_trade >= 0 else "negative"),
                    ("Current Open MTM", fmt_money(current_mtm), "positive" if current_mtm >= 0 else "negative"),
                ]),
                unsafe_allow_html=True,
            )

            def _closed_sell_date(c):
                dates = [t.get("SellDate") for t in c.get("Trades", []) if t.get("SellDate")]
                return max(dates) if dates else None

            def closed_row_html(c, is_top=False):
                pnl = c["BookedPnL"]
                pnl_cls = "pt-cell pos" if pnl >= 0 else "pt-cell neg"
                pnl_arrow = "▲" if pnl >= 0 else "▼"
                row_cls = "pos-table-row cols-closed top-gain-row" if is_top else "pos-table-row cols-closed"
                if is_top and pnl > 0:
                    leader_badge = '<span class="pt-leader-badge">🔥 Top Gain</span>'
                elif is_top:
                    leader_badge = '<span class="pt-leader-badge">🛡️ Least Loss</span>'
                else:
                    leader_badge = ""
                sell_date = fmt_sell_date(_closed_sell_date(c))
                return flat(f"""
                    <div class="{row_cls}">
                        <div class="pt-symbol">
                            <span class="pt-symbol-name">{c.get('Symbol', '-')}</span>
                            {leader_badge}
                        </div>
                        <div class="pt-cell muted">{c.get('Exchange', '-')}</div>
                        <div class="pt-cell">{fmt_qty(c.get('Qty'))}</div>
                        <div class="pt-cell">{fmt_money(c.get('AvgBuyPrice'))}</div>
                        <div class="pt-cell">{fmt_money(c.get('AvgSellPrice'))}</div>
                        <div class="pt-cell muted">{sell_date}</div>
                        <div class="{pnl_cls}">{pnl_arrow} {fmt_money(pnl)}</div>
                    </div>
                """)

            closed_equity = [c for c in closed if c.get("Segment") == "Equity"]
            closed_fo = [c for c in closed if c.get("Segment") == "F&O"]
            closed_segments = [("Equity", closed_equity), ("F&O", closed_fo)]
            closed_other = [c for c in closed if c.get("Segment") not in ("Equity", "F&O")]
            if closed_other:
                closed_segments.append(("Other", closed_other))

            # One sub-tab per segment so picking "F&O" shows only F&O, not every
            # segment stacked one after another.
            closed_seg_tabs = st.tabs([f"{label} ({len(rows)})" for label, rows in closed_segments])
            for (label, rows), seg_tab in zip(closed_segments, closed_seg_tabs):
              with seg_tab:
                if not rows:
                    st.markdown(f'<div class="section-label">{label} <span class="badge">0</span></div>', unsafe_allow_html=True)
                    st.caption(f"No closed {label.lower()} positions in this ledger.")
                    continue
                seg_win_rate = sum(1 for c in rows if c["BookedPnL"] >= 0) / len(rows) * 100
                st.markdown(
                    f'<div class="section-label">{label} <span class="badge">{len(rows)}</span></div>',
                    unsafe_allow_html=True,
                )
                # One-line summary scoped to THIS segment's closed positions
                # only — cost basis deployed here vs. what was booked here.
                seg_invested = sum(abs(c.get("Qty") or 0) * (c.get("AvgBuyPrice") or 0) for c in rows)
                seg_booked = sum(c["BookedPnL"] for c in rows)
                seg_booked_pct = (seg_booked / seg_invested * 100) if seg_invested else 0.0
                st.markdown(
                    seg_summary_line([
                        (f"{label} Investment", fmt_money(seg_invested), ""),
                        (
                            "Booked P&L",
                            f"{fmt_money(seg_booked)} ({'+' if seg_booked >= 0 else ''}{seg_booked_pct:.2f}%)",
                            "kpi-pos" if seg_booked >= 0 else "kpi-neg",
                        ),
                        ("Win rate", f"{seg_win_rate:.0f}%", ""),
                    ]),
                    unsafe_allow_html=True,
                )
                # Whichever row has the single highest booked P&L in this
                # segment always gets the glow — a genuine gain reads "Top
                # Gain", otherwise the least-bad loss reads "Least Loss".
                sorted_rows = sorted(rows, key=lambda x: x["BookedPnL"], reverse=True)
                best_pnl = max((c["BookedPnL"] for c in rows), default=None)
                rows_html = [
                    closed_row_html(
                        c,
                        is_top=(best_pnl is not None and c["BookedPnL"] == best_pnl),
                    )
                    for c in sorted_rows
                ]
                table_html = flat(f"""
                    <div class="pos-table-wrap">
                        <div class="pos-table">
                            <div class="pos-table-head cols-closed">
                                <div>Symbol</div><div>Exchange</div><div>Qty</div>
                                <div>Avg Buy</div><div>Avg Sell</div><div>Sell Date</div>
                                <div>Booked P&amp;L</div>
                            </div>
                            {"".join(rows_html)}
                        </div>
                    </div>
                """)
                st.markdown(table_html, unsafe_allow_html=True)

            # ── Charts — rendered below every position table so tables stay
            # the primary focus. Chart 1 stays full-width since a date axis
            # needs the room; charts 2 and 3 are compact enough to share a row.
            # Chart 1: cumulative booked profit over time, EQUITY ONLY — F&O
            # legs are intentionally excluded so the trend line reflects pure
            # equity performance and isn't skewed by F&O's larger swings.
            legs = []
            for p in closed:
                if p.get("Segment") != "Equity":
                    continue
                for t in p.get("Trades", []):
                    if t.get("SellDate"):
                        legs.append({"SellDate": t["SellDate"], "Pnl": t["Pnl"], "Segment": p.get("Segment", "Other")})

            if legs:
                # Multiple FIFO legs can share the exact same SellDate (e.g.
                # several lots/symbols closed on one day). If we cumsum one
                # row per leg, ties on SellDate get an arbitrary order, which
                # produces a zig-zagging line and — since the tooltip snaps to
                # the nearest x — a hover value that doesn't match what's
                # visually plotted at that point. Collapse to one point per
                # calendar day first so the x-axis is strictly increasing.
                chart_df = pd.DataFrame(legs)
                chart_df["SellDate"] = pd.to_datetime(chart_df["SellDate"]).dt.normalize()
                chart_df = chart_df.groupby("SellDate", as_index=False)["Pnl"].sum().sort_values("SellDate")
                chart_df["Cumulative"] = chart_df["Pnl"].cumsum()
                with st.container(border=True):
                    st.markdown('<div class="section-label">Cumulative booked profit over time (Equity)</div>', unsafe_allow_html=True)
                    area = alt.Chart(chart_df).mark_area(
                        line={"color": "#34d5c8", "strokeWidth": 2},
                        interpolate="monotone",
                        fillOpacity=0.15,
                        color=alt.Gradient(
                            gradient="linear",
                            stops=[alt.GradientStop(color="#34d5c8", offset=0), alt.GradientStop(color="transparent", offset=1)],
                            x1=1, x2=1, y1=1, y2=0,
                        ),
                    ).encode(
                        x=alt.X("SellDate:T", title=None),
                        y=alt.Y("Cumulative:Q", title="Cumulative ₹"),
                        tooltip=[alt.Tooltip("SellDate:T", title="Date"), alt.Tooltip("Cumulative:Q", title="Cumulative", format=",.0f")],
                    ).properties(height=240)
                    st.altair_chart(alt_dark(area), use_container_width=True)
            else:
                st.caption("No sell-dated trade legs available yet to plot a cumulative profit trend.")

            # Charts 2 + 3 share one row.
            top_df = pd.DataFrame(closed)[["Symbol", "BookedPnL"]].copy()
            top_df["AbsPnl"] = top_df["BookedPnL"].abs()
            top_df = top_df.sort_values("AbsPnl", ascending=False).head(12).drop(columns="AbsPnl")
            top_df["Direction"] = top_df["BookedPnL"].apply(lambda v: "Profit" if v >= 0 else "Loss")
            win_df = pd.DataFrame({"Outcome": ["Profitable", "Loss-making"], "Count": [wins, losses]})

            col_movers, col_donut = st.columns(2)
            with col_movers:
                with st.container(border=True):
                    st.markdown('<div class="section-label">Biggest movers — booked P&amp;L</div>', unsafe_allow_html=True)
                    bar = alt.Chart(top_df).mark_bar(cornerRadiusEnd=4).encode(
                        x=alt.X("BookedPnL:Q", title="Booked P&L (₹)"),
                        y=alt.Y("Symbol:N", sort="-x", title=None),
                        color=alt.Color(
                            "Direction:N",
                            scale=alt.Scale(domain=["Profit", "Loss"], range=["#2ee6a6", "#ff5c7a"]),
                            legend=None,
                        ),
                        tooltip=[alt.Tooltip("Symbol:N"), alt.Tooltip("BookedPnL:Q", title="Booked P&L", format=",.0f")],
                    ).properties(height=max(220, 24 * len(top_df)))
                    st.altair_chart(alt_dark(bar), use_container_width=True)

            with col_donut:
                with st.container(border=True):
                    st.markdown('<div class="section-label">Win / loss split</div>', unsafe_allow_html=True)
                    donut = alt.Chart(win_df).mark_arc(innerRadius=65, cornerRadius=3).encode(
                        theta=alt.Theta("Count:Q"),
                        color=alt.Color(
                            "Outcome:N",
                            scale=alt.Scale(domain=["Profitable", "Loss-making"], range=["#2ee6a6", "#ff5c7a"]),
                            legend=alt.Legend(orient="right", title=None),
                        ),
                        tooltip=[alt.Tooltip("Outcome:N"), alt.Tooltip("Count:Q")],
                    ).properties(height=220)
                    st.altair_chart(alt_dark(donut), use_container_width=True)

    with tab_news:
        st.markdown('<div class="section-label">News by stock</div>', unsafe_allow_html=True)
        open_symbols = sorted({t.get("symbol") for t in ticks if t.get("symbol")})
        closed_symbols = sorted({c.get("Symbol") for c in engine.closed_positions if c.get("Symbol")})

        news_tab_open, news_tab_closed = st.tabs([
            f"📈 Open positions ({len(open_symbols)})",
            f"✅ Closed positions ({len(closed_symbols)})",
        ])
        with news_tab_open:
            render_news_section(open_symbols, scope_key="open", scope_noun="open positions")
        with news_tab_closed:
            render_news_section(closed_symbols, scope_key="closed", scope_noun="closed positions")


# ── UI ──────────────────────────────────────────────────────────────────
def main():
    if not check_password():
        return

    inject_theme()

    client_name_disp = current_client_name()
    st.markdown(
        flat(f"""
        <div class="db-header">
            <div class="db-title">
                <div class="db-icon">📊</div>
                <h1>Booked Profit Dashboard</h1>
            </div>
            <div style="font-size:.82rem;color:var(--muted);">
                Portfolio of &nbsp;<strong style="color:var(--text);">{client_name_disp}</strong>
            </div>
        </div>
        """),
        unsafe_allow_html=True,
    )

    cfg          = current_client_cfg()
    client_name  = current_client_name()
    script_url   = cfg.get("apps_script_url", "")
    sheet_tab    = cfg.get("sheet_name", "") or ""
    dealer_cid   = cfg.get("dealer_client_id", "") or ""

    with st.sidebar:
        # ── Client badge ────────────────────────────────────────
        st.markdown(
            flat(f"""
            <div style="background:var(--panel-2);border:1px solid var(--border);
                        border-radius:12px;padding:12px 14px;margin-bottom:14px;">
              <div style="font-size:.68rem;font-weight:700;text-transform:uppercase;
                          letter-spacing:.06em;color:var(--muted);margin-bottom:4px;">
                Logged in as
              </div>
              <div style="font-size:1rem;font-weight:700;color:var(--text);">
                {client_name}
              </div>
              <div style="font-size:.72rem;color:var(--muted);margin-top:2px;">
                ID: {st.session_state.get("_client_id","?")}
                {"&nbsp;&nbsp;🔑 Admin" if is_admin() else ""}
              </div>
            </div>
            """),
            unsafe_allow_html=True,
        )

        if st.button("🚪 Sign out", use_container_width=True):
            for k in ["_client_ok","_client_id","_client_cfg","_client_name","_login_failed"]:
                st.session_state.pop(k, None)
            get_engine.clear()
            st.rerun()

        st.divider()

        # Admin: let them pick a different client to view
        if is_admin():
            clients = _get_clients()
            non_admin = {k: v for k, v in clients.items() if not v.get("is_admin")}
            if non_admin:
                chosen = st.selectbox(
                    "View client",
                    options=["(All merged)"] + list(non_admin.keys()),
                    format_func=lambda k: k if k == "(All merged)"
                                          else non_admin[k].get("display_name", k),
                    key="_admin_client_view",
                )
                if chosen != "(All merged)":
                    script_url = non_admin[chosen].get("apps_script_url", script_url)
                    sheet_tab  = non_admin[chosen].get("sheet_name", "") or ""
                    dealer_cid = non_admin[chosen].get("dealer_client_id", "") or ""

        if st.button("🔄 Restart feed / refetch sheet", use_container_width=True):
            get_engine.clear()
            st.rerun()
        st.caption(f"Live tables refresh every {TICK_REFRESH_SECONDS}s, tick by tick.")

        st.divider()
        access_log = _get_access_log()
        with st.expander(f"🔐 Access log ({len(access_log)})"):
            if not access_log:
                st.caption("No entries yet.")
            else:
                log_df = pd.DataFrame(
                    [{"Name": e["name"], "Time": e["ts"].strftime("%d %b %Y, %I:%M:%S %p")}
                     for e in reversed(access_log[-100:])]
                )
                st.dataframe(log_df, hide_index=True, use_container_width=True)
                st.caption(
                    f"Showing latest {min(100, len(access_log))} of {len(access_log)} (IST). "
                    "In-memory only, resets on app restart/redeploy."
                )

    if not dealer_cid and not script_url:
        st.warning(
            f"No trade data source configured for client **{client_name}**. "
            "Ask your administrator to add a `dealer_client_id` (direct broker "
            "feed) or an Apps Script URL to the app secrets."
        )
        return

    source_key = f"dealer:{dealer_cid}" if dealer_cid else f"sheet:{script_url}:{sheet_tab}"
    engine = get_engine(source_key, dealer_cid or None, script_url, sheet_tab or None)
    render_live(engine)


if __name__ == "__main__":
    main()
