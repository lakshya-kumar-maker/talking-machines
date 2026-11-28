"""Talking Machines — machine dependency map: which machine stops or slows which, and how we know.

Two independent sources:
  DATA  every time a machine's fault light comes on, look at the other machines in the next 30 s:
        did one stop, or drop below 70% of its normal speed, when it was running normally just before?
  CODE  read each machine's PLC program (translator.py): which OTHER machine's signals does it check, and
        does it then stop the machine (running := FALSE) or slow it (a lower speed)?
An arrow seen in both is "confirmed". One seen only in the data is physical (e.g. bottles backing up on
the conveyor); one only in the code is a rule that hasn't been triggered yet.

  python dependency.py        print the map for data/stream.csv
"""

import os
import sys

import pandas as pd

WINDOW = 30  # seconds after a fault starts in which a reaction counts
SLOW = 0.7  # below this share of normal speed = slowed
ORDER = ["depalletiser", "filler", "capper", "labeller", "case_packer", "cobot"]


def _signals(dec):
    sig = {}
    for t, r in dec.items():
        sig.setdefault(r["machine"], {})[r["signal"]] = t
        if r["signal"] == "tcp_speed":
            sig[r["machine"]].setdefault("speed", t)
    return sig


# ── 1. from the data ──────────────────────────────────────────────────────
def from_data(df, dec):
    """{(source, target): {"stops": n, "slows": n, "faults": n, "lag_s": [..], "times": [..]}}"""
    sig = _signals(dec)
    edges = {}
    for a in ORDER:
        if "fault" not in sig.get(a, {}):
            continue
        f = df[sig[a]["fault"]].astype(int)
        onsets = df.index[(f == 1) & (f.shift(1, fill_value=0) == 0)]
        for i in onsets:
            for b in ORDER:
                s = sig.get(b, {})
                if b == a or "running" not in s or "speed" not in s:
                    continue
                if "fault" in s and df[s["fault"]].iloc[max(i - 2, 0) : i + WINDOW].max() > 0:
                    continue  # b had its own fault: not a knock-on effect
                if "zone_occupied" in s and df[s["zone_occupied"]].iloc[max(i - 5, 0) : i + WINDOW].max() > 0:
                    continue  # the cobot slowed for a person, not for a
                run, spd = df[s["running"]] > 0.5, df[s["speed"]].astype(float)
                normal = spd[run].median()
                before = slice(max(i - 5, 0), i)
                if not run.iloc[before].all() or spd.iloc[before].median() < 0.9 * normal:
                    continue  # it wasn't running normally just before
                after = range(i, min(i + WINDOW, len(df)))
                stopped = next((j for j in after if not run.iloc[j]), None)
                slow = next((j for j in after if run.iloc[j] and spd.iloc[j] < SLOW * normal), None)
                e = edges.setdefault((a, b), {"stops": 0, "slows": 0, "faults": len(onsets), "lag_s": [], "times": []})
                if stopped is not None and (slow is None or stopped <= slow + 3):
                    e["stops"] += 1
                    e["lag_s"].append(stopped - i)
                    e["times"].append(df["timestamp"].iloc[i])
                elif slow is not None:
                    e["slows"] += 1
                    e["lag_s"].append(slow - i)
                    e["times"].append(df["timestamp"].iloc[i])
    return {k: v for k, v in edges.items() if v["stops"] or v["slows"]}


# ── 2. from the PLC code ──────────────────────────────────────────────────
def from_code(dec, folder="data/plc_programs"):
    """{(source, target): {"effect": "stops"|"slows", "when": "filler fault", "file": "PLC3_OB1.st",
    "speed": 120.0}}"""
    import translator

    edges = {}
    if not os.path.isdir(folder):
        return edges
    sig = _signals(dec)
    for f in sorted(os.listdir(folder)):
        if not f.endswith(".st"):
            continue
        with open(os.path.join(folder, f), encoding="utf-8") as fh:
            rules = [r for _, r in translator.split_rules(fh.read())]
        me = translator.which_machine([(None, r) for r in rules], dec)
        if not me:
            continue
        normal_speed = max(
            [s[2][1] for r in rules for s in _sets(r) if s[1] == sig.get(me, {}).get("speed") and s[2][0] == "num"]
            or [0]
        )

        for r in rules:
            if r[0] != "if":
                continue
            for cond, body in r[1]:
                for tag, negated in _tags(cond):
                    other = dec.get(tag, {}).get("machine")
                    if not other or other == me or other not in ORDER or me not in ORDER:
                        continue  # a machine not identified yet has no place on the map
                    signal = dec[tag]["signal"]
                    when = f"{other.replace('_', ' ')} {'not ' if negated else ''}{'running' if signal == 'running' else signal}"
                    when = when.replace("not running", "stopped") if signal == "running" else when
                    effect, speed = None, None
                    for s in body:
                        if s[0] != "set":
                            continue
                        if s[1] == sig[me].get("running") and s[2] == ("bool", False):
                            effect = "stops"
                        elif s[1] == sig[me].get("speed") and s[2][0] == "num" and effect != "stops":
                            if s[2][1] == 0:
                                effect = "stops"
                            elif normal_speed and s[2][1] < normal_speed:
                                effect, speed = "slows", s[2][1]
                    if effect:
                        edges.setdefault((other, me), {"effect": effect, "when": when, "file": f, "speed": speed})
    return edges


