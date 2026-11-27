"""Talking Machines — shared logic for the dashboard: line status, warnings and Ask the Line."""
import os

import pandas as pd

import decoder
import warning

MACHINE_ORDER = ["depalletiser", "filler", "capper", "labeller", "case_packer", "cobot"]


SNAP = "data/_snapshot.csv"


def load(path="data/stream.csv"):
    """Read the stream safely while the simulator is still writing it (the last line may be half-written)."""
    df = pd.read_csv(path, parse_dates=["timestamp"], on_bad_lines="skip").dropna().reset_index(drop=True)
    df.to_csv(SNAP, index=False)
    dec, _ = decoder.decode(SNAP)
    return df, dec


def by_machine(dec):
    """{machine: {signal: tag}}; the cobot's tool speed also counts as its "speed"."""
    out = {}
    for tag, r in dec.items():
        out.setdefault(r["machine"], {})[r["signal"]] = tag
        if r["signal"] == "tcp_speed":
            out[r["machine"]].setdefault("speed", tag)
    return out


def speed_unit(m):
    return "mm/s" if m == "cobot" else "bottles/min"


def status(df, dec):
    """Latest state of every machine, in line order."""
    rows = []
    last = df.iloc[-1]
    for m in MACHINE_ORDER:
        sig = by_machine(dec).get(m, {})
        get = lambda s: (last[sig[s]] if s in sig else None)
        stops = int((df[sig["running"]].diff() == -1).sum()) if "running" in sig else 0
        rows.append({"machine": decoder.MACHINE_NAMES[m], "key": m, "running": bool(get("running")),
                     "fault": bool(get("fault")), "speed": get("speed"), "unit": speed_unit(m),
                     "current_A": get("motor_current"), "temp_C": get("motor_temp"), "vibration": get("vibration"),
                     "zone": bool(get("zone_occupied")) if "zone_occupied" in sig else None,
                     "override": get("speed_override"), "stops": stops})
    return pd.DataFrame(rows)


def warning_lines(path, dec, window_s=180, top=3):
    """The same grouped warnings as warning.py prints, returned as text lines."""
    names = {t: r["name"] for t, r in dec.items()}
    alerts = warning.drift_warnings(SNAP if os.path.exists(SNAP) else path, names=names)
    if not alerts.empty:   # the cobot slowing for a person is expected behaviour, not an early warning
        expected = {t for t, r in dec.items() if r["machine"] == "cobot"
                    and r["signal"] in ("tcp_speed", "speed_override", "running", "cycle_count")}
        alerts = alerts[~alerts["tag"].isin(expected)]
    if alerts.empty:
        return []
    lines, group, start = [], [], None

    def flush():
        best = {}
        for g in group:
            if g["tag"] not in best or abs(g["change_pct"]) > abs(best[g["tag"]]["change_pct"]):
                best[g["tag"]] = g
        ranked = sorted(best.values(), key=lambda g: -abs(g["z"]))
        parts = [f"{g['name']} {'up' if g['change_pct'] > 0 else 'down'} {abs(g['change_pct'])}%"
                 for g in ranked[:top]]
        more = f" (+{len(ranked) - top} more)" if len(ranked) > top else ""
        lines.append(f"{group[0]['time']:%H:%M}  " + ", ".join(parts) + more)

    for _, a in alerts.iterrows():
        if start is None or (a["time"] - start).total_seconds() > window_s:
            if group:
                flush()
            group, start = [], a["time"]
        group.append(a)
    flush()
    return lines


def events(df, dec, limit=6):
    """Times each machine stopped, or ran slow (under 70% of its normal speed), as short text."""
    out = []
    for m in MACHINE_ORDER:
        sig = by_machine(dec).get(m, {})
        if "running" not in sig or "speed" not in sig:
            continue
        run, spd = df[sig["running"]] > 0.5, df[sig["speed"]]
        normal = spd[run].median()
        for label, mask in (("stopped", ~run), ("ran slow", run & (spd < 0.7 * normal))):
            starts = df.index[mask & ~mask.shift(fill_value=False)]
            ends = df.index[mask & ~mask.shift(-1, fill_value=False)]
            spans = [(df["timestamp"][s], (e - s + 1)) for s, e in zip(starts, ends) if e - s >= 4]
            if spans:
                times = ", ".join(f"{t:%H:%M} ({d}s)" for t, d in spans[:limit])
                extra = f" and {len(spans) - limit} more" if len(spans) > limit else ""
                out.append(f"{decoder.MACHINE_NAMES[m]} {label} at {times}{extra}")
    return out


