"""
Downloads Angel One's daily instrument master and resolves rows from your
Excel security list (Symbol, Exchange, Instrument Type, Expiry, CE/PE) into
the (exchangeType, token) pairs the WebSocket needs.

Angel One publishes the master at this URL (same one used by most SmartAPI
Python samples, including whatever Roh.py already leans on for token lookup):
    https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json

Each master entry looks roughly like:
    {
      "token": "3045", "symbol": "SBIN-EQ", "name": "SBIN",
      "expiry": "", "strike": "-1.000000", "lotsize": "1",
      "instrumenttype": "", "exch_seg": "NSE", "tick_size": "5.000000"
    }
For derivatives, instrumenttype is one of FUTSTK/FUTIDX/OPTSTK/OPTIDX,
expiry is like "28MAR2024", and strike is in paise (divide by 100), with
symbol ending in CE/PE for options.

NOTE: Angel One occasionally tweaks field names/values in this file. If
matches come back empty, print a few raw rows for your symbol (see
`debug_dump` below) and adjust the filters — this is the one part of the
pipeline most likely to need a tweak on their end, not a bug in the logic.

WebSocket exchangeType codes (different numbering than exch_seg strings!):
    NSE_CM=1, NSE_FO=2, BSE_CM=3, BSE_FO=4, MCX_FO=5, NCX_FO=7, CDE_FO=13
"""
import json
import os
import re
import time
import pandas as pd
import requests

MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

EXCH_SEG_TO_WS_TYPE = {
    "NSE": 1,   # NSE_CM
    "NFO": 2,   # NSE_FO
    "BSE": 3,   # BSE_CM
    "BFO": 4,   # BSE_FO
    "MCX": 5,   # MCX_FO
    "NCDEX": 7, # NCX_FO
    "CDS": 13,  # CDE_FO
}


