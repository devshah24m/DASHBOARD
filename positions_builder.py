"""
Reads a trade-ledger Excel and collapses it into one row per open position,
using FIFO matching between buys and sells and a weighted-average price for
whatever quantity is still open.

Expected columns (case-insensitive, extra columns ignored):

    Symbol | Exchange | Instrument Type | Expiry | CE/PE | Strike | Quantity
    | Buy Price | Buy Date | Sell Price | Sell Date

Each row is ONE transaction:
    - A BUY row has Buy Price + Buy Date filled in; Sell Price/Sell Date blank.
    - A SELL row has Sell Price + Sell Date filled in; Buy Price/Buy Date blank.
The same symbol (and same contract — same expiry/strike/CE-PE for F&O) can
appear across many rows as you build/trim the position over time.

FIFO logic:
    1. All BUY rows for a contract are queued oldest-first by Buy Date.
    2. Each SELL row (oldest-first by Sell Date) consumes quantity from the
       front of that queue — the oldest unsold buy lot(s) first.
    3. Whatever quantity remains unconsumed across all buy lots is the open
       position. Its average price is the QUANTITY-WEIGHTED average of the
       buy prices of only the remaining (unsold) portion of each lot —
       fully-sold lots contribute nothing to the average.

Output: one dict per contract with Symbol/Exchange/InstrumentType/Expiry/
OptionType/Strike/Qty/AvgPrice — Qty==0 rows (fully closed positions) are
dropped since there's nothing open to track live.
"""
import pandas as pd

COLUMN_MAP = {
    "symbol": "Symbol",
    "exchange": "Exchange",
    "instrument": "InstrumentType",
    "instrumer": "InstrumentType",   # matches the typo in the example sheet
    "instrument type": "InstrumentType",
    "instrumenttype": "InstrumentType",
    "expiry": "Expiry",
    "ce/pe": "OptionType",
    "ce_pe": "OptionType",
    "option type": "OptionType",
    "strike": "Strike",
    "quantity": "Quantity",
    "qty": "Quantity",
    "buy price": "BuyPrice",
    "buyprice": "BuyPrice",
    "buy date": "BuyDate",
    "buydate": "BuyDate",
    "sell price": "SellPrice",
    "sellprice": "SellPrice",
    "sell date": "SellDate",
    "selldate": "SellDate",
}

REQUIRED = ["Symbol", "Exchange"]
OPTIONAL_BLANK = ["InstrumentType", "Expiry", "OptionType", "Strike"]


def _norm(v):
    if pd.isna(v):
        return ""
    return str(v).strip().upper()


def _normalize_ledger_df(df):
    df.columns = [c.strip().lower() for c in df.columns]
    df = df.rename(columns={k: v for k, v in COLUMN_MAP.items() if k in df.columns})

    for req in REQUIRED:
        if req not in df.columns:
            raise ValueError(f"Trade ledger is missing required column: {req}")
    for opt in OPTIONAL_BLANK:
        if opt not in df.columns:
            df[opt] = ""
    if "InstrumentType" in df.columns:
        df["InstrumentType"] = df["InstrumentType"].replace("", "EQ").fillna("EQ")
    if "Quantity" not in df.columns:
        raise ValueError("Trade ledger is missing required column: Quantity")

    return df.to_dict(orient="records")


def load_trade_ledger(path):
    df = pd.read_excel(path)
    return _normalize_ledger_df(df)


def load_trade_ledger_from_records(records):
    """Same shape as load_trade_ledger but for already-parsed JSON — e.g.
    the array of row-objects returned by the Apps Script Web App
    (doGet in Code.gs). `records` is a list of dicts keyed by the
    sheet's header row, exactly like requests.get(url).json() gives you.
    """
    df = pd.DataFrame.from_records(records)
    return _normalize_ledger_df(df)


def _contract_key(row):
    return (
        _norm(row.get("Symbol")),
        _norm(row.get("Exchange")),
        _norm(row.get("InstrumentType")) or "EQ",
        _norm(row.get("Expiry")),
        _norm(row.get("OptionType")),
        _norm(row.get("Strike")),
    )


