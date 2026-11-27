"""Talking Machines — shared logic for the dashboard: line status, warnings and Ask the Line."""
import os

import pandas as pd

import decoder
import warning

MACHINE_ORDER = ["depalletiser", "filler", "capper", "labeller", "case_packer"]


SNAP = "data/_snapshot.csv"


def load(path="data/stream.csv"):
    """Read the stream safely while the simulator is still writing it (the last line may be half-written)."""
    df = pd.read_csv(path, parse_dates=["timestamp"], on_bad_lines="skip").dropna().reset_index(drop=True)
    df.to_csv(SNAP, index=False)
    dec, _ = decoder.decode(SNAP)
    return df, dec


def by_machine(dec):
    """{machine: {signal: tag}}"""
    out = {}
    for tag, r in dec.items():
        out.setdefault(r["machine"], {})[r["signal"]] = tag
    return out


def status(df, dec):
    """Latest state of every machine, in line order."""
    rows = []
    last = df.iloc[-1]
    for m in MACHINE_ORDER:
        sig = by_machine(dec).get(m, {})
        get = lambda s: (last[sig[s]] if s in sig else None)
        stops = int((df[sig["running"]].diff() == -1).sum()) if "running" in sig else 0
        rows.append({"machine": decoder.MACHINE_NAMES[m], "running": bool(get("running")),
                     "fault": bool(get("fault")), "speed": get("speed"), "current_A": get("motor_current"),
                     "temp_C": get("motor_temp"), "vibration": get("vibration"), "stops": stops})
    return pd.DataFrame(rows)


def warning_lines(path, dec, window_s=180, top=3):
    """The same grouped warnings as warning.py prints, returned as text lines."""
    names = {t: r["name"] for t, r in dec.items()}
    alerts = warning.drift_warnings(SNAP if os.path.exists(SNAP) else path, names=names)
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
        lines.append(f"{r['machine']}: {'running' if r['running'] else 'STOPPED'}"
                     f"{', FAULT' if r['fault'] else ''}, speed {r['speed']} bottles/min, "
                     f"motor {r['current_A']} A, {r['temp_C']} °C, vibration {r['vibration']} mm/s, "
                     f"stopped {r['stops']} times so far")
    ev = events(df, dec)
    lines.append("History: " + ("; ".join(ev) if ev else "no stops or slowdowns"))
    lines.append("Warnings so far: " + ("; ".join(warns) if warns else "none"))
    return "\n".join(lines)


def ask(question, df, dec, warns):
    """Answer a plant question from the data. Groq first, Gemini second, a plain summary if both fail."""
    context = facts(df, dec, warns)
    prompt = ("You are Talking Machines, an assistant for a bottling-line maintenance engineer. "
              "Answer ONLY from the facts below, in 2-3 short sentences, then list the signals you used "
              "as 'Signals used: ...'. If the facts don't answer it, say so.\n\n"
              f"FACTS:\n{context}\n\nQUESTION: {question}")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    try:
        from groq import Groq
        r = Groq(api_key=os.environ["GROQ_API_KEY"]).chat.completions.create(
            model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            messages=[{"role": "user", "content": prompt}], timeout=20)
        return r.choices[0].message.content.strip(), "Groq"
    except Exception:
        pass
    try:
        from google import genai
        r = genai.Client(api_key=os.environ["GEMINI_API_KEY"]).models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"), contents=prompt)
        return r.text.strip(), "Gemini"
    except Exception:
        pass
    return "I can't reach the AI right now, so here are the raw facts:\n\n" + context, "offline"


if __name__ == "__main__":
    df, dec = load()
    w = warning_lines("data/stream.csv", dec)
    print(status(df, dec).to_string(index=False))
    print("\n".join(w))
    ans, src = ask("Why did the filler slow down?", df, dec, w)
    print(f"\n[{src}] {ans[:600]}")