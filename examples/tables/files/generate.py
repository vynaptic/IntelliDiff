"""The files acceptance kit's two exports, written under DIR: side a's CSV as a legacy system writes one
(semicolons, a decimal comma, DD/MM/YYYY, zero-padded keys, a quoted field, a stamp file written last) and
side b's Parquet, with the differences README.md lists.

    python generate.py DIR
"""

from __future__ import annotations

import csv
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb

ROWS = 1000


def account(n: int) -> tuple[int, str, Decimal, date, str]:
    holder = f"Holder {n}; Ltd" if n % 100 == 0 else f"Holder {n}"  # a delimiter inside a quoted field
    return n, holder, Decimal(n) * Decimal("1.25"), date(2020, 1, 1) + timedelta(days=n), "A"


def write(root: Path) -> None:
    a, b = root / "accounts_a", root / "accounts_b"
    a.mkdir(parents=True, exist_ok=True)
    b.mkdir(parents=True, exist_ok=True)
    rows = [account(n) for n in range(1, ROWS + 1)]
    with (a / "accounts.csv").open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out, delimiter=";", lineterminator="\n")
        writer.writerow(["account_id", "holder", "balance", "opened", "status"])
        for n, holder, balance, opened, status in rows:
            writer.writerow(
                [f"{n:06d}", holder, f"{balance:.2f}".replace(".", ","), f"{opened:%d/%m/%Y}", status]
            )
    # The stamp file, written last: the time the export declares of itself.
    (a / "_AS_OF").write_text(datetime.now(UTC).isoformat(timespec="seconds") + "\n")
    changed = {
        5: lambda r: (r[0], r[1].upper(), *r[2:]),
        10: lambda r: (*r[:2], r[2] + Decimal("0.01"), *r[3:]),
        15: lambda r: (*r[:3], r[3] + timedelta(days=1), r[4]),
    }
    held = [changed.get(r[0], lambda r: r)(r) for r in rows if r[0] != 20]  # 20 is missing
    held += [account(30), (ROWS + 1, "New", Decimal("1.00"), date(2024, 1, 1), "A")]  # 30 twice, 1001 extra
    held += [(None, "no key", Decimal("0.00"), date(2024, 1, 1), "A")]
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE accounts (account_id BIGINT, holder VARCHAR, balance DECIMAL(12, 2), opened DATE,"
        " status VARCHAR)"
    )
    con.executemany("INSERT INTO accounts VALUES (?, ?, ?, ?, ?)", held)
    con.execute(f"COPY accounts TO '{b / 'accounts.parquet'}' (FORMAT parquet)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    write(Path(sys.argv[1]))
