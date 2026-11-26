"""Download the SEC's quarterly "Insider Transactions Data Sets" (Forms 3, 4, 5).

usage: fetch_insider_data.py <outdir> [first_quarter=2023q1]

The SEC asks every automated client to identify itself with a contact address in
the User-Agent. This script takes that identity from the EDGAR_IDENTITY
environment variable, which the owner sets (for example
"Market Signals research name@example.com"); on Windows a value saved for the
user account is read too, so a restart of the terminal is not needed. The
identity is sent only to sec.gov and is never printed.
"""
import datetime as dt
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

URL = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{quarter}_form345.zip"


def edgar_identity() -> str:
    value = os.environ.get("EDGAR_IDENTITY", "").strip()
    if not value and os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, "EDGAR_IDENTITY")[0]).strip()
        except OSError:
            value = ""
    return value


def quarters(first: str):
    year, q = int(first[:4]), int(first[5])
    today = dt.date.today()
    while (year, q) <= (today.year, (today.month - 1) // 3 + 1):
        yield f"{year}q{q}"
        year, q = (year, q + 1) if q < 4 else (year + 1, 1)


def main() -> int:
    out = Path(sys.argv[1])
    first = sys.argv[2] if len(sys.argv) > 2 else "2023q1"
    out.mkdir(parents=True, exist_ok=True)
    identity = edgar_identity()
    if "@" not in identity:
        print("EDGAR_IDENTITY is not set. The SEC requires a contact address; set the variable and run again.")
        return 2
    for quarter in quarters(first):
        target = out / f"{quarter}_form345.zip"
        if target.exists() and zipfile.is_zipfile(target):
            print(f"{quarter}: already here ({target.stat().st_size / 1e6:.1f} MB)")
            continue
        request = urllib.request.Request(URL.format(quarter=quarter), headers={"User-Agent": identity})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            print(f"{quarter}: HTTP {exc.code} (not published yet, or refused)")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"{quarter}: {exc.__class__.__name__}")
            continue
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        if not zipfile.is_zipfile(tmp):
            tmp.unlink()
            print(f"{quarter}: the answer was not a zip file ({len(data)} bytes)")
            continue
        os.replace(tmp, target)
        print(f"{quarter}: saved {len(data) / 1e6:.1f} MB")
        time.sleep(0.5)  # far below the SEC's 10 requests per second
    return 0


if __name__ == "__main__":
    sys.exit(main())
