"""Pushes an EURUSD snapshot from MetaTrader 5 to a GitHub repo every INTERVAL seconds.

Setup (once):
  1. Create a GitHub repo (e.g. eurusd-feed) with a README so it has one commit.
  2. git clone it to REPO_DIR below. Make sure `git push` works there without a prompt
     (GitHub login saved in Git Credential Manager is enough).
  3. py -3.12 -m pip install MetaTrader5

Run (leave the window open):  py -3.12 eurusd_push.py

It overwrites snapshot.txt and replaces the single latest commit (amend + force push),
so the repo history does not grow. Use a repo that is only for this feed.
"""
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5

SYMBOL = "EURUSD"                      # add broker suffix if needed, e.g. "EURUSD.m"
REPO_DIR = r"C:\Users\administracija\eurusd-feed"
INTERVAL = 30                          # seconds between pushes
D = 4


def fmt_time(epoch):
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%H:%M")


def candle(bar):
    colour = "zel" if bar["close"] >= bar["open"] else "crv"
    return (
        f"{fmt_time(bar['time'])} O {bar['open']:.{D}f} H {bar['high']:.{D}f} "
        f"L {bar['low']:.{D}f} C {bar['close']:.{D}f} {colour}"
    )


def snapshot():
    tick = mt5.symbol_info_tick(SYMBOL)
    m15 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, 6)
    m5 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, 4)
    d1 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_D1, 0, 2)
    if tick is None or m15 is None or m5 is None or d1 is None or len(d1) < 2:
        return None
    today, yesterday = d1[-1], d1[-2]
    now_local = datetime.now().strftime("%d.%m. %H:%M:%S")
    lines = [
        f"{SYMBOL} | moje vrijeme {now_local} | server tick {fmt_time(tick.time)}",
        f"Bid {tick.bid:.{D}f} Ask {tick.ask:.{D}f} Spread {(tick.ask - tick.bid) * 10000:.1f}p",
        f"Danas: O {today['open']:.{D}f} H {today['high']:.{D}f} L {today['low']:.{D}f} | "
        f"jucer C {yesterday['close']:.{D}f}",
        "M15 (stara -> nova, zadnja je u tijeku):",
    ]
    lines += ["  " + candle(b) for b in m15]
    lines.append("M5 zadnje 4:")
    lines += ["  " + candle(b) for b in m5]
    stale_min = (tick.time - int(m5[-1]["time"])) / 60
    if stale_min > 10:
        lines.append(f"UPOZORENJE: svijece kasne {stale_min:.0f} min. Otvori M5 i M15 grafikon u MT5.")
    return "\n".join(lines)


def git(*args):
    return subprocess.run(
        ["git", "-C", REPO_DIR, *args], capture_output=True, text=True, check=False
    )


if not mt5.initialize():
    raise SystemExit(f"MT5 not reachable: {mt5.last_error()}")
mt5.symbol_select(SYMBOL, True)

out_file = Path(REPO_DIR) / "snapshot.txt"
print(f"Pushing {SYMBOL} every {INTERVAL}s to {REPO_DIR}. Ctrl+C to stop.")

try:
    while True:
        text = snapshot()
        if text is None:
            print("No data from MT5, retrying...")
        else:
            out_file.write_text(text, encoding="utf-8")
            git("add", "snapshot.txt")
            git("commit", "--amend", "-m", "snapshot")
            result = git("push", "--force", "origin", "HEAD")
            status = "OK" if result.returncode == 0 else f"PUSH FAILED: {result.stderr.strip()[:200]}"
            print(datetime.now().strftime("%H:%M:%S"), status)
        time.sleep(INTERVAL)
except KeyboardInterrupt:
    pass
finally:
    mt5.shutdown()
