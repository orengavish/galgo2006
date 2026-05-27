"""
back-trading/bt_command.py
BacktradeCommand dataclass — the single input unit for the backtrader.

A command is one bracket order to simulate:
  - direction BUY or SELL
  - entry_type MKT (fill at next tick) or LMT (fill when price touched)
  - price: entry limit price (ignored for MKT)
  - tp_ticks / sl_ticks: symmetric or asymmetric bracket sizes
  - ts: the timestamp from which to start fetching ticks
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timezone


@dataclass
class BacktradeCommand:
    symbol:     str
    ts:         datetime     # UTC — start fetching from here
    direction:  str          # "BUY" or "SELL"
    entry_type: str          # "MKT" or "LMT"
    price:      float        # entry limit price (0.0 for MKT)
    tp_ticks:   int          # take-profit ticks
    sl_ticks:   int          # stop-loss ticks
    quantity:   int = 1
    command_id: int = 0      # set after DB insert

    def validate(self):
        assert self.direction  in ("BUY", "SELL"),   f"bad direction: {self.direction}"
        assert self.entry_type in ("MKT", "LMT"),    f"bad entry_type: {self.entry_type}"
        assert self.tp_ticks   > 0,                   "tp_ticks must be > 0"
        assert self.sl_ticks   > 0,                   "sl_ticks must be > 0"
        assert self.quantity   > 0,                   "quantity must be > 0"
        assert self.ts.tzinfo is not None,            "ts must be timezone-aware"

    def to_dict(self):
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        return d

    @classmethod
    def from_db_row(cls, row) -> "BacktradeCommand":
        return cls(
            symbol     = row["symbol"],
            ts         = datetime.fromisoformat(row["ts"]).replace(tzinfo=timezone.utc),
            direction  = row["direction"],
            entry_type = row["entry_type"],
            price      = row["price"],
            tp_ticks   = row["tp_ticks"],
            sl_ticks   = row["sl_ticks"],
            quantity   = row["quantity"],
            command_id = row["id"],
        )