def facts(df, dec, warns):
    """A short, factual description of the line for the language model (and the fallback)."""
    st = status(df, dec)
    lines = [f"Time now: {df['timestamp'].iloc[-1]:%H:%M}. Line order: depalletiser → filler → capper → "
             "labeller → case packer."]
    for _, r in st.iterrows():
        if r["key"] == "cobot":
            lines.append(f"Cobot (palletising robot arm, collaborative): {'running' if r['running'] else 'STOPPED'}"
                         f"{', FAULT' if r['fault'] else ''}, tool speed {r['speed']} mm/s, speed setting "
                         f"{r['override']}%, person in safety zone: {'YES' if r['zone'] else 'no'}")
            continue
        lines.append(f"{r['machine']}: {'running' if r['running'] else 'STOPPED'}"
                     f"{', FAULT' if r['fault'] else ''}, speed {r['speed']} bottles/min, "
                     f"motor {r['current_A']} A, {r['temp_C']} °C, vibration {r['vibration']} mm/s, "
                     f"stopped {r['stops']} times so far")
    lines.append("The cobot slows to 30% collaborative speed whenever a person is in its safety zone; "
                 "that is normal safety behaviour, not a fault.")
    ev = events(df, dec)
    lines.append("History: " + ("; ".join(ev) if ev else "no stops or slowdowns"))
    lines.append("Warnings so far: " + ("; ".join(warns) if warns else "none"))
    for f, t in program_rules(dec).items():
        if t["machine"]:
            lines.append(f"PLC program {f} ({decoder.MACHINE_NAMES[t['machine']]}): "
                         + " ".join(r["english"] for r in t["rules"]))
    return "\n".join(lines)


def spans(df, dec):
    """{machine: {"stopped": [(start, end)], "ran slow": [(start, end)]}} as timestamps."""
    out = {}
    for m in MACHINE_ORDER:
        sig = by_machine(dec).get(m, {})
        if "running" not in sig or "speed" not in sig:
            continue
        run, spd = df[sig["running"]] > 0.5, df[sig["speed"]]
        normal = spd[run].median()
        out[m] = {}
        for label, mask in (("stopped", ~run), ("ran slow", run & (spd < 0.7 * normal))):
            st_ = df.index[mask & ~mask.shift(fill_value=False)]
            en_ = df.index[mask & ~mask.shift(-1, fill_value=False)]
            out[m][label] = [(df["timestamp"][a], df["timestamp"][b]) for a, b in zip(st_, en_) if b - a >= 4]
    return out


def _overlaps(a, b):
    return a[0] <= b[1] and b[0] <= a[1]


LABEL = {"vibration": "Vibration", "motor_current": "Motor current", "motor_temp": "Motor temperature",
         "speed": "Speed", "fill_pressure": "Fill pressure", "cap_torque": "Capping torque",
         "glue_temp": "Glue temperature"}


