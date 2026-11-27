"""Talking Machines — how sure is an alert? A confidence % that is measured, not guessed.

Every alert card gets a few pieces of evidence:
  drift      the single-signal check sees a signal far from normal
  forest     the Isolation Forest sees the machine's signals together as unusual
  forecast   a signal is heading for the trip limit in the machine's PLC program
  signals    how many signals moved (vibration, motor current, motor temperature, ...)
  minutes    how long the alert has been up
  progress   how far the signal has moved from normal towards its PLC limit (0..1)
  others     how many OTHER machines are alerting at the same time (a warm hall makes every motor hotter)
A small logistic regression turns these into a probability that the alert is a real developing fault.

It is trained and tested on simulated shifts that also contain harmless look-alikes (a heavier product
batch, a forklift bump, a warm afternoon) and injected faults, with the answer key saying which alerts were
real. Trained on 30 runs, tested on 20 runs it has never seen; the test results ship with the model.

  python confidence.py --build     replay 50 runs and save every alert with its evidence (a few minutes)
  python confidence.py --train     fit the model, print the honest test table, save confidence_model.json
"""
import json
import math
import os
import sys

import pandas as pd

MODEL = "confidence_model.json"
FEATURES = ["drift", "forest", "forecast", "signals", "vibration", "current", "temperature", "heater",
            "minutes", "progress", "others"]
DATASET = "data/confidence_dataset.csv"


def features(card, cards):
    keys = [c["key"] for c in card["changed"]]
    others = sum(1 for c in cards if c is not card and c["status"] != "FAULT")
    now = card.get("_now")
    mins = (now - card["first_seen"]).total_seconds() / 60 if now is not None and card["first_seen"] is not None else 0
    return {"drift": int(bool(keys)), "forest": int(bool(card.get("forest"))),
            "forecast": int(bool(card.get("forecast"))), "signals": len(keys),
            "vibration": int("vibration" in keys), "current": int("motor_current" in keys),
            "temperature": int("motor_temp" in keys), "heater": int("glue_temp" in keys),
            "minutes": min(mins, 10.0),
            "progress": float(card["forecast"]["progress"]) if card.get("forecast") else 0.0,
            "others": min(others, 4)}


_model = None


def load():
    global _model
    if _model is None:
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            with open(os.path.join(here, MODEL), encoding="utf-8") as f:
                _model = json.load(f)
        except OSError:
            _model = {}
    return _model


def score(card, cards):
    """0-100 (%), or None if no model has been trained yet. Fault cards are certain: it has happened."""
    if card["status"] == "FAULT":
        return 100
    m = load()
    if not m:
        return None
    f = features(card, cards)
    z = m["intercept"] + sum(m["coef"][k] * (f[k] - m["mean"][k]) / m["scale"][k] for k in FEATURES)
    return round(100 / (1 + math.exp(-z)))


def band(pct):
    """The tested result for alerts at this confidence, as a short sentence."""
    m = load()
    for b in m.get("test_bands", []):
        if b["low"] <= pct <= b["high"] and b["alerts"]:
            return (f"in testing on {m['test_runs']} unseen runs, alerts at {b['low']}–{b['high']}% were real "
                    f"{b['real']} of {b['alerts']} times")
    return ""


# ── building the test set ─────────────────────────────────────────────────
def _scenario(seed):
    """Every 3rd run gets an injected fault as well, alternating a bearing and a glue heater."""
    import random
    r = random.Random(seed * 101)
    if seed % 3:
        return None, None
    what = "filler_wear" if seed % 2 else "glue_heater"
    return what, r.randint(12, 28)


def _truth(machine, t, ev, what, at_min, start):
    """Was this machine really developing a fault at time t? (from the answer key)"""
    if what == "glue_heater" and machine == "labeller" and t >= start + pd.Timedelta(minutes=at_min):
        return 1
    if what == "filler_wear" and machine == "filler" and t >= start + pd.Timedelta(minutes=at_min):
        return 1
    if machine == "filler" and t >= pd.Timestamp(ev["filler_wear_starts"]):
        return 1
    return 0


