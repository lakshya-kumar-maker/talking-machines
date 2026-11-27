"""Talking Machines — time until failure.

When a signal is drifting towards the limit in its machine's PLC program (read by translator.py, e.g. the
filler trips if vibration stays above 7.2 mm/s for 10 s), fit a trend to the recent values and extend it
to that limit. The answer is a time and a range: "trips at about 09:37 (09:35-09:39)".

  python prediction.py           forecast for data/stream.csv as it is now
  python prediction.py --test    replay many runs and measure how far off the forecasts were
"""

import sys

import numpy as np
import pandas as pd

SMOOTH = 60  # seconds of smoothing before fitting
MIN_PROGRESS = 0.12  # forecast only once the signal has moved this share of the way from normal to the limit
MIN_POINTS = 90  # at least this many seconds of drift to fit on
MAX_WINDOW = 10 * 60  # fit on at most the last 10 minutes
HORIZON = 120  # don't forecast further than 2 hours ahead
BASE = (3, 8)  # healthy minutes = "normal"


def _crossing(x, y, limit, deg, now):
    """Minutes from now until the fitted curve reaches the limit (None if it doesn't within the horizon)."""
    if len(x) < deg + 2:
        return None
    c = np.polyfit(x, y, deg)
    p = np.poly1d(c)
    if (p(now) - limit) * (p(now + HORIZON) - limit) > 0:  # never reaches the limit within the horizon
        return None
    roots = [r.real for r in (p - limit).roots if abs(r.imag) < 1e-9 and r.real > now]
    return (min(roots) - now) if roots else None


