"""Talking Machines — Tag Translator (Person C).

Works out what every cryptic signal is from HOW IT BEHAVES, without the answer key.

  python decoder.py              decode data/stream.csv -> data/decoded.json, print the table
  python decoder.py --llm        also ask the LLM to double-check low-confidence signals
  python decoder.py --score      compare with data/answers.json (Person A runs this, not the decoder person)
"""
import argparse
import json
import re
import sys

import numpy as np
import pandas as pd

NAMES = {"running": ("Running", ""), "fault": ("Fault", ""), "speed": ("Speed", "bottles/min"),
         "count": ("Bottle counter", "bottles"), "motor_current": ("Motor current", "A"),
         "motor_temp": ("Motor temperature", "°C"), "vibration": ("Vibration", "mm/s"),
         "fill_pressure": ("Fill pressure", "bar"), "cap_torque": ("Capping torque", "Nm"),
         "glue_temp": ("Glue heater temperature", "°C"), "case_count": ("Case counter", "cases"),
         "layer_count": ("Layer counter", "layers"),
         # palletising cobot
         "gripper_closed": ("Gripper closed", ""), "zone_occupied": ("Safety zone occupied", ""),
         "cycle_count": ("Cases palletised", "cases"), "tcp_speed": ("Tool speed", "mm/s"),
         "speed_override": ("Speed override", "%"),
         "j1_torque": ("Joint 1 (base) torque", "Nm"), "j2_torque": ("Joint 2 (shoulder) torque", "Nm"),
         "j3_torque": ("Joint 3 (elbow) torque", "Nm"), "j4_torque": ("Joint 4 (wrist 1) torque", "Nm"),
         "j5_torque": ("Joint 5 (wrist 2) torque", "Nm"), "j6_torque": ("Joint 6 (wrist 3) torque", "Nm")}
MACHINE_NAMES = {"depalletiser": "Depalletiser", "filler": "Filler", "capper": "Capper",
                 "labeller": "Labeller", "case_packer": "Case packer", "cobot": "Cobot"}


# ── 1. fingerprint: how each signal behaves ────────────────────────────────
def fingerprint(s):
    v = s.astype(float)
    d = v.diff().dropna()
    uniq = set(np.unique(v.round(6)))
    # a counter only goes up, but real counters get reset (new shift, operator reset), and a PLC can still
    # show last shift's value for its first scan: allow a few drops if it steps up many times
    drops, rises = int((d < 0).sum()), int((d > 0).sum())
    counter = bool(v.is_monotonic_increasing and v.iloc[-1] > v.iloc[0]) or (
        0 < drops <= 3 and rises >= 20)
    return {
        "bool": uniq <= {0.0, 1.0},
        "counter": counter,
        "rate_per_min": float(d.clip(lower=0).sum() / (len(v) / 60)),
        "mean": float(v.mean()), "median": float(v.median()), "std": float(v.std()),
        "min": float(v.min()), "max": float(v.max()),
        "share_ones": float(v.mean()) if uniq <= {0.0, 1.0} else None,
        # smooth = changes little second to second compared with its overall spread (temperatures)
        "smooth": float(d.abs().mean() / (v.std() or 1)),
    }


