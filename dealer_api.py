"""
Client for SynapseWave's "Cloud Rest Pro" dealer API (fronts an ODIN OMS).
Docs: https://www.synapsewave.com/api/cloudrestpro/

Replaces the manual "trade ledger in a Google Sheet" step: instead of a
human copying each buy/sell into a spreadsheet, this pulls the dealer's
executed Trade Book straight from the broker and reshapes it into exactly
the row format positions_builder.load_trade_ledger_from_records() expects,
so build_positions()'s FIFO matching runs unchanged.

Auth flow (per the docs):
    1. POST /authentication/v1/dealer/session  with user_id/password/api_key
       -> dealer_token (send as `Authorization: Bearer <token>` from then on).
    2. Every other call also needs the `x-api-key` header (same api_key).
    3. Tokens aren't documented with a short TTL, but we still handle a 401
       by transparently re-logging-in once and retrying — cheap insurance
       against the token being revoked/rotated broker-side.

Secrets required (put these in Streamlit Cloud's "Secrets" panel):
    DEALER_HOST_URL   e.g. "https://<your-broker-host>"   (no trailing slash)
    DEALER_API_KEY
    DEALER_USER_ID
    DEALER_PASSWORD

Per-client config (in the existing [clients.XXX] tables in secrets.toml):
    dealer_client_id = "VISHAL1"   # the client_id this dealer sees them as

If a client's config has no dealer_client_id, app.py falls back to the old
Apps Script / Google Sheet path — the two sources can coexist per-client.
"""
import threading
import time

import pandas as pd
import requests

DEFAULT_TIMEOUT = 30
TRADE_BOOK_PAGE_LIMIT = 200  # max rows requested per page when paginating


class DealerAPIError(RuntimeError):
    """Raised on a non-'success' response from the dealer API."""


