"""
back-trading/bt_fetcher.py
Incremental 1000-tick fetcher for the backtrader.

Unlike trader/fetcher.py (which fetches a full day), this fetcher:
  - Starts at a given timestamp (the command's entry ts)
  - Returns exactly 1000 ticks (or fewer if near session end)
  - Maintains a cursor so the engine can call next_batch() repeatedly
  - Fetches both TRADES and BID_ASK in lockstep (same cursor)

Used by bt_engine.py when tick data is not yet in DB or CSV files.
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_ROOT   = Path(__file__).parent.parent
_BT_DIR = Path(__file__).parent
for _p in [str(_ROOT), str(_BT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.logger import get_logger
from zoneinfo import ZoneInfo

CT  = ZoneInfo("America/Chicago")

log = get_logger("bt_fetcher")

_TICKS_PER_BATCH = 1000
_TICK_TIMEOUT    = 30  # seconds per IB request


class IncrementalFetcher:
    """
    Stateful incremental fetcher: call next_batch() in a loop.
    Each call returns (trades_batch, bidask_batch) — lists of raw tick tuples.
    Returns ([], []) when the session is exhausted.
    """

    def __init__(self, ib, contract, session_end_utc: datetime):
        self._ib          = ib
        self._contract    = contract
        self._session_end = session_end_utc
        self._cursor      = None   # set on first next_batch call
        self._done        = False
        self._total       = 0

    def start_from(self, ts: datetime):
        """Set the starting cursor. Must be called before next_batch()."""
        self._cursor = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    def is_done(self) -> bool:
        return self._done

    def next_batch(self) -> tuple:
        """
        Fetch the next batch of up to 1000 ticks.
        Returns (trades: list[tuple], bidask: list[tuple]).
        Each trade tuple: (ts_utc, price, size)
        Each bidask tuple: (ts_utc, bid_p, bid_s, ask_p, ask_s)
        """
        if self._done or self._cursor is None:
            return [], []

        loop = asyncio.get_event_loop()
        trades = loop.run_until_complete(
            self._fetch_what("TRADES", self._cursor))
        bidask = loop.run_until_complete(
            self._fetch_what("BID_ASK", self._cursor))

        if not trades:
            self._done = True
            return [], []

        # Advance cursor to last tick time
        last_ts = max(t[0] for t in trades)
        if last_ts >= self._session_end:
            self._done = True
        elif len(trades) < _TICKS_PER_BATCH:
            self._done = True
        else:
            # Add 1ms to avoid re-fetching the last tick
            self._cursor = last_ts + timedelta(milliseconds=1)

        self._total += len(trades)
        log.debug("bt_fetcher: batch %d trades, %d bidask (total %d)",
                  len(trades), len(bidask), self._total)
        return trades, bidask

    async def _fetch_what(self, what: str, start: datetime) -> list:
        for attempt in range(3):
            try:
                batch = await asyncio.wait_for(
                    self._ib.reqHistoricalTicksAsync(
                        self._contract,
                        startDateTime=start,
                        endDateTime="",
                        numberOfTicks=_TICKS_PER_BATCH,
                        whatToShow=what,
                        useRth=False,
                    ),
                    timeout=_TICK_TIMEOUT,
                )
            except asyncio.TimeoutError:
                log.warning("bt_fetcher timeout on %s attempt %d", what, attempt + 1)
                await asyncio.sleep(2)
                continue
            except Exception as e:
                log.warning("bt_fetcher error %s attempt %d: %s", what, attempt + 1, e)
                await asyncio.sleep(5)
                continue

            result = []
            for tick in batch:
                t_u = tick.time
                if not isinstance(t_u, datetime):
                    t_u = datetime.fromtimestamp(float(t_u), tz=timezone.utc)
                if t_u.tzinfo is None:
                    t_u = t_u.replace(tzinfo=timezone.utc)
                if t_u >= self._session_end:
                    break
                if what == "TRADES":
                    result.append((t_u, tick.price, tick.size))
                else:
                    bp = getattr(tick, "priceBid", 0.0)
                    bs = getattr(tick, "sizeBid",  0)
                    ap = getattr(tick, "priceAsk", 0.0)
                    as_ = getattr(tick, "sizeAsk", 0)
                    result.append((t_u, bp, bs, ap, as_))
            return result

        return []  # all attempts failed
