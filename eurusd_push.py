"""EURUSD live feed: compact GitHub API updates + rolling and archived history."""
import base64
import csv
import io
import json
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import MetaTrader5 as mt5

OWNER, REPO, BRANCH = "aladinenergy-alt", "eurusd-feed", "main"
REPO_DIR = r"C:\Users\amarijan\Documents\eurusd-feed"
SYMBOL = "EURUSD"
INTERVAL = 30
ARCHIVE_MINUTES = 5
D = 5
API = f"https://api.github.com/repos/{OWNER}/{REPO}"
TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}
HISTORY_COLUMNS = [
    "time", "bid", "ask", "spread_pips", "balance", "equity", "margin",
    "free_margin", "margin_level", "net_lots", "floating_pl",
    "account_positions", "account_floating_pl", "pending_orders",
    "USDJPY", "GBPUSD", "DXY",
]
for tf in ("M1", "M5", "M15", "H1", "H4", "D1"):
    HISTORY_COLUMNS += [f"{tf}_{x}" for x in ("time", "open", "high", "low", "close")]
for tf in ("M15", "H1", "H4", "D1"):
    HISTORY_COLUMNS += [
        f"{tf}_{x}" for x in (
            "ema20", "ema50", "ema100", "rsi14", "atr14_pips",
            "bb_low", "bb_mid", "bb_high",
        )
    ]