class DealerAPIClient:
    """One dealer login, shared across all of the dealer's mapped clients.
    Thread-safe: the Streamlit fragment / background threads may all hit
    this concurrently, so login/refresh is guarded by a lock.
    """

    def __init__(self, host_url: str, api_key: str, user_id: str, password: str,
                 version: str = "2.0.0", build_version: str = "1.0.0"):
        self.host_url = host_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.password = password
        self.version = version
        self.build_version = build_version

        self._token = None
        self._lock = threading.Lock()

    # ── auth ────────────────────────────────────────────────────────────
    def login(self):
        """POST /authentication/v1/dealer/session — obtain a fresh dealer_token."""
        url = f"{self.host_url}/authentication/v1/dealer/session"
        payload = {
            "user_id": self.user_id,
            "password": self.password,
            "api_key": self.api_key,
            "source": "DEALERAPI",
            "version": self.version,
            "build_version": self.build_version,
        }
        headers = {"Content-Type": "application/json", "x-api-key": self.api_key}
        resp = requests.post(url, json=payload, headers=headers, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise DealerAPIError(f"Dealer login failed: {data.get('message') or data}")
        with self._lock:
            self._token = data["data"]["dealer_token"]
        return self._token

    def _auth_headers(self):
        if self._token is None:
            self.login()
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "Authorization": f"Bearer {self._token}",
        }

    # ── low-level request with one-shot re-login on 401 ────────────────
    def _request(self, method, path, params=None, json_body=None, _retried=False):
        url = f"{self.host_url}{path}"
        resp = requests.request(
            method, url, params=params, json=json_body,
            headers=self._auth_headers(), timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code == 401 and not _retried:
            self.login()  # token expired/revoked — get a new one and retry once
            return self._request(method, path, params=params, json_body=json_body, _retried=True)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") not in ("success", "Success", True):
            raise DealerAPIError(f"Dealer API error on {path}: {data.get('message') or data}")
        return data

    # ── endpoints actually needed here ──────────────────────────────────
    def get_client_mapping(self):
        """Full list of clients mapped to this dealer login."""
        data = self._request("GET", "/nontransactional/v1/dealer/client-mapping")
        return data.get("data", [])

    def get_trade_book(self, client_id: str | None = None, order_ids: str | None = None):
        """Fetch the ENTIRE trade book for a client, paginating until exhausted.
        `client_id` empty/None returns the dealer's own trades instead of a
        client's — always pass a client_id for this dashboard's use case.
        """
        all_trades = []
        offset = 1
        while True:
            params = {"limit": TRADE_BOOK_PAGE_LIMIT, "offset": offset}
            if client_id:
                params["client_id"] = client_id
            if order_ids:
                params["order_ids"] = order_ids
            data = self._request("GET", "/transactional/v1/dealer/trades", params=params)
            page = data.get("data") or []
            all_trades.extend(page)
            total = (data.get("metadata") or {}).get("total_records")
            if len(page) < TRADE_BOOK_PAGE_LIMIT:
                break  # short page — that was the last one
            if total is not None and len(all_trades) >= total:
                break
            offset += 1
        return all_trades

    def get_positions(self, position_type: str = "all", client_id: str | None = None):
        """Live open positions straight from the exchange side (optional —
        useful as a cross-check against positions_builder's own FIFO output,
        or eventually as a replacement for it)."""
        params = {"client_id": client_id} if client_id else None
        data = self._request("GET", f"/transactional/v1/dealer/portfolio/positions/{position_type}", params=params)
        return data.get("data", [])


# ── mapping: dealer Trade Book rows -> positions_builder ledger rows ──────
def _clean_strike(strike_price):
    try:
        val = float(strike_price)
    except (TypeError, ValueError):
        return ""
    return "" if val <= 0 else val


def _clean_option_type(option_type):
    ot = (option_type or "").strip().upper()
    return ot if ot in ("CE", "PE") else ""


def _instrument_type_for(trade):
    """Trade Book rows carry an 'instrument' field: '' for cash equity,
    'FUTSTK'/'FUTIDX'/... for futures, 'OPTSTK'/'OPTIDX'/... for options."""
    instrument = (trade.get("instrument") or "").strip().upper()
    if instrument.startswith("FUT"):
        return "FUT"
    if instrument.startswith("OPT"):
        return "OPT"
    return "EQ"


def _cash_exchange(exchange_code):
    """'NSE_EQ' / 'NSE_FO' / 'BSE_FO' / 'MCX_FO' -> 'NSE' / 'NSE' / 'BSE' / 'MCX'.
    token_resolver expects the plain cash-exchange name (it does its own
    NSE->NFO / BSE->BFO remap for F&O), so strip the _EQ/_FO/_CUR/_COMM suffix.
    """
    return (exchange_code or "").split("_")[0].strip().upper()


def trade_to_ledger_row(trade: dict) -> dict:
    """One Trade Book record -> one positions_builder ledger row (a single
    BUY or SELL leg — positions_builder's own FIFO logic does the matching,
    so no aggregation needed here)."""
    is_buy = (trade.get("transaction_type") or "").strip().upper() == "BUY"
    price = float(trade.get("trade_price") or 0)
    qty = float(trade.get("trade_quantity") or 0)
    ts = trade.get("trade_timestamp") or ""

    row = {
        "Symbol": trade.get("symbol"),
        "Exchange": _cash_exchange(trade.get("exchange")),
        "Instrument Type": _instrument_type_for(trade),
        "Expiry": trade.get("expiry_date") or "",
        "Option Type": _clean_option_type(trade.get("option_type")),
        "Strike": _clean_strike(trade.get("strike_price")),
        "Quantity": qty,
        "Buy Price": price if is_buy else "",
        "Buy Date": ts if is_buy else "",
        "Sell Price": "" if is_buy else price,
        "Sell Date": "" if is_buy else ts,
    }
    return row


def trades_to_ledger_rows(trades: list[dict]) -> list[dict]:
    return [trade_to_ledger_row(t) for t in trades if float(t.get("trade_quantity") or 0) > 0]