def alert_cards(df, dec, base=(3, 8), smooth=60):
    """One explained alert per machine: what changed, since when, how far from normal, likely cause, action.
    Severity = how serious it is; confidence = how sure we are it's real (how many signals agree, how long)."""
    names = decoder.MACHINE_NAMES
    if len(df) < base[1] * 60 + smooth:
        return []
    ts = df.set_index("timestamp")
    sm = ts.rolling(smooth, min_periods=1).mean()
    healthy = sm.iloc[base[0] * 60:base[1] * 60]
    mu, sd = healthy.mean(), healthy.std()
    sp, st = spans(df, dec), status(df, dec)
    cards = []
    for i, m in enumerate(MACHINE_ORDER):
        sig = by_machine(dec).get(m, {})
        row = st.iloc[i]
        changed = []
        for s in ("vibration", "motor_current", "motor_temp"):
            if s not in sig:
                continue
            t = sig[s]
            floor = max(sd[t], 0.02 * max(abs(mu[t]), 1))
            z = (sm[t] - mu[t]) / floor
            above = (z > 6) & (ts[sig["running"]] > 0.5) if "running" in sig else (z > 6)
            held = above.rolling(smooth).sum() >= smooth - 5
            if held.any():
                since = held.idxmax()
                now_v = sm[t][held].iloc[-1]
                changed.append({"signal": LABEL[s], "key": s, "since": since, "normal": round(mu[t], 2),
                                "now": round(now_v, 2), "unit": dec[t]["unit"],
                                "change_pct": round((now_v - mu[t]) / abs(mu[t]) * 100, 1), "z": round(z[held].iloc[-1], 1)})
        changed.sort(key=lambda c: c["since"])
        if not changed and not row["fault"]:
            continue
        keys = [c["key"] for c in changed]
        if keys[:1] == ["vibration"] and len(keys) > 1:
            cause = "Vibration rose first, then the motor drew more current and heated up: the typical pattern of a wearing bearing or misalignment."
            action = f"Inspect the {names[m].lower()} motor bearing and alignment; plan a stop before it fails."
        elif "vibration" in keys:
            cause = "Vibration is rising on its own: early sign of bearing wear, looseness or imbalance."
            action = f"Check the {names[m].lower()} for loose parts and bearing noise at the next chance."
        elif "motor_temp" in keys and "motor_current" in keys:
            cause = "The motor is working harder and getting hotter: overload or extra friction."
            action = f"Check the {names[m].lower()} load, lubrication and cooling."
        elif "motor_temp" in keys:
            cause = "The motor is heating up without extra load: possible cooling problem."
            action = "Check the motor fan and cooling path."
        elif "motor_current" in keys:
            cause = "The motor draws more current at the same speed: extra friction or a partial jam."
            action = "Check for jams and lubrication."
        else:
            cause = "The machine raised its own fault signal."
            action = f"Check the {names[m].lower()} fault message on its panel."
        if row["fault"]:
            action = (f"Repair the {names[m].lower()} before restarting: "
                      + action.split(";")[0].replace(f"the {names[m].lower()} ", "its ")[0].lower()
                      + action.split(";")[0].replace(f"the {names[m].lower()} ", "its ")[1:] + "."
                      if changed else action)
            severity = "Critical"
            title = f"{names[m]}: FAULT, line stopped"
        elif len(changed) >= 2 or max(c["z"] for c in changed) > 15:
            severity = "High"
            title = f"{names[m]}: likely to fail soon"
        else:
            severity = "Medium"
            title = f"{names[m]}: drifting from normal"
        mins = (ts.index[-1] - changed[0]["since"]).total_seconds() / 60 if changed else 0
        confidence = "high" if len(changed) >= 2 and mins >= 3 else ("medium" if mins >= 1 else "low")
        cards.append({"machine": names[m], "title": title, "severity": severity, "confidence": confidence,
                      "changed": changed, "cause": cause, "action": action,
                      "first_seen": changed[0]["since"] if changed else None,
                      "status": "FAULT" if row["fault"] else ("Running" if row["running"] else "Stopped")})
    # knock-on effects: machines that slowed only because another machine had a fault
    return cards


def explain_card(c):
    lines = [f"{c['title']}  [{c['severity']} severity, {c['confidence']} confidence]"]
    for ch in c["changed"]:
        lines.append(f"  • {ch['signal']}: normal {ch['normal']} → now {ch['now']} {ch['unit']} "
                     f"({ch['change_pct']:+}%) since {ch['since']:%H:%M}")
    lines.append(f"  Why: {c['cause']}")
    lines.append(f"  Do: {c['action']}")
    return "\n".join(lines)


def _drifting(warns):
    """machine -> [(time, "vibration up 21.9%"), …] from the warnings, oldest first (rises only)."""
    names, out = decoder.MACHINE_NAMES, {}
    for w in warns:
        for part in w[7:].split(", "):
            part = part.split(" (+")[0]
            if " up " in part:
                m = next((m for m in MACHINE_ORDER if part.startswith(names[m])), None)
                if m:
                    out.setdefault(m, []).append((w.split()[0], part[len(names[m]) + 1:]))
    return out


def program_rules(dec):
    """What each machine's own PLC program says, translated with the decoder's names (empty if no programs)."""
    try:
        import translator
        return translator.translate_all(names=dec)
    except Exception:
        return {}


def trip_rule(machine, dec):
    try:
        import translator
        return translator.fault_rules(dec).get(machine)
    except Exception:
        return None


def fault_since(df, dec, m):
    """When the machine's current fault started (the last time its fault flag switched on)."""
    col = by_machine(dec).get(m, {}).get("fault")
    if not col:
        return None
    f = df[col].astype(int)
    on = df.index[(f == 1) & (f.shift(1, fill_value=0) == 0)]
    return df["timestamp"].iloc[on[-1]] if len(on) else None


