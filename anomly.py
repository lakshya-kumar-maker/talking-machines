"""Talking Machines — second opinion: Isolation Forest anomaly detection.

The drift check (warning.py) looks at one signal at a time. This looks at several signals of a machine
TOGETHER, so it can also catch combinations that are each "almost normal" on their own.

For every machine (not the cobot, whose slowdowns for people are normal):
  1. features each second, lightly smoothed (5 s): vibration, how shaky the vibration is, motor current,
     current per bottle (current ÷ speed) and the machine's own process value (fill pressure, capping
     torque, glue temperature)
  2. learn "normal" from the healthy minutes 3-8, only while the machine runs at full speed
  3. score every second, then average the score over 30 s; 0 = looks like normal, 1 = clearly unusual
Why smooth the score and not the inputs: an Isolation Forest only knows the range it was trained on, so it
needs to see the normal second-to-second noise. Averaging its score afterwards removes the flicker.
Seconds when the machine is stopped or slowed by another machine are not scored: a machine waiting for
its neighbour is not behaving abnormally.

  python anomaly.py          score data/stream.csv (decodes the tag names itself)
"""
import json
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

BASE = (3, 8)          # healthy minutes used to learn normal (the same as the drift check)
SMOOTH = 5             # seconds of smoothing on the inputs
SCORE_SMOOTH = 30      # seconds the score is averaged over
MARGIN, RANGE = 0.03, 0.12   # raw forest score above (training max + MARGIN) starts counting; + RANGE = score 1
FLAG = 0.35            # score above this ... (healthy machines never stayed above 0.01 in 12 test runs)
HOLD = 30              # ... for this many seconds = an anomaly
LABELS = {"vibration": "vibration", "vib_spread": "vibration shakiness", "motor_current": "motor current",
          "amps_per_bottle": "current per bottle", "fill_pressure": "fill pressure",
          "cap_torque": "capping torque", "glue_temp": "glue temperature"}
PROCESS = ("fill_pressure", "cap_torque", "glue_temp")
_MODELS = {}


def _features(df, sig):
    f = pd.DataFrame(index=df.index)
    if "vibration" in sig:
        v = df[sig["vibration"]].astype(float)
        f["vibration"] = v.rolling(SMOOTH, min_periods=1).mean()
        f["vib_spread"] = v.rolling(SMOOTH, min_periods=2).std()
    if "motor_current" in sig:
        f["motor_current"] = df[sig["motor_current"]].astype(float).rolling(SMOOTH, min_periods=1).mean()
        if "speed" in sig:
            spd = df[sig["speed"]].astype(float).clip(lower=1)
            f["amps_per_bottle"] = (df[sig["motor_current"]] / spd).rolling(SMOOTH, min_periods=1).mean()
    for p in PROCESS:
        if p in sig:
            f[p] = df[sig[p]].astype(float).rolling(SMOOTH, min_periods=1).mean()
    return f


def score_machine(df, sig):
    """-> DataFrame(score, flagged, top) indexed like df, or None if the machine can't be scored."""
    if "speed" not in sig or "running" not in sig or len(df) < BASE[1] * 60 + SMOOTH:
        return None
    feats = _features(df, sig)
    if feats.shape[1] < 2:
        return None
    spd = df[sig["speed"]].astype(float)
    rated = spd[spd > 0.5 * spd.max()].median()
    # full speed for the whole smoothing window, so a restart or a slowdown doesn't look unusual
    full = (df[sig["running"]] > 0.5) & (spd > 0.9 * rated)
    steady = full.rolling(SCORE_SMOOTH, min_periods=SCORE_SMOOTH).min().fillna(0).astype(bool)
    lo, hi = BASE[0] * 60, BASE[1] * 60
    train = feats.iloc[lo:hi][steady.iloc[lo:hi]].dropna()
    if len(train) < 60:
        return None
    mu, sd = train.mean(), train.std().replace(0, np.nan)
    sd = sd.fillna(1e-6).clip(lower=0.005 * mu.abs().clip(lower=0.05))     # small floor so a value that never
                                                                            # moves can't look "infinitely" off
    z = (feats - mu) / sd
    # the healthy minutes never change once recorded, so train once per run and reuse the model
    key = (tuple(sorted(sig.items())), str(df.index[0]), len(train), round(float(train.values.sum()), 6))
    if key not in _MODELS:
        _MODELS[key] = IsolationForest(n_estimators=150, random_state=0).fit(z.loc[train.index])
    model = _MODELS[key]
    raw = pd.Series(np.nan, index=df.index)
    ok = steady & z.notna().all(axis=1)
    raw[ok] = -model.score_samples(z[ok])                          # the forest's anomaly score, ~0.45 = normal
    raw = raw.rolling(SCORE_SMOOTH, min_periods=SCORE_SMOOTH).mean()
    # 0..1: 0 = as normal as the healthy minutes, 1 = far outside anything seen while healthy
    edge = float(raw.iloc[lo:hi].max()) + MARGIN
    score = ((raw - edge) / RANGE).clip(0, 1)
    flagged = (score > FLAG).rolling(HOLD, min_periods=HOLD).sum() >= HOLD
    # which feature is most out of line (explains the score in plain words)
    zs = z.rolling(SCORE_SMOOTH, min_periods=1).mean()
    top = zs.abs().idxmax(axis=1).where(score > FLAG)
    direction = pd.Series([("up" if zs.at[i, c] > 0 else "down") if isinstance(c, str) else None
                           for i, c in top.items()], index=df.index)
    return pd.DataFrame({"score": score, "flagged": flagged, "top": top, "dir": direction})


def score_line(df, dec):
    """{machine: DataFrame(score, flagged, top, dir)} for every machine that can be scored."""
    sigs = {}
    for tag, r in dec.items():
        sigs.setdefault(r["machine"], {})[r["signal"]] = tag
    out = {}
    for m, sig in sigs.items():
        if m in ("cobot", "unknown"):
            continue
        s = score_machine(df, sig)
        if s is not None:
            out[m] = s
    return out


def findings(df, scores):
    """[{machine, since, score, why}] for machines flagged now or earlier, oldest first."""
    out = []
    for m, s in scores.items():
        if not s["flagged"].any():
            continue
        first = s.index[s["flagged"].values.argmax()]
        window = s.loc[first:]
        reasons = window["top"].dropna()
        main = reasons.value_counts().index[0] if len(reasons) else None
        d = window.loc[window["top"] == main, "dir"].mode()
        out.append({"machine": m, "since": df["timestamp"][first], "score": float(window["score"].max()),
                    "now": float(s["score"].dropna().iloc[-1]) if s["score"].notna().any() else None,
                    "why": f"{LABELS.get(main, main)} {d.iloc[0] if len(d) else ''}".strip() if main else ""})
    return sorted(out, key=lambda f: f["since"])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import decoder
    df = pd.read_csv("data/stream.csv", parse_dates=["timestamp"])
    dec, _ = decoder.decode(df)                       # names from this run's data (no stale decoded.json)
    found = findings(df, score_line(df, dec))
    if not found:
        print("Isolation Forest: nothing unusual.")
    for f in found:
        print(f"⚠ {f['since']:%H:%M}  {decoder.MACHINE_NAMES.get(f['machine'], f['machine'])}: unusual "
              f"(score up to {f['score']:.2f}), mainly {f['why']}")