# ── 2. group signals that belong to the same controller block ─────────────
def address_group(tag):
    """Signals stored next to each other in a PLC usually belong to one machine."""
    m = re.match(r"(ROB\d+)\.", tag)
    if m:
        return m.group(1)                             # robot controller
    m = re.match(r"DB(\d+)\.", tag)
    if m:
        return "DB" + m.group(1)                      # Siemens data block
    m = re.match(r"(M\d+)_", tag)
    if m:
        return m.group(1)                             # legacy prefix
    m = re.match(r"[FN]\d+:(\d+)$", tag)
    if m:
        return "AB" + str(int(m.group(1)) // 10)      # Allen-Bradley element range
    m = re.match(r"B\d+:(\d+)/", tag)
    if m:
        return "AB" + m.group(1)                      # Allen-Bradley bit word
    return tag


# ── 3. name signals inside one machine from their behaviour ───────────────
def classify_group(df, tags):
    fp = {t: fingerprint(df[t]) for t in tags}
    out, reasons = {}, {}
    bools = [t for t in tags if fp[t]["bool"]]
    counters = sorted([t for t in tags if fp[t]["counter"] and not fp[t]["bool"]],
                      key=lambda t: -fp[t]["rate_per_min"])
    reals = [t for t in tags if t not in bools and t not in counters]

    # running = the on/off signal that is on exactly while the machine is producing (its counter climbs);
    # a fault flag is the other one. (Not "on most of the time": after a long stop that would be wrong.)
    making = None
    if counters:
        fastest = max(counters, key=lambda t: fp[t]["rate_per_min"])
        making = df[fastest].diff().rolling(5, min_periods=1).sum().fillna(0) > 0
    if bools:
        agree = {t: ((df[t] > 0.5) == making).mean() if making is not None else fp[t]["share_ones"] for t in bools}
        run_flag = max(bools, key=agree.get)
        for t in bools:
            out[t] = "running" if t == run_flag else "fault"
            reasons[t] = (f"on/off signal, on {fp[t]['share_ones']:.0%} of the time"
                          + (", on exactly while the counter climbs" if t == run_flag and making is not None else ""))
    if counters:
        main = counters[0]
        out[main] = "count"
        reasons[main] = f"only goes up, about {fp[main]['rate_per_min']:.0f} per minute"
        for t in counters[1:]:
            ratio = fp[main]["rate_per_min"] / max(fp[t]["rate_per_min"], 1e-9)
            out[t] = "case_count" if ratio < 60 else "layer_count"
            reasons[t] = f"only goes up, 1 for every {ratio:.0f} bottles"

    run = next((t for t in bools if out[t] == "running"), None)
    on = df[run] > 0.5 if run else pd.Series(True, index=df.index)
    left = list(reals)
    # typical value WHILE RUNNING: after a long stop the plain median would just be the stopped value
    med = {t: float(df.loc[on, t].median()) if on.sum() > 60 else fp[t]["median"] for t in reals}

    def take(name, pick, why):
        if pick in left:
            left.remove(pick)
            out[pick] = name
            reasons[pick] = why

    # speed: matches how fast the bottle counter climbs (bottles per minute)
    if counters:
        per_min = df[counters[0]].diff().rolling(60).sum()
        rate = float(per_min[per_min > 0].median()) if (per_min > 0).any() else 0.0   # while running only
        cand = [t for t in left if rate > 0 and abs(med[t] - rate) < 0.15 * rate]
        if cand:
            t = min(cand, key=lambda t: abs(med[t] - rate))
            take("speed", t, f"around {med[t]:.0f}, the same as the counter's {rate:.0f} per minute")
    speed = next((t for t, n in out.items() if n == "speed"), None)

    # heater: high and steady even when the machine stops
    for t in list(left):
        stopped = df.loc[~on, t] if run and (~on).any() else df[t]
        # "stays hot while stopped" only counts with enough stopped time: a few cold seconds at start-up
        # (a real PLC that starts before its sensors report) must not hide a heater
        hot_when_stopped = len(stopped) < 60 or stopped.median() > 100
        if fp[t]["median"] > 100 and df[t].diff().abs().median() < 1.5 and hot_when_stopped:
            # hot, moves only slowly, and stays hot while the machine is stopped (a speed drops to 0)
            take("glue_temp", t, f"steady around {fp[t]['median']:.0f} even when stopped (a heater)")

    # temperature: 20-90 and changes slowly
    temps = [t for t in left if 20 <= fp[t]["median"] <= 90 and fp[t]["smooth"] < 0.05]
    if temps:
        t = min(temps, key=lambda t: fp[t]["smooth"])
        take("motor_temp", t, f"{fp[t]['min']:.0f}-{fp[t]['max']:.0f}, changes slowly")

    # current: follows the speed closely while running
    if speed and left:
        # while running; if the speed never changes while running (a PLC that reports its exact setpoint),
        # use the whole run instead: the current drops when the machine stops
        rows = on if on.sum() > 30 and df.loc[on, speed].std() > 1e-6 else pd.Series(True, index=df.index)
        cand = [t for t in left if med[t] > 3] or left
        with np.errstate(invalid="ignore", divide="ignore"):       # a constant signal has no correlation
            corr = {t: abs(df.loc[rows, t].corr(df.loc[rows, speed])) for t in cand}
        corr = {t: (c if c == c else 0) for t, c in corr.items()}
        t = max(corr, key=corr.get)
        if med[t] > 3:
            take("motor_current", t, f"rises and falls with speed (r={corr[t]:.2f}), about {med[t]:.1f}")

    # vibration: small, noisy, never exactly steady while running
    noisy = [t for t in left if med[t] < 10 and df.loc[on, t].std() > 0.1]
    if noisy:
        # vibration jitters every second; a process value (e.g. capping torque) is steady with occasional drops,
        # so compare the TYPICAL second-to-second change, not the overall spread (a jam's drop would inflate that)
        jitter = {t: float(df.loc[on, t].diff().abs().median()) / (abs(med[t]) or 1) for t in noisy}
        t = max(noisy, key=jitter.get)
        take("vibration", t, f"small and noisy, about {med[t]:.1f}")

    # whatever is left is the machine's own process value
    for t in left:
        out[t] = "process"
        reasons[t] = f"steady about {med[t]:.2f} while running, drops when stopped"
    return out, reasons, fp


# ── 3b. a robot arm looks different: many joint signals that swing in a repeating cycle ───
def looks_like_robot(df, tags):
    fp = {t: fingerprint(df[t]) for t in tags}
    bools = [t for t in tags if fp[t]["bool"]]
    reals = [t for t in tags if not fp[t]["bool"] and not fp[t]["counter"]]
    flips = {t: (df[t].diff().abs().sum() / (len(df) / 60)) for t in bools}
    return len(reals) >= 6 and any(f >= 4 for f in flips.values())     # a gripper opening and closing


def classify_robot(df, tags):
    fp = {t: fingerprint(df[t]) for t in tags}
    out, reasons = {}, {}
    bools = [t for t in tags if fp[t]["bool"]]
    counters = [t for t in tags if fp[t]["counter"] and not fp[t]["bool"]]
    reals = [t for t in tags if t not in bools and t not in counters]
    flips = {t: df[t].diff().abs().sum() / (len(df) / 60) for t in bools}

    # gripper: opens and closes every cycle
    grip = max(bools, key=lambda t: flips[t])
    out[grip], reasons[grip] = "gripper_closed", f"switches {flips[grip]:.0f} times a minute (every pick)"
    for t in counters:
        out[t], reasons[t] = "cycle_count", f"only goes up, about {fp[t]['rate_per_min']:.1f} per minute"
    # speed override: only a couple of fixed values, 100 most of the time
    for t in reals:
        vals = set(np.unique(df[t].round(1)))
        if len(vals) <= 4 and fp[t]["max"] <= 100.5 and fp[t]["median"] >= 90:
            out[t], reasons[t] = "speed_override", f"jumps between {', '.join(f'{v:g}' for v in sorted(vals))}: a speed setting in %"
    over = next((t for t, n in out.items() if n == "speed_override"), None)
    left = [t for t in reals if t not in out]
    # tool speed: the biggest value, and it falls whenever the override drops
    if left:
        t = max(left, key=lambda t: float(df[t].quantile(0.9)))       # fastest while moving (it is 0 when idle)
        out[t], reasons[t] = "tcp_speed", f"up to about {df[t].quantile(0.9):.0f} and drops with the speed setting"
        left.remove(t)
    # joints: the shoulder holds the most weight, then the elbow, the base, then the three wrist joints
    order = ["j2_torque", "j3_torque", "j1_torque", "j4_torque", "j5_torque", "j6_torque"]
    for name, t in zip(order, sorted(left, key=lambda t: -abs(fp[t]["median"]))):
        out[t], reasons[t] = name, (f"about {fp[t]['median']:.0f} Nm, rises when the gripper holds a case; "
                                    f"ranked by load (shoulder carries most)")
    # the remaining on/off signals: running is mostly on; the safety zone is on exactly when the speed setting drops
    rest = [t for t in bools if t != grip]
    if over is not None:
        zone = [t for t in rest if (df[t] == 1).any() and (df.loc[df[t] == 1, over] < df[over].max() - 1).mean() > 0.9]
        if zone:
            out[zone[0]], reasons[zone[0]] = "zone_occupied", "on exactly when the robot slows to collaborative speed"
            rest.remove(zone[0])
    tcp = next((t for t, n in out.items() if n == "tcp_speed"), None)
    if rest:
        moving = df[tcp] > 1 if tcp else None
        agree = {t: ((df[t] > 0.5) == moving).mean() if moving is not None else fp[t]["share_ones"] for t in rest}
        run_flag = max(rest, key=agree.get)
        for t in rest:
            out[t] = "running" if t == run_flag else "fault"
            reasons[t] = (f"on/off signal, on {fp[t]['share_ones']:.0%} of the time"
                          + (", on exactly while the arm moves" if t == run_flag else ""))
    return out, reasons, fp


# ── 4. which machine is which, from how the line behaves ───────────────────
def identify_machines(df, groups, signals):
    ident, why = {}, {}

    def has(g, name):
        return next((t for t in groups[g] if signals[t] == name), None)

    left = list(groups)
    for g in list(left):
        if has(g, "glue_temp"):
            ident[g], why[g] = "labeller", "it has a heater (glue for labels)"
            left.remove(g)
        elif has(g, "case_count"):
            ident[g], why[g] = "case_packer", "it counts cases, not just bottles"
            left.remove(g)
        elif has(g, "layer_count"):
            ident[g], why[g] = "depalletiser", "it counts layers of a pallet (first machine on the line)"
            left.remove(g)
    # of the rest: the one that keeps stopping is the capper, the one slowed by it is the filler
    stops = {}
    for g in left:
        run = has(g, "running")
        stops[g] = int((df[run].diff() == -1).sum()) if run else 0
    unsure = len(left) > 1 and len(set(stops.values())) == 1          # no stoppage seen yet
    for g in sorted(left, key=lambda g: -stops[g]):
        if "capper" not in ident.values():
            ident[g], why[g] = "capper", (f"it stopped on its own {stops[g]} times" if not unsure else
                                          "GUESS: filler and capper look the same until one of them stops")
        elif "filler" not in ident.values():
            ident[g], why[g] = "filler", ("it slows down whenever the capper stops" if not unsure else
                                          "GUESS: filler and capper look the same until one of them stops")
        else:
            ident[g], why[g] = "unknown", "not enough evidence"
    return ident, why


def decode(data="data/stream.csv"):
    """data: a CSV path or a DataFrame with a timestamp column."""
    df = data if isinstance(data, pd.DataFrame) else pd.read_csv(data)
    df = df.drop(columns=["timestamp"], errors="ignore")
    groups = {}
    for t in df.columns:
        groups.setdefault(address_group(t), []).append(t)
    signals, reasons, fps = {}, {}, {}
    robots = {g for g, tags in groups.items() if looks_like_robot(df, tags)}
    for g, tags in groups.items():
        s, r, f = (classify_robot if g in robots else classify_group)(df, tags)
        signals.update(s), reasons.update(r), fps.update(f)
    machines, mwhy = identify_machines(df, {g: t for g, t in groups.items() if g not in robots}, signals)
    for g in robots:
        machines[g], mwhy[g] = "cobot", "a robot arm: six joint loads that swing with every pick, and a gripper"
    result = {}
    for g, tags in groups.items():
        m = machines[g]
        for t in tags:
            sig = signals[t]
            if sig == "process":
                sig = {"filler": "fill_pressure", "capper": "cap_torque"}.get(m, "process")
            name, unit = NAMES.get(sig, ("Process value", ""))
            conf = "low" if sig == "process" or m == "unknown" or mwhy[g].startswith("GUESS") else "high"
            result[t] = {"machine": m, "signal": sig, "name": f"{MACHINE_NAMES.get(m, m)} {name.lower()}",
                         "unit": unit, "confidence": conf,
                         "reason": f"{reasons[t]}; machine: {mwhy[g]}"}
    return result, fps


def llm_check(result, fps):
    """Ask the LLM about low-confidence signals only; rules stay in charge."""
    try:
        import os
        from dotenv import load_dotenv
        from groq import Groq
        load_dotenv()
        q = Groq(api_key=os.environ["GROQ_API_KEY"])
    except Exception as e:
        print("LLM not available, rules only:", e)
        return result
    for t, r in result.items():
        if r["confidence"] != "low":
            continue
        f = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in fps[t].items()}
        prompt = (f"A bottling-line PLC signal named {t} on the {r['machine']} behaves like this: {f}. "
                  "In at most 6 words, what physical quantity is it most likely? Answer with the name only.")
        try:
            a = q.chat.completions.create(model="openai/gpt-oss-120b",
                                          messages=[{"role": "user", "content": prompt}])
            r["llm_guess"] = a.choices[0].message.content.strip()
        except Exception as e:
            r["llm_guess"] = f"(LLM failed: {e})"
    return result


def score(result, answers="data/answers.json"):
    with open(answers, encoding="utf-8") as f:
        key = json.load(f)["tags"]
    ok = [t for t in key if result.get(t, {}).get("machine") == key[t]["machine"]
          and result.get(t, {}).get("signal") == key[t]["signal"]]
    print(f"\nDecoded correctly: {len(ok)} / {len(key)}")
    for t in key:
        if t not in ok:
            got = result.get(t, {})
            print(f"  wrong: {t}: guessed {got.get('machine')}/{got.get('signal')}, "
                  f"really {key[t]['machine']}/{key[t]['signal']}")
    return len(ok), len(key)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")    # Windows consoles/pipes default to cp1252
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    result, fps = decode()
    if a.llm:
        result = llm_check(result, fps)
    with open("data/decoded.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, ensure_ascii=False)
    for t, r in sorted(result.items(), key=lambda kv: (kv[1]["machine"], kv[1]["signal"])):
        print(f"{t:14} → {r['name']:34} {r['unit']:12} [{r['confidence']}]  {r['reason']}")
    print("\nsaved data/decoded.json")
    if a.score:
        score(result)