def rule_answer(question, df, dec, warns):
    """A plain-language answer worked out from the data itself, for when no AI service is reachable."""
    q = question.lower()
    names = decoder.MACHINE_NAMES
    asked = next((m for m in MACHINE_ORDER if names[m].lower() in q or m.replace("_", " ") in q
                  or (m == "cobot" and any(w in q for w in ("robot", "arm", "palletis", "palletiz")))), None)
    sp, st = spans(df, dec), status(df, dec)

    if asked and any(w in q for w in ("slow", "stop", "why", "down", "happen")):
        mine = sp.get(asked, {})
        trouble = sorted(mine.get("ran slow", []) + mine.get("stopped", []))
        if not trouble:
            return f"The {names[asked].lower()} hasn't slowed down or stopped so far.", "signals: speed, running"
        faults = {m: by_machine(dec)[m]["fault"] for m in MACHINE_ORDER if "fault" in by_machine(dec).get(m, {})}
        ts = df.set_index("timestamp")
        causes = {}
        zone = by_machine(dec).get(asked, {}).get("zone_occupied")
        for t in trouble:                       # whose fault light was on during this slowdown?
            win = ts.loc[t[0]:t[1]]
            if zone and win[zone].max() > 0:
                who = "__zone__"                # a person stepped into the robot's safety zone
            else:
                who = next((m for m in MACHINE_ORDER if m in faults and win[faults[m]].max() > 0), None)
            causes.setdefault(who, []).append(t)
        parts = []
        for who, ts_ in causes.items():
            when = ", ".join(f"{t[0]:%H:%M}" for t in ts_[:4]) + (" …" if len(ts_) > 4 else "")
            n = f"{len(ts_)} time{'s' if len(ts_) > 1 else ''}"
            if who == asked:
                own = _drifting(warns).get(asked)
                rule = trip_rule(asked, dec)
                parts.append(f"{n} ({when}) because of its own fault"
                             + (f", warned about first at {own[0][0]} ({own[0][1]})" if own else "")
                             + (f". Its PLC program ({rule[0]}) raises that fault if {rule[1]}" if rule else ""))
            elif who == "__zone__":
                parts.append(f"{n} ({when}) because a person stepped into its safety zone, so it slowed to "
                             "collaborative speed: normal safety behaviour, not a fault")
            elif who:
                rel = "after" if MACHINE_ORDER.index(who) > MACHINE_ORDER.index(asked) else "before"
                item = "cases" if asked == "cobot" else "bottles"
                effect = f"couldn't pass {item} on" if rel == "after" else f"had no {item} coming in"
                parts.append(f"{n} ({when}) because the {names[who].lower()}, which comes {rel} it on the line, "
                             f"had a fault, so it {effect}")
            else:
                parts.append(f"{n} ({when}) with no fault anywhere, so the cause isn't clear from the data")
        return (f"The {names[asked].lower()} slowed or stopped " + "; and ".join(parts) + ".",
                f"signals: {names[asked]} speed and running, fault flags of every machine")

    if asked and any(w in q for w in ("program", "logic", "code", "plc", "trip", "rule", "when does", "control")):
        progs = program_rules(dec)
        mine = next(((f, t) for f, t in progs.items() if t["machine"] == asked), None)
        if mine:
            f, t = mine
            return (f"The {names[asked].lower()}'s PLC program ({f}) says: " + " ".join(r["english"] for r in t["rules"]),
                    f"source: {f}, translated with the decoded tag names")

    drifting = _drifting(warns)
    faults_now = [m for m in MACHINE_ORDER if st.loc[MACHINE_ORDER.index(m), "fault"]]

    if not asked and "line" in q and any(w in q for w in ("stop", "down", "why", "happen")):
        if faults_now:
            m = faults_now[0]
            early = drifting.get(m)
            rule = trip_rule(m, dec)
            since = fault_since(df, dec, m)
            gap = ""
            if early and since is not None:
                try:
                    h, mi = map(int, early[0][0].split(":")[:2])
                    gap = f", {since.hour * 60 + since.minute - (h * 60 + mi)} minutes before the stop"
                except ValueError:
                    pass
            text = (f"The line stopped at {since:%H:%M}" if since is not None else "The line stopped")
            text += f" because the {names[m].lower()} has a fault, and every other machine waits for it."
            if early:
                text += f" It was warned about first at {early[0][0]} ({early[0][1]}){gap}."
            if rule:
                text += f" Its PLC program ({rule[0]}) raises that fault if {rule[1]}."
            return text, f"signals: fault flags, {names[m]} running and speed"
        stopped = [names[m].lower() for m in MACHINE_ORDER if not st.loc[MACHINE_ORDER.index(m), "running"]]
        if stopped:
            return f"Right now these are stopped: {', '.join(stopped)}, but no fault light is on.", "signals: running, fault"
        return "The whole line is running right now.", "signals: running"

    if any(w in q for w in ("attention", "fail", "wrong", "problem", "risk", "worry", "check", "break")):
        if faults_now:
            m = faults_now[0]
            early = drifting.get(m)
            since = fault_since(df, dec, m)
            return (f"The {names[m].lower()} is in FAULT now" + (f" (since {since:%H:%M})." if since is not None else ".") +
                    (f" It was warned about first at {early[0][0]}: {early[0][1]}." if early else ""),
                    "signals: fault, vibration, motor current, motor temperature")
        if drifting:
            m = max(drifting, key=lambda m: len(drifting[m]))
            steps = "; then ".join(f"{what} (since {t})" for t, what in drifting[m])
            return (f"The {names[m].lower()} needs attention: {steps}. Vibration rising first and then the motor "
                    "heating up is a typical sign of a wearing bearing, so check it before it fails.",
                    f"signals: {names[m]} vibration, motor current, motor temperature")
        return "Nothing needs attention: no machine is drifting away from normal.", "signals: all"

    if asked == "cobot":
        r = st[st["key"] == "cobot"].iloc[0]
        visits = len(sp.get("cobot", {}).get("ran slow", []))
        state = "in FAULT" if r["fault"] else ("running" if r["running"] else "stopped")
        return (f"The cobot is {state}: tool speed {r['speed']} mm/s at a {r['override']}% speed setting"
                f"{', a person is in its safety zone right now' if r['zone'] else ''}. It has slowed for people "
                f"{visits} time{'s' if visits != 1 else ''} so far, which is normal collaborative behaviour.",
                "signals: Cobot running, fault, tool speed, speed setting, safety zone")
    if asked:
        r = st[st["machine"] == names[asked]].iloc[0]
        state = "in FAULT" if r["fault"] else ("running" if r["running"] else "stopped")
        return (f"The {names[asked].lower()} is {state}: {r['speed']} bottles/min, {r['current_A']} A, "
                f"{r['temp_C']} °C, vibration {r['vibration']} mm/s. It has stopped {r['stops']} times so far.",
                "signals: running, fault, speed, motor current, motor temperature, vibration")
    return ("I can only answer from the line's own data. Ask about a machine (filler, capper…), why it slowed "
            "down, why the line stopped, or what needs attention.", "")