def _segment_for(instrument_type):
    it = _norm(instrument_type)
    if it in ("", "EQ"):
        return "Equity"
    if it in ("F&O", "FO") or it.startswith("FUT") or it.startswith("OPT"):
        return "F&O"
    return "Other"


def _is_buy_row(row):
    return not pd.isna(row.get("BuyPrice")) and row.get("BuyPrice") not in ("", None)


def _is_sell_row(row):
    return not pd.isna(row.get("SellPrice")) and row.get("SellPrice") not in ("", None)


def _fmt_date(d):
    """Best-effort YYYY-MM-DD string for a date-ish value; None if unusable."""
    ts = pd.to_datetime(d, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d")


def _fifo_match(buys, sells):
    """
    buys/sells: lists of {"qty","price","date"}, already sorted oldest-first.
    Returns (remaining_lots, matched_trades, oversold_qty, oversold_lots):
      remaining_lots: leftover buy lots after all sells consumed what they could,
                       each {"qty","price","date"} — qty may be 0 for fully-consumed lots.
      matched_trades: one entry per FIFO match segment (a single sell can span
                       multiple buy lots, producing multiple entries) —
                       {"qty","buy_price","buy_date","sell_price","sell_date"}.
      oversold_qty: total sell quantity beyond total buys (i.e. sold short —
                    there was no buy lot left to match against).
      oversold_lots: the same excess broken out per sell row as
                      {"qty","price","date"}, price/date taken from the SELL
                      side, so the caller can compute a weighted-average
                      short entry price (this is how a contract becomes an
                      open SHORT position instead of the qty just being
                      dropped with a warning).
    """
    lots = [{"qty": b["qty"], "price": b["price"], "date": b["date"]} for b in buys]
    matched_trades = []
    oversold_lots = []
    lot_idx = 0
    oversold_qty = 0

    for sell in sells:
        remaining_sell_qty = sell["qty"]
        while remaining_sell_qty > 0 and lot_idx < len(lots):
            lot = lots[lot_idx]
            if lot["qty"] <= 0:
                lot_idx += 1
                continue
            consume = min(lot["qty"], remaining_sell_qty)
            matched_trades.append({
                "qty": consume,
                "buy_price": lot["price"],
                "buy_date": lot["date"],
                "sell_price": sell["price"],
                "sell_date": sell["date"],
            })
            lot["qty"] -= consume
            remaining_sell_qty -= consume
            if lot["qty"] <= 0:
                lot_idx += 1
        if remaining_sell_qty > 0:
            oversold_qty += remaining_sell_qty
            oversold_lots.append({"qty": remaining_sell_qty, "price": sell["price"], "date": sell["date"]})

    return lots, matched_trades, oversold_qty, oversold_lots


def build_positions(rows, verbose=True):
    """
    Single FIFO pass producing BOTH views at once:
      open:   list of {Symbol, Exchange, InstrumentType, Expiry, OptionType,
              Strike, Segment, Qty, AvgPrice} — still-held quantity, weighted
              by the buy price of only the unsold portion of each lot.
      closed: list of {Symbol, Exchange, InstrumentType, Expiry, OptionType,
              Strike, Segment, Qty, AvgBuyPrice, AvgSellPrice, BookedPnL} —
              realized trades so far, aggregated per contract.
    A contract can appear in BOTH lists at once (partial exit).
    """
    by_contract = {}
    for row in rows:
        key = _contract_key(row)
        by_contract.setdefault(key, {"buys": [], "sells": []})
        qty = float(row.get("Quantity") or 0)
        if qty <= 0:
            continue
        if _is_buy_row(row):
            by_contract[key]["buys"].append({"qty": qty, "price": float(row["BuyPrice"]), "date": row.get("BuyDate")})
        elif _is_sell_row(row):
            by_contract[key]["sells"].append({"qty": qty, "price": float(row["SellPrice"]), "date": row.get("SellDate")})
        elif verbose:
            print(f"WARNING: row for {row.get('Symbol')} has neither Buy Price nor Sell Price filled — skipped: {row}")

    open_positions, closed_positions = [], []
    for key, legs in by_contract.items():
        symbol, exchange, instrument_type, expiry, option_type, strike = key
        segment = _segment_for(instrument_type)
        buys = sorted(legs["buys"], key=lambda b: (pd.to_datetime(b["date"], errors="coerce") or pd.Timestamp.min))
        sells = sorted(legs["sells"], key=lambda s: (pd.to_datetime(s["date"], errors="coerce") or pd.Timestamp.min))

        lots, matched_trades, oversold_qty, oversold_lots = _fifo_match(buys, sells)
        if oversold_qty > 0 and verbose:
            print(f"NOTE: {symbol} ({exchange}) — sells exceed total buys by {oversold_qty} units. "
                  f"Treating this as an open SHORT position (weighted-avg sell price as entry); "
                  f"if that's wrong, check for a missing buy row in the ledger.")

        open_qty = sum(l["qty"] for l in lots if l["qty"] > 0)
        if open_qty > 0:
            weighted_cost = sum(l["qty"] * l["price"] for l in lots if l["qty"] > 0)
            open_positions.append({
                "Symbol": symbol, "Exchange": exchange, "InstrumentType": instrument_type,
                "Expiry": expiry, "OptionType": option_type, "Strike": strike, "Segment": segment,
                "Qty": round(open_qty, 4), "AvgPrice": round(weighted_cost / open_qty, 4),
                "PositionType": "LONG",
            })

        # Sold more than was ever bought in this ledger -> net open SHORT position.
        # Qty is stored NEGATIVE on purpose: downstream MTM is (ltp - avgPrice) * qty,
        # so a negative qty automatically flips the sign to (avgPrice - ltp) * |qty| —
        # i.e. profit when price falls, loss when price rises — without needing a
        # separate short/long branch anywhere else in the pipeline.
        if oversold_qty > 0:
            weighted_short_cost = sum(o["qty"] * o["price"] for o in oversold_lots)
            open_positions.append({
                "Symbol": symbol, "Exchange": exchange, "InstrumentType": instrument_type,
                "Expiry": expiry, "OptionType": option_type, "Strike": strike, "Segment": segment,
                "Qty": round(-oversold_qty, 4), "AvgPrice": round(weighted_short_cost / oversold_qty, 4),
                "PositionType": "SHORT",
            })

        if matched_trades:
            # matched_trades already holds every FIFO-matched leg for THIS contract
            # (same symbol+exchange+instrument+expiry+option+strike) across however many
            # buy/sell rows the ledger had — i.e. re-entries into the same scrip are already
            # netted together here, oldest buy lot first. Nothing further to merge upstream.
            realized_qty = sum(m["qty"] for m in matched_trades)
            avg_buy = sum(m["qty"] * m["buy_price"] for m in matched_trades) / realized_qty
            avg_sell = sum(m["qty"] * m["sell_price"] for m in matched_trades) / realized_qty
            booked_pnl = sum(m["qty"] * (m["sell_price"] - m["buy_price"]) for m in matched_trades)
            trades_out = [{
                "Qty": round(m["qty"], 4),
                "BuyPrice": round(m["buy_price"], 4),
                "BuyDate": _fmt_date(m["buy_date"]),
                "SellPrice": round(m["sell_price"], 4),
                "SellDate": _fmt_date(m["sell_date"]),
                "Pnl": round(m["qty"] * (m["sell_price"] - m["buy_price"]), 2),
            } for m in matched_trades]
            closed_positions.append({
                "Symbol": symbol, "Exchange": exchange, "InstrumentType": instrument_type,
                "Expiry": expiry, "OptionType": option_type, "Strike": strike, "Segment": segment,
                "Qty": round(realized_qty, 4), "AvgBuyPrice": round(avg_buy, 4),
                "AvgSellPrice": round(avg_sell, 4), "BookedPnL": round(booked_pnl, 2),
                "Trades": trades_out,  # per-FIFO-leg breakdown, newest/oldest order as matched
            })

    return open_positions, closed_positions


def build_open_positions(rows, verbose=True):
    """Back-compat wrapper — open positions only (see build_positions for both views)."""
    open_positions, _ = build_positions(rows, verbose=verbose)
    return open_positions