def _sets(rule):
    if rule[0] == "set":
        return [rule]
    if rule[0] == "if":
        out = []
        for _, body in rule[1]:
            for s in body:
                out += _sets(s)
        for s in rule[2] or []:
            out += _sets(s)
        return out
    return []


def _tags(node, negated=False):
    """[(tag, negated)] read by a condition."""
    k = node[0]
    if k == "tag":
        return [(node[1], negated)]
    if k == "not":
        return _tags(node[1], not negated)
    if k in ("and", "or"):
        return _tags(node[1], negated) + _tags(node[2], negated)
    if k == "cmp":
        return _tags(node[2], negated) + _tags(node[3], negated)
    return []


# ── 3. both together ──────────────────────────────────────────────────────
def build(df, dec):
    """[{source, target, effect, data: {...}|None, code: {...}|None, status}] in line order."""
    data, code = from_data(df, dec), from_code(dec)
    out = []
    for key in sorted(set(data) | set(code), key=lambda k: (ORDER.index(k[0]) if k[0] in ORDER else 99,
                                                          ORDER.index(k[1]) if k[1] in ORDER else 99)):
        d, c = data.get(key), code.get(key)
        effect = c["effect"] if c else ("stops" if d["stops"] >= d["slows"] else "slows")
        status = "confirmed" if d and c else ("seen in data" if d else "in code, not seen yet")
        out.append({"source": key[0], "target": key[1], "effect": effect, "data": d, "code": c, "status": status})
    return out


def describe(e: dict, names: dict) -> str:
    """Return one dependency edge as a plain-English sentence."""
    a, b = names.get(e["source"], e["source"]), names.get(e["target"], e["target"])
    parts = [f"{a} → {b}: {e['effect']} it"]
    if e["data"]:
        d = e["data"]
        n = d["stops"] + d["slows"]
        lag = sorted(d["lag_s"])[len(d["lag_s"]) // 2]
        parts.append(
            f"seen {n} of {d['faults']} times the {a.lower()} faulted ("
            + ("immediately" if lag <= 1 else f"within {lag} s")
            + ")"
        )
    if e["code"]:
        c = e["code"]
        how = f"slows it to {c['speed']:g}" if c["effect"] == "slows" and c["speed"] else c["effect"] + " it"
        parts.append(f"{c['file']} {how} when {c['when']}")
    return " · ".join(parts) + f" [{e['status']}]"


def graph_dot(edges, names):
    """Graphviz text for the map (Streamlit draws it with st.graphviz_chart)."""
    color = {"confirmed": "#2e9e5b", "seen in data": "#e08a00", "in code, not seen yet": "#3b82f6"}
    present = sorted({e["source"] for e in edges} | {e["target"] for e in edges} | set(ORDER[:5]), key=ORDER.index)
    lines = [
        "digraph G {",
        'rankdir=LR; bgcolor="transparent";',
        'node [shape=box, style="rounded,filled", fillcolor="#f4f4f5", fontname="Helvetica", fontsize=12];',
        'edge [fontname="Helvetica", fontsize=10];',
    ]
    for m in present:
        lines.append(f'"{m}" [label="{names.get(m, m)}"];')
    for a, b in zip(ORDER, ORDER[1:]):  # the conveyor itself, for orientation
        if a in present and b in present:
            lines.append(f'"{a}" -> "{b}" [style=dotted, color="#a1a1aa", arrowhead=none, weight=10];')
    for e in edges:
        d = e["data"]
        n = (d["stops"] + d["slows"]) if d else 0
        label = e["effect"] + (f" ({n}×)" if n else "")
        width = 1 + min(n, 5) * 0.6
        lines.append(
            f'"{e["source"]}" -> "{e["target"]}" [label="{label}", color="{color[e["status"]]}", '
            f'fontcolor="{color[e["status"]]}", penwidth={width:.1f}, '
            f"style={'dashed' if e['status'] == 'in code, not seen yet' else 'solid'}, constraint=false];"
        )
    lines.append("}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import decoder

    df = pd.read_csv("data/stream.csv", parse_dates=["timestamp"])
    dec, _ = decoder.decode(df)
    for e in build(df, dec):
        print(describe(e, decoder.MACHINE_NAMES))