def ask(question, df, dec, warns):
    """Answer a plant question from the data. Groq first, Gemini second, our own rules if both fail."""
    context = facts(df, dec, warns)
    analysis, _ = rule_answer(question, df, dec, warns)
    cards = "\n".join(explain_card(c) for c in alert_cards(df, dec)) or "no active alerts"
    prompt = ("You are Talking Machines, an assistant for a bottling-line maintenance engineer. "
              "Answer ONLY from the facts below, in 2-3 short sentences, then list the signals you used "
              "as 'Signals used: ...'. If the facts don't answer it, say so.\n"
              "RULES: The ANALYSIS section was computed from the data and is correct about causes: base your "
              "answer on it. When a machine stops, its speed, current, vibration and temperature drop as a "
              "result; those drops are effects, not problems. A machine that slowed while another machine had "
              "a fault was blocked or starved by that machine. Do not invent causes. Always keep every time "
              "(HH:MM) and every 'warned about first at' time from the ANALYSIS in your answer, and say how many "
              "minutes of warning there were when the ANALYSIS gives it.\n\n"
              f"ANALYSIS:\n{analysis}\n\nALERTS:\n{cards}\n\nFACTS:\n{context}\n\nQUESTION: {question}")
    errors = []
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception as e:
        errors.append(f"dotenv: {e}")
    try:
        from groq import Groq
        r = Groq(api_key=os.environ["GROQ_API_KEY"]).chat.completions.create(
            model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            messages=[{"role": "user", "content": prompt}], timeout=20)
        return r.choices[0].message.content.strip(), "Groq"
    except Exception as e:
        errors.append(f"Groq: {type(e).__name__}: {str(e)[:150]}")
    try:
        from google import genai
        r = genai.Client(api_key=os.environ["GEMINI_API_KEY"]).models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"), contents=prompt)
        return r.text.strip(), "Gemini"
    except Exception as e:
        errors.append(f"Gemini: {type(e).__name__}: {str(e)[:150]}")
    text, used = rule_answer(question, df, dec, warns)
    return text + (f"\n\n_{used}_" if used else ""), "offline rules  ·  " + "  |  ".join(errors)


if __name__ == "__main__":
    df, dec = load()
    w = warning_lines("data/stream.csv", dec)
    print(status(df, dec).to_string(index=False))
    print("\n".join(w))
    ans, src = ask("Why did the filler slow down?", df, dec, w)
    print(f"\n[{src}] {ans[:600]}")