def _run(seed):
    import csv
    import random
    from datetime import datetime, timedelta
    import decoder
    import line
    import simulator
    os.makedirs(f"data/_conf/{seed}", exist_ok=True)
    os.chdir(f"data/_conf/{seed}")                       # each run in its own folder (runs in parallel)
    os.makedirs("data", exist_ok=True)
    os.makedirs("data/plc_programs", exist_ok=True)
    what, at = _scenario(seed)
    start = datetime(2026, 11, 27, 9)
    inj = (lambda t: [what] if t == at * 60 else []) if what else None
    rows_ = []
    with open("data/stream.csv", "w", newline="") as f:
        w = cols = None
        for t, row, tags in simulator.simulate(45, seed, inj):
            if cols is None:
                cols = list(row)
                random.Random(seed).shuffle(cols)
                simulator.write_answers(tags, seed, start)
                w = csv.writer(f)
                w.writerow(["timestamp"] + cols)
            w.writerow([str(start + timedelta(seconds=t))] + [row[c] for c in cols])
    full = pd.read_csv("data/stream.csv", parse_dates=["timestamp"])
    ev = json.load(open("data/answers.json"))["events"]
    dec, _ = decoder.decode(full)
    for minute in range(9, 45):
        cut = full.iloc[:minute * 60]
        now = cut["timestamp"].iloc[-1]
        cards = line.alert_cards(cut, dec)
        for c in cards:
            c["_now"] = now
        for c in cards:
            if c["status"] == "FAULT":
                continue
            key = next(k for k, v in line.decoder.MACHINE_NAMES.items() if v == c["machine"])
            rows_.append({"seed": seed, "minute": minute, "machine": key, "title": c["title"],
                          "real": _truth(key, now, ev, what, at, pd.Timestamp(start)), **features(c, cards)})
    return rows_


def build(seeds=range(1, 51)):
    import multiprocessing as mp
    here = os.getcwd()
    with mp.Pool(max(1, (os.cpu_count() or 2))) as pool:
        parts = pool.map(_run_in, [(here, s) for s in seeds])
    df = pd.DataFrame([r for p in parts for r in p])
    df.to_csv(DATASET, index=False)
    print(f"saved {len(df)} alert snapshots from {len(list(seeds))} runs to {DATASET} "
          f"({int(df['real'].sum())} real, {int((1 - df['real']).sum())} harmless)")
    return df


def _run_in(arg):
    here, seed = arg
    os.chdir(here)
    sys.path.insert(0, here)
    # each worker needs the plc programs of its own run: simulator.write_answers renders them in its folder
    return _run(seed)


def train(train_seeds=range(1, 31), test_seeds=range(31, 51)):
    from sklearn.linear_model import LogisticRegression
    df = pd.read_csv(DATASET)
    tr, te = df[df["seed"].isin(train_seeds)], df[df["seed"].isin(test_seeds)]
    mean, scale = tr[FEATURES].mean(), tr[FEATURES].std().replace(0, 1)
    lr = LogisticRegression(C=0.5, max_iter=2000).fit((tr[FEATURES] - mean) / scale, tr["real"])
    p = lr.predict_proba((te[FEATURES] - mean) / scale)[:, 1] * 100
    te = te.assign(conf=p.round())
    bands = []
    for lo, hi in ((90, 100), (70, 89), (50, 69), (0, 49)):
        sel = te[(te["conf"] >= lo) & (te["conf"] <= hi)]
        bands.append({"low": lo, "high": hi, "alerts": int(len(sel)), "real": int(sel["real"].sum())})
    model = {"features": FEATURES, "intercept": float(lr.intercept_[0]),
             "coef": dict(zip(FEATURES, map(float, lr.coef_[0]))), "mean": mean.to_dict(), "scale": scale.to_dict(),
             "train_runs": len(list(train_seeds)), "test_runs": len(list(test_seeds)), "test_bands": bands}
    with open(MODEL, "w", encoding="utf-8") as f:
        json.dump(model, f, indent=1)
    print(f"Trained on {len(tr)} alert snapshots ({len(list(train_seeds))} runs); "
          f"tested on {len(te)} from {len(list(test_seeds))} runs it never saw:\n")
    print(f"{'confidence':>12} {'alerts':>7} {'real':>6} {'right':>7}")
    for b in bands:
        right = f"{b['real'] / b['alerts']:.0%}" if b["alerts"] else "-"
        # high bands are "right" when real; low bands are right when harmless
        print(f"{b['low']:>5}-{b['high']:<4}% {b['alerts']:>7} {b['real']:>6} {right:>7}")
    print("\nWhat moves confidence (positive = more likely real):")
    for k, v in sorted(model["coef"].items(), key=lambda kv: -abs(kv[1])):
        print(f"  {k:12} {v:+.2f}")
    print(f"\nsaved {MODEL}")
    return te


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--build" in sys.argv:
        build()
    if "--train" in sys.argv:
        train()