def get_github_token():
    result = subprocess.run(
        ["git", "-C", REPO_DIR, "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, timeout=20, check=False,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("password=") and line[9:].strip():
                return line[9:].strip()
    raise RuntimeError("GitHub prijava nije pronađena u Git Credential Manageru.")


def api(method, path, token, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "EURUSD-live-feed",
    })
    try:
        with urlopen(req, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"GitHub API {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"GitHub mrežna greška: {exc.reason}") from exc


def publish_files(files, token):
    ref = api("GET", f"/git/ref/heads/{BRANCH}", token)
    old = api("GET", f"/git/commits/{ref['object']['sha']}", token)
    entries = []
    for path, text in files.items():
        blob = api("POST", "/git/blobs", token, {
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "encoding": "base64",
        })
        entries.append({
            "path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]
        })
    tree = api("POST", "/git/trees", token, {
        "base_tree": old["tree"]["sha"], "tree": entries,
    })
    commit = api("POST", "/git/commits", token, {
        "message": "Refresh EURUSD feed", "tree": tree["sha"], "parents": [],
    })
    api("PATCH", f"/git/refs/heads/{BRANCH}", token, {
        "sha": commit["sha"], "force": True,
    })


def rates_for(tf, count=160):
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAMES[tf], 0, count)
    if rates is None or len(rates) < 105:
        raise RuntimeError(f"Nedovoljno MT5 podataka za {tf}: {mt5.last_error()}")
    return rates


def ema(values, period):
    alpha = 2.0 / (period + 1)
    value = float(values[0])
    for x in values[1:]:
        value = alpha * float(x) + (1 - alpha) * value
    return value


def indicators(rates):
    # Exclude the currently forming candle from indicators.
    bars = rates[:-1]
    closes = [float(x["close"]) for x in bars]
    highs = [float(x["high"]) for x in bars]
    lows = [float(x["low"]) for x in bars]
    prev_close = [closes[0]] + closes[:-1]
    trs = [max(h - l, abs(h - p), abs(l - p)) for h, l, p in zip(highs, lows, prev_close)]
    period = 14
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        gains.append(max(b - a, 0.0))
        losses.append(max(a - b, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    rsi = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    atr = sum(trs[-period:]) / period
    last20 = closes[-20:]
    mean = sum(last20) / 20
    variance = sum((x - mean) ** 2 for x in last20) / 20
    sd = variance ** 0.5
    return {
        "ema20": ema(closes, 20), "ema50": ema(closes, 50),
        "ema100": ema(closes, 100), "rsi14": rsi,
        "atr14_pips": atr * 10000,
        "bb_low": mean - 2 * sd, "bb_mid": mean, "bb_high": mean + 2 * sd,
    }


def find_symbol(candidates):
    available = mt5.symbols_get() or ()
    names = [s.name for s in available]
    for candidate in candidates:
        for name in names:
            if name.upper() == candidate.upper():
                mt5.symbol_select(name, True)
                return name
    for candidate in candidates:
        for name in names:
            if candidate.upper() in name.upper():
                mt5.symbol_select(name, True)
                return name
    return None


def external_price(symbol):
    if not symbol:
        return ""
    tick = mt5.symbol_info_tick(symbol)
    return "" if tick is None else (tick.bid + tick.ask) / 2


def candle_line(tf, bar):
    colour = "ZELENO" if bar["close"] >= bar["open"] else "CRVENO"
    t = datetime.fromtimestamp(int(bar["time"]), timezone.utc).strftime("%d.%m %H:%M")
    return (
        f"{t} O {bar['open']:.{D}f} H {bar['high']:.{D}f} "
        f"L {bar['low']:.{D}f} C {bar['close']:.{D}f} {colour}"
    )


def build_snapshot_and_row(external):
    tick = mt5.symbol_info_tick(SYMBOL)
    account = mt5.account_info()
    all_positions = mt5.positions_get()
    pending = mt5.orders_get()
    if tick is None or account is None or all_positions is None or pending is None:
        raise RuntimeError(f"Nedostaju MT5 tick/račun/pozicije/nalozi: {mt5.last_error()}")
    # Broker suffixes/punctuation (e.g. EURUSDm or EURUSDm#) still belong to EURUSD.
    positions = [
        p for p in all_positions
        if p.symbol.upper().replace("#", "").startswith(SYMBOL)
    ]
    account_floating = sum(p.profit + p.swap for p in all_positions)
    net_lots = sum(p.volume if p.type == mt5.POSITION_TYPE_BUY else -p.volume for p in positions)
    floating = sum(p.profit + p.swap for p in positions)

    data = {tf: rates_for(tf) for tf in TIMEFRAMES}
    ind = {tf: indicators(data[tf]) for tf in ("M15", "H1", "H4", "D1")}
    now = datetime.now().astimezone()
    stamp = now.isoformat(timespec="seconds")
    prices = {k: external_price(v) for k, v in external.items()}

    lines = [
        f"EURUSD | lokalno {now.strftime('%d.%m.%Y %H:%M:%S')} | tick {datetime.fromtimestamp(tick.time, timezone.utc).strftime('%H:%M:%S')} UTC",
        f"CIJENA: Bid {tick.bid:.5f} | Ask {tick.ask:.5f} | Spread {(tick.ask-tick.bid)*10000:.1f} pips",
        f"RACUN: Balance {account.balance:.2f} | Equity {account.equity:.2f} | Margin {account.margin:.2f} | FreeMargin {account.margin_free:.2f} | MarginLevel {account.margin_level:.1f}%",
        f"OTVORENE POZICIJE SVI SIMBOLI: {len(all_positions)} | Ukupni floating P/L {account_floating:+.2f}",
        f"EURUSD POZICIJE: {len(positions)} | Net lot {net_lots:+.2f} | Floating P/L {floating:+.2f}",
    ]
    for p in all_positions:
        side = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
        lines.append(
            f"  POZICIJA #{p.ticket} {p.symbol} {side} {p.volume:g} lot | "
            f"ulaz {p.price_open:.5f} | trenutno {p.price_current:.5f} | "
            f"SL {p.sl:.5f} | TP {p.tp:.5f} | profit+swap {p.profit + p.swap:+.2f}"
        )
    lines.append(f"ČEKAJUĆI NALOZI: {len(pending)}")
    for o in pending:
        lines.append(
            f"  NALOG #{o.ticket} {o.symbol} tip {o.type} {o.volume_initial:g} lot | "
            f"cijena {o.price_open:.5f} | SL {o.sl:.5f} | TP {o.tp:.5f}"
        )
    lines.append(
        "VANJSKI SIMBOLI: " +
        " | ".join(f"{k} {prices[k] if prices[k] != '' else 'NEDOSTUPAN'}" for k in prices)
    )

    d1, d1prev = data["D1"][-1], data["D1"][-2]
    w1 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_W1, 0, 2)
    if w1 is not None and len(w1) >= 2:
        lines.append(
            f"RASPON DANAS O/H/L {d1['open']:.5f}/{d1['high']:.5f}/{d1['low']:.5f} | "
            f"JUČER H/L {d1prev['high']:.5f}/{d1prev['low']:.5f} | "
            f"OVAJ TJEDAN H/L {w1[-1]['high']:.5f}/{w1[-1]['low']:.5f} | "
            f"PROŠLI TJEDAN H/L {w1[-2]['high']:.5f}/{w1[-2]['low']:.5f}"
        )
    for tf in ("M1", "M5", "M15", "H1", "H4", "D1"):
        lines.append(f"{tf} SVJEĆE (stare -> nove):")
        lines.extend("  " + candle_line(tf, b) for b in data[tf][-4:])
    for tf in ("M15", "H1", "H4", "D1"):
        v = ind[tf]
        lines.append(
            f"{tf} INDIKATORI: EMA20 {v['ema20']:.5f} | EMA50 {v['ema50']:.5f} | "
            f"EMA100 {v['ema100']:.5f} | RSI14 {v['rsi14']:.1f} | "
            f"ATR14 {v['atr14_pips']:.1f} pips | BB {v['bb_low']:.5f}/{v['bb_mid']:.5f}/{v['bb_high']:.5f}"
        )

    row = {
        "time": stamp, "bid": tick.bid, "ask": tick.ask,
        "spread_pips": (tick.ask - tick.bid) * 10000,
        "balance": account.balance, "equity": account.equity,
        "margin": account.margin, "free_margin": account.margin_free,
        "margin_level": account.margin_level, "net_lots": net_lots,
        "floating_pl": floating, "account_positions": len(all_positions),
        "account_floating_pl": account_floating, "pending_orders": len(pending), **prices,
    }
    for tf, bars in data.items():
        b = bars[-1]
        row.update({
            f"{tf}_time": datetime.fromtimestamp(int(b["time"]), timezone.utc).isoformat(),
            f"{tf}_open": b["open"], f"{tf}_high": b["high"],
            f"{tf}_low": b["low"], f"{tf}_close": b["close"],
        })
    for tf, values in ind.items():
        row.update({f"{tf}_{k}": v for k, v in values.items()})
    return "\n".join(lines), row


def csv_text(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=HISTORY_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def main():
    token = get_github_token()
    api("GET", f"/git/ref/heads/{BRANCH}", token)
    if not mt5.initialize():
        raise RuntimeError(f"MT5 nije dostupan: {mt5.last_error()}")
    try:
        if not mt5.symbol_select(SYMBOL, True):
            raise RuntimeError(f"Ne mogu odabrati {SYMBOL}: {mt5.last_error()}")
        external = {
            "USDJPY": find_symbol(["USDJPY"]),
            "GBPUSD": find_symbol(["GBPUSD"]),
            "DXY": find_symbol(["DXY", "USDX", "USDOLLAR"]),
        }
        history_latest = deque(maxlen=10)
        archive_rows = []
        archive_key = None
        print("EURUSD feed: snapshot + indikatori + 6 TF + povijest. Upload samo malih datoteka. Ctrl+C za stop.")
        while True:
            try:
                snapshot, row = build_snapshot_and_row(external)
                now = datetime.now().astimezone()
                bucket_minute = (now.minute // ARCHIVE_MINUTES) * ARCHIVE_MINUTES
                new_key = now.strftime("%Y%m%d_%H") + f"{bucket_minute:02d}"
                files = {
                    "snapshot.txt": snapshot,
                }
                history_latest.append(row)
                files["history_latest.csv"] = csv_text(list(history_latest))

                # Archive the completed block; keep its rows until GitHub confirms the upload.
                archive_transition = archive_key is not None and new_key != archive_key
                if archive_key is None:
                    archive_key = new_key
                if archive_transition:
                    archive_path = f"history/{archive_key}.csv"
                    files[archive_path] = csv_text(archive_rows)
                else:
                    archive_rows.append(row)

                Path(REPO_DIR, "snapshot.txt").write_text(snapshot, encoding="utf-8")
                publish_files(files, token)
                if archive_transition:
                    archive_rows = [row]
                    archive_key = new_key
                print(now.strftime("%H:%M:%S"), "OK | snapshot + indikatori + povijest")
            except Exception as exc:
                print(datetime.now().strftime("%H:%M:%S"), "GREŠKA:", str(exc)[:350])
            time.sleep(INTERVAL)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Zaustavljeno.")
    except Exception as exc:
        raise SystemExit(f"Pokretanje nije uspjelo: {exc}")