def forecast_signal(df, tag, op, limit, running=None, full_speed=None):
    """-> dict(minutes, low, high, now_value, normal, progress) or None. df: the stream up to 'now'."""
    if len(df) < BASE[1] * 60 + SMOOTH:
        return None
    y = df[tag].astype(float)
    keep = pd.Series(True, index=df.index) if full_speed is None else full_speed
    sm = y.where(keep).rolling(SMOOTH, min_periods=SMOOTH // 2).mean()
    normal = float(sm.iloc[BASE[0] * 60 : BASE[1] * 60].mean())
    now_value = sm.iloc[-1]
    if np.isnan(now_value) or np.isnan(normal) or normal == limit:
        return None
    if running is not None and not bool(running.iloc[-1]):
        return None  # a stopped machine isn't wearing any further
    progress = (now_value - normal) / (limit - normal)
    if (
        progress < MIN_PROGRESS
        or (op in (">", ">=") and now_value >= limit)
        or (op in ("<", "<=") and now_value <= limit)
    ):
        return None
    # the drift: from where it first moved 5% of the way to the limit, at most the last 10 minutes
    prog = (sm - normal) / (limit - normal)
    moved = prog.index[(prog > 0.05).fillna(False)]
    start = max(moved[0] if len(moved) else df.index[0], df.index[-1] - MAX_WINDOW)
    win = sm.loc[start:].dropna()
    if len(win) < MIN_POINTS:
        return None
    # minutes, 0 = now; a 60 s average describes the middle of its minute, so shift it back by half
    x = (win.index.values - SMOOTH / 2 - df.index[-1]) / 60.0
    curved, straight = [], []
    for frac in (1.0, 0.66, 0.4):  # whole drift, and only its recent part
        part = slice(int(len(win) * (1 - frac)), None)
        g = _crossing(x[part], win.values[part], limit, 2, 0.0)  # a curve: wear speeds up as it goes
        if g is not None:
            curved.append(g)
        g = _crossing(x[part], win.values[part], limit, 1, 0.0)  # a straight line: the slow case
        if g is not None:
            straight.append(g)
    guesses = np.array(curved + straight)
    if not len(guesses):
        return None
    best = float(np.median(curved if curved else straight))  # the curve is the best guess, lines set the range
    deg = 2 if curved else 1
    fit = np.polyfit(x, win.values, deg)  # for drawing the forecast line
    return {
        "minutes": best,
        "low": float(guesses.min()),
        "high": float(guesses.max()),
        "now_value": float(now_value),
        "normal": normal,
        "progress": float(progress),
        "fit": [float(c) for c in fit],
        "fit_from": float(x[0]),
    }


def forecasts(df, dec, limits):
    """[{machine, tag, name, op, limit, unit, hold_s, file, at, low_at, high_at, minutes, ...}] soonest first.
    limits: translator.trip_limits(dec)."""
    sig = {}
    for t, r in dec.items():
        sig.setdefault(r["machine"], {})[r["signal"]] = t
    out = []
    now = df["timestamp"].iloc[-1]
    for m, lims in limits.items():
        s = sig.get(m, {})
        running = df[s["running"]] > 0.5 if "running" in s else None
        full = None
        if "speed" in s and running is not None:
            spd = df[s["speed"]].astype(float)
            full = running & (spd > 0.9 * spd[spd > 0.5 * spd.max()].median())
        for lim in lims:
            heater = dec[lim["tag"]]["signal"] == "glue_temp"  # a heater cools even while its machine runs slowly
            f = forecast_signal(
                df.set_index(df.index),
                lim["tag"],
                lim["op"],
                lim["value"],
                None if heater else running,
                None if heater else full,
            )
            if not f:
                continue
            hold = pd.Timedelta(seconds=lim["hold_s"])
            at = now + pd.Timedelta(minutes=f["minutes"]) + hold
            out.append(
                {
                    "machine": m,
                    "tag": lim["tag"],
                    "name": dec[lim["tag"]]["name"],
                    "op": lim["op"],
                    "limit": lim["value"],
                    "unit": dec[lim["tag"]]["unit"],
                    "hold_s": lim["hold_s"],
                    "file": lim["file"],
                    "at": at,
                    "minutes": f["minutes"] + lim["hold_s"] / 60,
                    "low_at": now + pd.Timedelta(minutes=f["low"]) + hold,
                    "high_at": now + pd.Timedelta(minutes=f["high"]) + hold,
                    "now_value": f["now_value"],
                    "normal": f["normal"],
                    "progress": f["progress"],
                    "fit": f["fit"],
                    "fit_from": f["fit_from"],
                    "now": now,
                }
            )
    return sorted(out, key=lambda o: o["at"])


def curve(p, step_s=15):
    """DataFrame(timestamp, value) of the fitted trend from the start of the drift to the predicted trip."""
    poly = np.poly1d(p["fit"])
    end = p["minutes"] - p["hold_s"] / 60  # where the trend reaches the limit
    xs = np.arange(p["fit_from"], end, step_s / 60)
    ys = poly(xs)
    above = p["op"] in (">", ">=")
    keep = (ys < p["limit"]) if above else (ys > p["limit"])  # stop the drawn line at the limit
    xs, ys = list(xs[keep]), list(ys[keep])
    xs.append(end)
    ys.append(p["limit"])
    return pd.DataFrame({"timestamp": [p["now"] + pd.Timedelta(minutes=float(x)) for x in xs], "value": ys})


def describe(p: dict, names: dict) -> str:
    """Return a forecast as one plain-English sentence."""
    m = names.get(p["machine"], p["machine"])
    word = "above" if p["op"] in (">", ">=") else "below"
    hold = f" for {p['hold_s']:g} s" if p["hold_s"] else ""
    return (
        f"{m} predicted to trip at about {p['at']:%H:%M} (in ~{p['minutes']:.0f} min, between "
        f"{p['low_at']:%H:%M} and {p['high_at']:%H:%M}): {p['name'].lower()} is {p['now_value']:.1f} {p['unit']} "
        f"(normal {p['normal']:.1f}) and heading for its PLC limit of {p['limit']:g} {p['unit']} "
        f"({word}{hold}, {p['file']})."
    )


def _test(seeds=range(1, 21), before=(10, 7, 5, 3)):
    """Replay runs and compare each forecast with the time the machine really tripped."""
    import json
    import subprocess

    import decoder
    import translator

    rows = []
    for seed in seeds:
        subprocess.run([sys.executable, "simulator.py", "--fast", "--seed", str(seed)], check=True, capture_output=True)
        full = pd.read_csv("data/stream.csv", parse_dates=["timestamp"])
        with open("data/answers.json") as f:
            fail = pd.Timestamp(json.load(f)["events"]["filler_fails"])
        dec, _ = decoder.decode(full)
        limits = translator.trip_limits(dec)
        for b in before:
            cut = full[full["timestamp"] <= fail - pd.Timedelta(minutes=b)]
            p = [x for x in forecasts(cut, dec, limits) if x["machine"] == "filler"]
            if not p:
                rows.append((seed, b, None, None))
                continue
            err = (p[0]["at"] - fail).total_seconds() / 60
            inside = p[0]["low_at"] <= fail + pd.Timedelta(seconds=30) and fail <= p[0]["high_at"] + pd.Timedelta(
                seconds=30
            )
            rows.append((seed, b, err, inside))
    r = pd.DataFrame(rows, columns=["seed", "min_before", "error_min", "inside_range"])
    print(
        r.groupby("min_before")
        .agg(
            forecasts=("error_min", "count"),
            runs=("seed", "count"),
            mean_abs_error_min=("error_min", lambda e: e.abs().mean()),
            inside_range=("inside_range", lambda v: f"{int(v.sum())}/{v.notna().sum()}"),
        )
        .sort_index(ascending=False)
    )
    return r


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--test" in sys.argv:
        _test()
    else:
        import decoder
        import translator

        df = pd.read_csv("data/stream.csv", parse_dates=["timestamp"])
        dec, _ = decoder.decode(df)
        found = forecasts(df, dec, translator.trip_limits(dec))
        print("\n".join(describe(p, decoder.MACHINE_NAMES) for p in found) or "No machine is heading for a trip limit.")
