from lib.db import get_db
from pathlib import Path
with get_db(Path("trader/data/galao.db")) as con:
    rows = con.execute(
        "SELECT DATE(fill_time) as d, COUNT(*) as n,"
        " SUM(CASE WHEN exit_reason='TP' THEN 1 ELSE 0 END) as tp,"
        " SUM(CASE WHEN exit_reason='SL' THEN 1 ELSE 0 END) as sl,"
        " SUM(CASE WHEN exit_reason='STAGNATION' THEN 1 ELSE 0 END) as stag,"
        " ROUND(SUM(pnl_points),2) as pnl"
        " FROM verified_trades GROUP BY d ORDER BY d"
    ).fetchall()
    print(f"{'DATE':<12} {'TRADES':>6} {'TP':>5} {'SL':>5} {'STAG':>6} {'PNL':>8}")
    print("-"*48)
    for r in rows:
        print(f"{r['d']:<12} {r['n']:>6} {r['tp']:>5} {r['sl']:>5} {r['stag']:>6} {r['pnl']:>8.2f}")
    print("-"*48)
    total = con.execute(
        "SELECT COUNT(*), ROUND(SUM(pnl_points),2) FROM verified_trades"
    ).fetchone()
    print(f"{'TOTAL':<12} {total[0]:>6}                        {total[1]:>8.2f}")