def load_master(cache_path, max_age_hours):
    if os.path.exists(cache_path):
        age_hours = (time.time() - os.path.getmtime(cache_path)) / 3600
        if age_hours < max_age_hours:
            with open(cache_path, "r") as f:
                return json.load(f)
    resp = requests.get(MASTER_URL, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    with open(cache_path, "w") as f:
        json.dump(data, f)
    return data


def debug_dump(master, symbol_substring, limit=10):
    """Print raw master rows matching a symbol/name substring — use this in a
    REPL when a row in your Excel isn't resolving, to see the actual field
    values Angel One is using today."""
    hits = [m for m in master if symbol_substring.upper() in (m.get("name", "") + m.get("symbol", "")).upper()]
    for m in hits[:limit]:
        print(m)
    return hits


def _norm(s):
    return str(s).strip().upper() if s is not None else ""


_EXPIRY_RE = re.compile(r"^\d{1,2}[A-Z]{3}\d{4}$")


def _norm_expiry(value):
    """
    Normalize an expiry value to Angel One's own 'DDMMMYYYY' format (e.g.
    '29SEP2026'), whatever form it arrives in — an already-correct string, a
    pandas Timestamp, an ISO string like '2026-09-29 00:00:00', 'dd/mm/yyyy',
    etc. Excel dates commonly come back through pandas as Timestamp objects
    (str() -> '2026-09-29 00:00:00'), which never string-equals Angel One's
    master format — that mismatch silently broke FUT/OPT expiry filtering.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if _EXPIRY_RE.match(s.upper()):
        return s.upper()  # already Angel One's own format — leave as-is, don't risk misparsing it
    year_first = bool(re.match(r"^\d{4}[-/]", s))  # e.g. '2026-09-29 00:00:00' — unambiguous
    ts = pd.to_datetime(s, errors="coerce", dayfirst=not year_first)
    if pd.isna(ts):
        return s.upper()  # unparseable — fall back to raw normalized string
    return ts.strftime("%d%b%Y").upper()


CASH_TO_DERIV_EXCH_SEG = {
    "NSE": "NFO",  # Angel One files all NSE futures/options under exch_seg 'NFO',
    "BSE": "BFO",  # NOT 'NSE' — that segment is cash/equity only. Same for BSE/BFO.
}                  # A row whose Excel 'Exchange' says 'NSE' for a FUT/OPT contract
                   # must be looked up (and WS-subscribed) under NFO, not NSE.


def resolve_row(master_by_name, row):
    """
    row: dict with keys Symbol, Exchange, InstrumentType, Expiry, OptionType (CE/PE/''), Strike (optional)
    Optionally carries Qty / AvgPrice (e.g. from positions_builder.build_open_positions) —
    these pass through unchanged onto the returned dict if present.
    Returns dict {symbol, exchange, exchangeType, token, [qty, avgPrice]} or None if unresolved.
    """
    symbol = _norm(row.get("Symbol"))
    exchange = _norm(row.get("Exchange"))  # the CASH exchange as typed in the sheet, e.g. 'NSE'
    instrument_type = _norm(row.get("InstrumentType"))  # EQ / FUT / OPT / F&O / FO (see below)
    expiry = _norm_expiry(row.get("Expiry"))
    option_type = _norm(row.get("OptionType"))  # CE / PE / ''
    strike = row.get("Strike")
    has_strike = strike not in (None, "", 0) and not (isinstance(strike, str) and _norm(strike) in ("", "-1", "0"))

    candidates = master_by_name.get(symbol, [])
    if not candidates:
        return None

    # positions_builder's own segment classifier is lenient — it accepts "F&O"/"FO"
    # as well as "FUT*"/"OPT*" (see _segment_for). Match that here too, or a row
    # whose Instrument Type literally says "F&O" falls through BOTH the FUT and
    # OPT branches below, skips instrument-type/expiry filtering entirely, and
    # resolve_row silently returns filtered[0] — some other contract (could be
    # the equity, or a different expiry/strike) with a similar but wrong price.
    # When the label is generic, use OptionType/Strike to tell FUT from OPT.
    is_fut_label = instrument_type.startswith("FUT")
    is_opt_label = instrument_type.startswith("OPT")
    is_generic_fo = instrument_type in ("F&O", "FO", "FNO")
    is_fut = is_fut_label or (is_generic_fo and not option_type and not has_strike)
    is_opt = is_opt_label or (is_generic_fo and (option_type in ("CE", "PE") or has_strike))

    # Derivatives live under a different exch_seg than cash — resolve THAT segment
    # before filtering candidates, or every FUT/OPT row gets filtered out at this
    # very first step (their exch_seg is 'NFO'/'BFO', never 'NSE'/'BSE').
    lookup_seg = CASH_TO_DERIV_EXCH_SEG.get(exchange, exchange) if (is_fut or is_opt) else exchange

    filtered = [c for c in candidates if _norm(c.get("exch_seg")) == lookup_seg]

    if instrument_type == "EQ" or (not instrument_type and not is_generic_fo):
        filtered = [c for c in filtered if _norm(c.get("instrumenttype")) in ("", "EQ")]
    elif is_fut:
        filtered = [c for c in filtered if _norm(c.get("instrumenttype")).startswith("FUT")]
        if expiry:
            filtered = [c for c in filtered if _norm_expiry(c.get("expiry")) == expiry]
    elif is_opt:
        filtered = [c for c in filtered if _norm(c.get("instrumenttype")).startswith("OPT")]
        if expiry:
            filtered = [c for c in filtered if _norm_expiry(c.get("expiry")) == expiry]
        if option_type:
            filtered = [c for c in filtered if _norm(c.get("symbol")).endswith(option_type)]
        if has_strike:
            target_strike = round(float(strike) * 100, 2)  # master stores strike in paise
            filtered = [c for c in filtered if abs(float(c.get("strike", -1)) - target_strike) < 1]
    else:
        # Unrecognized InstrumentType value and not enough signal (no option_type/strike)
        # to guess FUT vs OPT for a generic F&O label — refuse rather than silently
        # grabbing an arbitrary, possibly wrong, contract.
        print(f"WARNING: {symbol} ({exchange}) — unrecognized InstrumentType {row.get('InstrumentType')!r} "
              f"and not enough info to disambiguate FUT vs OPT. Row NOT resolved — fix the sheet.")
        return None

    if not filtered:
        return None
    if len(filtered) > 1:
        # Still ambiguous after filtering (e.g. duplicate master rows, or expiry
        # didn't narrow it down) — this is exactly the failure mode that silently
        # picked a wrong-but-similar-priced contract before. Surface it loudly
        # instead of guessing.
        print(f"WARNING: {symbol} ({exchange}, {row.get('InstrumentType')!r}, expiry={expiry!r}) — "
              f"{len(filtered)} candidate tokens still match after filtering: "
              f"{[(c.get('token'), c.get('symbol'), c.get('expiry'), c.get('strike')) for c in filtered]}. "
              f"Using the first one — verify this is the right contract.")

    chosen = filtered[0]
    ws_type = EXCH_SEG_TO_WS_TYPE.get(lookup_seg)
    if ws_type is None:
        return None
    out = {
        "symbol": row.get("Symbol"),
        "exchange": exchange,  # keep the original cash exchange for display/matching elsewhere
        "exchangeType": ws_type,  # but the WS subscription type reflects the actual segment (NFO/BFO etc.)
        "token": str(chosen.get("token")),
    }
    if "Qty" in row:
        out["qty"] = row["Qty"]
    if "AvgPrice" in row:
        out["avgPrice"] = row["AvgPrice"]
    return out


def build_name_index(master):
    idx = {}
    for m in master:
        key = _norm(m.get("name"))
        idx.setdefault(key, []).append(m)
        # Also index by the raw symbol (helps for EQ rows where name==base symbol anyway)
        key2 = _norm(m.get("symbol"))
        idx.setdefault(key2, []).append(m)
    return idx


def resolve_all(excel_rows, cache_path, max_age_hours):
    master = load_master(cache_path, max_age_hours)
    name_idx = build_name_index(master)
    resolved, unresolved = [], []
    for row in excel_rows:
        r = resolve_row(name_idx, row)
        if r:
            resolved.append(r)
            print(f"RESOLVED: {row.get('Symbol')} {row.get('InstrumentType')!r} "
                  f"expiry={row.get('Expiry')!r} strike={row.get('Strike')!r} "
                  f"-> token={r['token']} exchangeType={r['exchangeType']}")
        else:
            unresolved.append(row)
    return resolved, unresolved
