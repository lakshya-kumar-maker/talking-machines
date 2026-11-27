"""Talking Machines — dashboard.   Run:  python -m streamlit run app.py
The simulated line starts by itself from 09:00. By default it runs as a PLC on Modbus TCP and a collector
reads it (read-only), exactly like a real plant; the sidebar can switch to the plain file mode.
"""

import atexit
import os
import subprocess
import sys
import time

import altair as alt
import pandas as pd
import streamlit as st

import line

PATH = "data/stream.csv"
MIN_ROWS = 180  # the decoder needs ~3 simulated minutes before names mean anything
RUN_MINUTES = 45
CHART_WINDOW_S = 15 * 60  # the live charts show the last 15 simulated minutes
REFRESH = 3  # seconds between live updates

st.set_page_config(page_title="Talking Machines", page_icon="🏭", layout="wide")
st.markdown(
    """<style>
.stale-element, [data-stale="true"] {opacity: 1 !important; filter: none !important; transition: none !important;}
[data-testid="stStatusWidget"] {visibility: hidden;}
</style>""",
    unsafe_allow_html=True,
)


# ── the simulated line, running in the background ─────────────────────────
LOGS = {"proc": "data/simulator.log", "collector": "data/collector.log"}


@st.cache_resource
def line_process():
    """The simulated PLC (and the collector reading it), kept between page refreshes."""
    atexit.register(stop_line)  # once per server, not once per script rerun
    return {"proc": None, "collector": None, "speed": 20, "seed": 7, "mode": "PLC (Modbus TCP)"}


def _spawn(key, args):
    """Start a helper process; its output goes to a log file so a crash can be shown on the page."""
    log = open(LOGS[key], "w", encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    line_process()[key] = subprocess.Popen([sys.executable] + args, stdout=log, stderr=subprocess.STDOUT, env=env)
    log.close()  # the child keeps its own handle


OPENPLC_MAP = "data/openplc/map.json"
OPENPLC_DEFAULT = "127.0.0.1:502"  # where OpenPLC Runtime listens unless the sidebar says otherwise
OPENPLC_MAX_SPEED = 10  # OpenPLC's timers run in real time: faster plants make jams too short for its 2 s timer


def openplc_seed():
    """Seed the exported OpenPLC program was built for, or None if none is exported."""
    try:
        import json

        with open(OPENPLC_MAP, encoding="utf-8") as f:
            return json.load(f)["seed"]
    except (OSError, ValueError, KeyError):
        return None


def start_line(speed, seed, mode):
    """(Re)start the simulated line from 09:00 in the chosen mode, clearing the old run's files."""
    lp = line_process()
    stop_line()
    os.makedirs("data", exist_ok=True)
    for f in ("data/stream.csv", "data/plc_map.json", "data/inject.json"):
        if os.path.exists(f):
            os.remove(f)
    args = ["simulator.py", "--speed", str(speed), "--seed", str(seed), "--minutes", str(RUN_MINUTES)]
    if mode.startswith("OpenPLC"):
        # the real soft PLC runs our program; plant.py plays the machines, the collector reads the PLC
        host, _, port = lp.get("openplc", OPENPLC_DEFAULT).partition(":")
        if not os.path.exists(OPENPLC_MAP):
            import openplc_export

            openplc_export.export(seed)
        _spawn(
            "proc",
            [
                "plant.py",
                "--host",
                host,
                "--port",
                port or "502",
                "--speed",
                str(min(speed, OPENPLC_MAX_SPEED)),
                "--minutes",
                str(RUN_MINUTES),
            ],
        )
        _spawn(
            "collector", ["collector.py", "--host", host, "--port", port or "502", "--map", OPENPLC_MAP, "--rate", "4"]
        )
        speed = min(speed, OPENPLC_MAX_SPEED)
    elif mode.startswith("PLC"):
        _spawn("proc", args + ["--plc"])
        _spawn("collector", ["collector.py", "--rate", str(max(50, speed * 3))])
    else:
        _spawn("proc", args)
    lp["speed"], lp["seed"], lp["mode"] = speed, seed, mode
    time.sleep(1.0)  # let the first rows arrive


def stop_line():
    """Stop the simulator/plant and the collector, if they are running."""
    lp = line_process()
    for key in ("collector", "proc"):
        p = lp.get(key)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        lp[key] = None


def crashed():
    """[(name, last log lines)] for helper processes that exited with an error."""
    out = []
    for key, name in (("proc", "simulator"), ("collector", "collector")):
        p = line_process().get(key)
        if p is not None and p.poll() not in (None, 0):
            try:
                with open(LOGS[key], encoding="utf-8", errors="replace") as f:
                    tail = "".join(f.readlines()[-8:])
            except OSError:
                tail = "(no log)"
            out.append((name, tail))
    return out


if line_process()["proc"] is None and not line_process().get("stopped"):  # first run: start from 09:00
    start_line(line_process()["speed"], line_process()["seed"], line_process()["mode"])

with st.sidebar:
    st.header("Simulated line")
    lp = line_process()
    speed = st.select_slider("Speed (simulated seconds per real second)", [5, 10, 20, 30, 60], value=lp["speed"])
    seed = st.number_input("Seed (changes tag names and noise)", value=lp["seed"], step=1)
    modes = ["PLC (Modbus TCP)", "OpenPLC (real soft PLC)", "File only"]
    mode = st.radio(
        "How Talking Machines reads the line",
        modes,
        index=modes.index(lp["mode"]),
        help="PLC: the simulator acts as a PLC on Modbus TCP port 5020 and a collector reads it, "
        "read-only, like in a real plant. OpenPLC: OpenPLC Runtime runs our PLC programs and "
        "plant.py plays the machines; see OPENPLC_SETUP.md. File: the simulator writes the "
        "data file directly.",
    )
    if mode.startswith("OpenPLC"):
        lp["openplc"] = st.text_input("OpenPLC address (host:port)", value=lp.get("openplc", OPENPLC_DEFAULT))
        exported = openplc_seed()
        if exported is None:
            st.caption("No OpenPLC program exported yet.")
        elif exported != int(seed):
            st.warning(
                f"OpenPLC runs the program exported for seed {exported}. Seed {int(seed)} has different "
                "tag names: export it and upload it to OpenPLC again, or set the seed back."
            )
        if st.button("Export OpenPLC program for this seed", use_container_width=True):
            import openplc_export

            openplc_export.export(int(seed))
            st.success(
                f"Wrote data/openplc/talking_machines.st for seed {int(seed)}. Upload it in OpenPLC "
                "(Programs → Upload Program) and press Start PLC, then ▶ Restart line."
            )
        if speed > OPENPLC_MAX_SPEED:
            st.caption(
                f"With OpenPLC the plant runs at {OPENPLC_MAX_SPEED}× at most: the PLC's timers count real seconds."
            )
    if st.button("▶ Restart line from 09:00", use_container_width=True):
        lp["stopped"] = False
        st.session_state.pop("inject_log", None)
        start_line(speed, int(seed), mode)
        st.rerun()
    if st.button("■ Stop line", use_container_width=True):
        lp["stopped"] = True
        stop_line()
    alive = lambda p: p is not None and p.poll() is None
    running = alive(lp["proc"]) and (alive(lp["collector"]) or lp["mode"] == "File only")
    st.caption(("🟢 running" if running else "⚪ stopped") + f" · {lp['speed']}× speed · seed {lp['seed']}")
    if lp["mode"].startswith("PLC"):
        st.caption("Source: PLC at 127.0.0.1:5020 · Modbus TCP · read-only (writes are refused)")
    elif lp["mode"].startswith("OpenPLC"):
        st.caption(
            f"Source: OpenPLC at {lp.get('openplc', OPENPLC_DEFAULT)} · Modbus TCP · Talking Machines only "
            "reads; faults are raised by OpenPLC running our Structured Text"
        )
    line.PRICE = float(
        st.number_input(
            "Value of one bottle (₹)",
            min_value=1.0,
            value=line.PRICE,
            step=1.0,
            help="Used to turn lost output into rupees. Change it to your plant's figure.",
        )
    )
    st.caption(
        f"A full run is {RUN_MINUTES} simulated minutes: about "
        f"{RUN_MINUTES * 60 / lp['speed'] / 60:.1f} real minutes at this speed."
    )

st.title("🏭 Talking Machines")
st.caption("Bottling line · read-only: it watches the machines and never controls them")


# ── reading the data ───────────────────────────────────────────────────────
@st.cache_data(show_spinner=False, max_entries=3)
def snapshot(bucket):
    """Load the data file and decode it; `bucket` changes every REFRESH seconds to force a reload."""
    df, dec = line.load(PATH)
    warns = line.warning_lines(df, dec) if len(df) >= MIN_ROWS else []
    return df, dec, warns


def current():
    """Return (df, decoded tags, warnings) for the line right now, or None while there is no data."""
    if not os.path.exists(PATH):
        return None
    try:
        # reload every few seconds; Windows doesn't update the file's time while the simulator writes it
        return snapshot(int(time.time() // REFRESH))
    except Exception as e:  # file being rewritten: try again on the next refresh
        st.info(f"Reading the line… ({e.__class__.__name__})")
        return None


# ── live demo: let a judge break a machine ─────────────────────────────────
INJECT_MIN = 8  # the system learns "normal" from minutes 3-8, so faults can be added after that

with st.sidebar:
    st.divider()
    st.header("🔧 Break a machine")
    st.caption(
        "For a live demo: pick a fault and watch Talking Machines catch it. "
        "Each one follows a real rule in that machine's PLC program."
    )
    import simulator

    what = st.selectbox("Fault", list(simulator.INJECTABLE), format_func=simulator.INJECTABLE.get, key="inject_what")
    _data = current()
    _minute = (
        (_data[0]["timestamp"].iloc[-1] - _data[0]["timestamp"].iloc[0]).total_seconds() / 60
        if _data is not None and len(_data[0])
        else 0
    )
    _lp = line_process()
    _live = _lp["proc"] is not None and _lp["proc"].poll() is None
    if not _live:
        st.caption("Start the line first (▶ Restart line).")
    elif _minute < INJECT_MIN and what in ("filler_wear", "glue_heater"):
        st.caption(f"Available from 09:{INJECT_MIN:02d}: the system is still learning what normal looks like.")
    elif st.button("💥 Break it now", use_container_width=True, type="primary"):
        st.session_state["inject_id"] = st.session_state.get("inject_id", 0) + 1
        import json

        with open("data/inject.json.tmp", "w", encoding="utf-8") as f:
            json.dump({"id": f"{time.time():.0f}-{st.session_state['inject_id']}", "what": what}, f)
        os.replace("data/inject.json.tmp", "data/inject.json")
        st.session_state["inject_log"] = st.session_state.get("inject_log", []) + [
            f"{_data[0]['timestamp'].iloc[-1]:%H:%M} · {simulator.INJECTABLE[what].split(' (')[0]}"
        ]
        st.toast("Fault injected. Watch the Live line tab: the warning comes first, then the stop.", icon="💥")
    for item in reversed(st.session_state.get("inject_log", [])[-4:]):
        st.caption("💥 " + item)


def line_anomaly_flag():
    """Return the Isolation Forest score above which a machine counts as unusual."""
    import anomaly

    return anomaly.FLAG


def clock(df):
    """Draw the line-time metric and the shift progress bar."""
    now = df["timestamp"].iloc[-1]
    start = df["timestamp"].iloc[0]
    done = (now - start).total_seconds() / (RUN_MINUTES * 60)
    lp = line_process()
    live_now = lp["proc"] is not None and lp["proc"].poll() is None
    c1, c2 = st.columns([1, 3])
    c1.metric("🕘 Line time", f"{now:%H:%M:%S}")
    with c2:
        st.write("")
        st.progress(min(done, 1.0), text=f"Shift progress · {'live' if live_now else 'stopped'} · {lp['speed']}× speed")


tab_live, tab_work, tab_tags, tab_plc, tab_map, tab_ask = st.tabs(
    ["Live line", "Work Orders", "Tag Translator", "PLC Logic", "Line Map", "Ask the Line"]
)


@st.fragment(run_every=f"{REFRESH}s")
def live():
    """Draw the Live line tab: machine cards, forecasts, downtime cost, alerts, forest and charts."""
    for name, tail in crashed():
        st.error(f"The {name} stopped with an error. Last lines of its log:")
        st.code(tail or "(empty)", language=None)
    data = current()
    if data is None:
        st.warning("Starting the line… (if this stays, press ▶ Restart line in the sidebar)")
        return
    df, dec, warns = data
    clock(df)
    if len(df) < MIN_ROWS:
        st.info(f"Listening… {len(df)} seconds of data so far. Names appear after about 3 simulated minutes.")
        return

    # one card per machine, in line order
    stat = line.status(df, dec)
    culprit = stat.loc[stat["fault"], "machine"].tolist()
    cols = st.columns(len(stat))
    for col, (_, r) in zip(cols, stat.iterrows()):
        with col:
            if r["fault"]:
                st.error(f"**{r['machine']}**  \nFAULT")
            elif not r["running"]:
                why = f"Stopped · waiting on {culprit[0]}" if culprit else "Stopped"
                st.warning(f"**{r['machine']}**  \n{why}")
            elif r["key"] == "cobot" and r["zone"]:
                st.info(f"**{r['machine']}**  \nSlowed · person nearby")
            else:
                st.success(f"**{r['machine']}**  \nRunning")
            st.metric(f"Speed ({r['unit']})", f"{r['speed']:.0f}" if r["speed"] is not None else "–")
            if r["key"] == "cobot":
                st.caption(
                    f"speed setting {r['override']:.0f}% · "
                    + ("🧍 person in safety zone" if r["zone"] else "safety zone clear")
                )
            else:
                st.caption(f"{r['current_A']} A · {r['temp_C']} °C · vib {r['vibration']} mm/s")

    # the single most important thing to do (details in the Work Orders tab)
    try:
        import maintenance

        orders = line.work_orders(df, dec)
        if orders and orders[0]["when"][0] in "NB":
            st.warning("🛠 " + maintenance.summary(orders[0], line.rupees) + "  → see **Work Orders**")
    except Exception as e:
        st.caption(f"(work orders unavailable: {type(e).__name__}: {e})")

    # time until failure: the drifting signal's trend, extended to the limit in the machine's PLC program
    preds = line.predictions(df, dec)
    if line.PRED_ERROR:
        st.error(
            f"The time-to-failure forecast couldn't run: {line.PRED_ERROR}. Check that prediction.py and "
            "translator.py are in the project folder."
        )
    if preds:
        import prediction

        st.subheader("⏱ Time until failure")
        for p in preds[:2]:
            names = line.decoder.MACHINE_NAMES
            left = max(p["minutes"], 0)
            st.error(
                f"**{names[p['machine']]} predicted to trip at about {p['at']:%H:%M}** "
                f"(in ~{left:.0f} min · likely between {p['low_at']:%H:%M} and {p['high_at']:%H:%M})"
            )
            hist = df[["timestamp", p["tag"]]].tail(CHART_WINDOW_S).copy()
            hist["value"] = hist[p["tag"]].rolling(60, min_periods=1).mean()
            hist["Line"] = "Measured (1-min average)"
            fut = prediction.curve(p)
            fut = fut[fut["timestamp"] >= hist["timestamp"].iloc[0]]
            fut["Line"] = "Forecast"
            both = pd.concat([hist[["timestamp", "value", "Line"]], fut])
            colors = alt.Scale(domain=["Measured (1-min average)", "Forecast"])
            lines_ = (
                alt.Chart(both)
                .mark_line()
                .encode(
                    x=alt.X("timestamp:T", title=None),
                    y=alt.Y("value:Q", title=f"{p['name']} ({p['unit']})"),
                    color=alt.Color("Line:N", scale=colors, legend=alt.Legend(orient="bottom", title=None)),
                    strokeDash=alt.condition(alt.datum.Line == "Forecast", alt.value([6, 4]), alt.value([1, 0])),
                )
            )
            limit = alt.Chart(pd.DataFrame({"y": [p["limit"]], "t": [f"PLC trip limit {p['limit']:g} {p['unit']}"]}))
            rule = limit.mark_rule(color="#e5484d").encode(y="y:Q")
            label = limit.mark_text(align="left", dx=4, dy=-6, color="#e5484d").encode(
                y="y:Q", x=alt.value(0), text="t:N"
            )
            st.altair_chart((lines_ + rule + label).properties(height=230), use_container_width=True)
            word = "above" if p["op"] in (">", ">=") else "below"
            hold = f" for {p['hold_s']:g} s" if p["hold_s"] else ""
            cost = (
                line.rupees(round(line.downtime(df, dec, line.PRICE)["rated"], -1) * line.PRICE)
                if line.downtime(df, dec, line.PRICE)
                else None
            )
            st.caption(
                f"Now {p['now_value']:.1f} {p['unit']} (normal {p['normal']:.1f}). The limit comes from the "
                f"machine's own PLC program ({p['file']}: trips if {p['name'].lower()} is {word} "
                f"{p['limit']:g} {p['unit']}{hold}). Trend fitted on the drift so far; the range comes from "
                f"fitting a curve and a straight line on different parts of it."
                + (f" Once it trips, the line loses about {cost} per minute." if cost else "")
            )

    # what the stoppages have cost so far
    d = line.downtime(df, dec, line.PRICE)
    if d:
        st.subheader("Cost of downtime")
        c1, c2, c3 = st.columns(3)
        c1.metric("Lost today", line.rupees(d["rupees"]))
        c2.metric("Bottles not made", f"{d['bottles']:,.0f}")
        mins = sum(v["minutes"] for v in d["by_cause"].values())
        c3.metric("Time below full speed", f"{mins:.0f} min")
        if d["events"]:
            names = line.decoder.MACHINE_NAMES
            table = pd.DataFrame(
                [
                    {
                        "From": f"{e['start']:%H:%M}",
                        "Cause": names.get(e["cause"], "Unexplained") + (" fault" if e["cause"] in names else ""),
                        "Minutes": round(e["minutes"], 1),
                        "Bottles lost": round(e["bottles"]),
                        "Cost": line.rupees(e["rupees"]) + (" and rising" if e["ongoing"] else ""),
                    }
                    for e in reversed(d["events"])
                ]
            )
            st.dataframe(table, hide_index=True, use_container_width=True)
        saved = [x for x in line.cost_text(d, warns).split(". ") if "could have been planned" in x]
        if saved:
            st.warning("💡 " + saved[0].strip().rstrip(".") + ".")
        st.caption(
            f"Lost bottles = normal speed ({d['rated']:.0f}/min) minus actual output at the case packer, "
            f"each second; the cause is the first machine on the line whose fault light was on. "
            f"Valued at {line.rupees(d['price'])} per bottle (change it in the sidebar)."
        )

    st.subheader("Why this alert")
    cards = line.alert_cards(df, dec)
    if not cards:
        st.success("No alerts: every machine looks normal.")
    for c in cards:
        with st.container(border=True):
            badge = {"Critical": "🔴", "High": "🟠", "Medium": "🟡"}[c["severity"]]
            conf = f"**{c['confidence_pct']}%** confident" if "confidence_pct" in c else f"{c['confidence']} confidence"
            st.markdown(
                f"**{badge} {c['title']}**  ·  {c['severity']} severity  ·  {conf}",
                help=(
                    "How sure we are this is a real developing fault, not a harmless look-alike "
                    "(heavier batch, forklift bump, warm hall). " + c.get("confidence_note", "")
                ).strip(),
            )
            if c.get("confidence_note") and c["status"] != "FAULT":
                st.caption("🎯 " + c["confidence_note"][0].upper() + c["confidence_note"][1:])
            if c["changed"]:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Signal": ch["signal"],
                                "Normal": ch["normal"],
                                "Now": ch["now"],
                                "Unit": ch["unit"],
                                "Change": f"{ch['change_pct']:+}%",
                                "Since": f"{ch['since']:%H:%M}",
                            }
                            for ch in c["changed"]
                        ]
                    ),
                    hide_index=True,
                    use_container_width=True,
                )
            if c.get("forest"):
                f = c["forest"]
                st.caption(
                    f"🌲 Isolation Forest {'agrees' if c['changed'] else 'noticed it first'}: all its signals "
                    f"together look unusual since {f['since']:%H:%M} (score up to {f['score']:.2f}), "
                    f"mainly {f['why']}."
                )
            st.markdown(f"**Why:** {c['cause']}  \n**Do:** {c['action']}")
    with st.expander("All warnings, newest first"):
        for w in reversed(warns):
            st.write("⚠ " + w)

    # second opinion: an Isolation Forest looks at each machine's signals together
    scores, found = line.forest(df, dec)
    st.subheader("Second opinion: Isolation Forest")
    if line.FOREST_ERROR:
        st.error(
            f"The Isolation Forest couldn't run: {line.FOREST_ERROR}. Check that anomaly.py is in the "
            "project folder and scikit-learn is installed (pip install scikit-learn)."
        )
    elif not scores:
        st.info("The Isolation Forest starts scoring after 09:08, once it has learned what normal looks like.")
    if scores:
        st.caption(
            "Learns what each machine's signals look like together while healthy (09:03–09:08), then scores "
            "every second: 0 = normal, 1 = unlike anything seen while healthy. It is not scored while a "
            f"machine is stopped or slowed by another one. Above {line_anomaly_flag()} for 30 s = unusual."
        )
        names = line.decoder.MACHINE_NAMES
        recent = pd.DataFrame({names[m]: s_["score"] for m, s_ in scores.items()}).assign(timestamp=df["timestamp"])
        recent = recent.tail(CHART_WINDOW_S).melt("timestamp", var_name="Machine", value_name="Score").dropna()
        base = (
            alt.Chart(recent)
            .mark_line()
            .encode(
                x=alt.X("timestamp:T", title=None),
                y=alt.Y("Score:Q", title=None, scale=alt.Scale(domain=[0, 1])),
                color=alt.Color("Machine:N", legend=alt.Legend(orient="bottom", title=None)),
            )
        )
        rule = alt.Chart(pd.DataFrame({"y": [line_anomaly_flag()]})).mark_rule(strokeDash=[4, 4]).encode(y="y:Q")
        st.altair_chart((base + rule).properties(height=200), use_container_width=True)
        if not found:
            st.success("🌲 Nothing unusual: every machine's signals together look like normal.")
        for f in found:
            first_drift = next(
                (w[:5] for w in warns if w[7:].startswith(names[f["machine"]]) and (" up " in w or "glue" in w)), None
            )
            compare = ""
            if first_drift:
                gap = (int(first_drift[:2]) * 60 + int(first_drift[3:5])) - (f["since"].hour * 60 + f["since"].minute)
                compare = (
                    f" · {gap} min before the single-signal warning ({first_drift})"
                    if gap > 0
                    else " · at the same time as the single-signal warning"
                    if gap == 0
                    else f" · {-gap} min after the single-signal warning ({first_drift})"
                )
            st.warning(
                f"🌲 **{names[f['machine']]}** unusual since **{f['since']:%H:%M}**, mainly {f['why']} "
                f"(score up to {f['score']:.2f}){compare}"
            )

    st.subheader("Signals")
    machines = line.by_machine(dec)
    labels = {line.decoder.MACHINE_NAMES.get(m, m): m for m in line.MACHINE_ORDER if m in machines}
    pick = st.selectbox("Machine", list(labels), index=1 if len(labels) > 1 else 0, key="pick_machine")
    sig = machines[labels[pick]]
    wanted = [s for s in ("motor_current", "motor_temp", "vibration") if s in sig] or [
        s for s in ("j2_torque", "j3_torque", "j1_torque") if s in sig
    ]
    recent = df.tail(CHART_WINDOW_S).set_index("timestamp")

    def short(name):  # "Cobot joint 1 (base) torque" -> "Joint 1 (base) torque"
        return name[len(pick) :].strip().capitalize() if name.lower().startswith(pick.lower() + " ") else name

    chart = recent[[sig[s] for s in wanted]].rename(columns={sig[s]: short(dec[sig[s]]["name"]) for s in wanted})
    long = chart.reset_index().melt("timestamp", var_name="Signal", value_name="Value")
    st.altair_chart(
        alt.Chart(long)
        .mark_line()
        .encode(
            x=alt.X("timestamp:T", title=None),
            y=alt.Y("Value:Q", title=None),
            color=alt.Color(
                "Signal:N",
                sort=list(chart.columns),
                legend=alt.Legend(orient="bottom", direction="vertical", title=None, labelLimit=0),
            ),
        )
        .properties(height=280),
        use_container_width=True,
    )
    if "speed" in sig:
        st.line_chart(recent[[sig["speed"]]].rename(columns={sig["speed"]: dec[sig["speed"]]["name"]}), height=160)


with tab_live:
    live()


TAB_REFRESH = 10  # the side tabs update by themselves every few seconds


@st.fragment(run_every=f"{TAB_REFRESH}s")
def tag_translator():
    """Draw the Tag Translator tab: every decoded tag with its meaning and latest value."""
    data = current()
    if data is None or len(data[0]) < MIN_ROWS:
        n = 0 if data is None else len(data[0])
        st.info(
            f"The Tag Translator needs about 3 simulated minutes of data ({n} of {MIN_ROWS} seconds so far). "
            "This page updates by itself."
        )
        return
    df, dec, _ = data
    last = df.iloc[-1]
    table = pd.DataFrame(
        [
            {
                "Tag": t,
                "Means": r["name"],
                "Unit": r["unit"],
                "Confidence": r["confidence"],
                "Latest value": last[t],
                "Why": r["reason"],
            }
            for t, r in dec.items()
        ]
    )
    table = table.sort_values(["Means"]).reset_index(drop=True)
    low = int((table["Confidence"] == "low").sum())
    st.write(
        f"**{len(table)} signals decoded from how they behave** · {low} marked low confidence "
        f"· as of {df['timestamp'].iloc[-1]:%H:%M:%S}"
    )
    st.dataframe(table, use_container_width=True, hide_index=True, height=560)


@st.fragment(run_every=f"{TAB_REFRESH}s")
def plc_logic():
    """Draw the PLC Logic tab: each PLC program translated rule by rule into English."""
    try:
        import translator
    except Exception as e:
        st.error(f"translator.py is missing or broken: {e}")
        return
    data = current()
    folder = "data/plc_programs"
    files = sorted(f for f in os.listdir(folder) if f.endswith(".st")) if os.path.isdir(folder) else []
    if not files:
        st.warning(
            "No PLC programs found in data/plc_programs. Check that plc_programs.py is in the project "
            "folder, then press ▶ Restart line in the sidebar."
        )
        return
    if data is None or len(data[0]) < MIN_ROWS:
        n = 0 if data is None else len(data[0])
        st.info(
            f"The PLC Logic translator needs the decoded tag names: about 3 simulated minutes of data "
            f"({n} of {MIN_ROWS} seconds so far). This page updates by itself."
        )
        return
    dec = data[1]
    try:
        progs = translator.translate_all(folder, dec)
    except Exception as e:
        st.error(f"Couldn't translate the programs: {type(e).__name__}: {e}")
        return
    label = lambda f: f"{f}  →  {line.decoder.MACHINE_NAMES.get(progs[f]['machine'], 'unknown machine')}"
    st.write(
        "Each machine's own control program, read straight from the PLC export. The code uses the plant's "
        "cryptic tags and has no comments; we translate it rule by rule using the decoded names."
    )
    pick = st.selectbox("Program", files, format_func=label, key="pick_program")
    t = progs[pick]
    for n, r in enumerate(t["rules"], 1):
        left, right = st.columns([1, 1])
        left.code(r["code"], language="pascal")
        right.markdown(f"**Rule {n}.** {r['english']}")
    if t["unknown"]:
        st.caption("Not decoded (internal flags with no live data): " + ", ".join(t["unknown"]))
    summaries = st.session_state.setdefault("plc_summaries", {})
    if st.button("Summarise this program with AI"):
        with st.spinner("Summarising…"):
            summaries[pick] = translator.summarise(t) or ""
    if pick in summaries:
        if summaries[pick]:
            st.success(summaries[pick])
        else:
            st.warning("No AI service reachable; the rule-by-rule translation above is exact.")


@st.fragment(run_every=f"{TAB_REFRESH}s")
def line_map():
    """Draw the Line Map tab: which machine stops or slows which, from data and PLC code."""
    data = current()
    if data is None or len(data[0]) < MIN_ROWS:
        n = 0 if data is None else len(data[0])
        st.info(
            f"The Line Map needs the decoded tag names: about 3 simulated minutes of data ({n} of {MIN_ROWS} "
            "seconds so far). This page updates by itself."
        )
        return
    import dependency

    df, dec, _ = data
    edges = line.dependencies(df, dec)
    if line.DEPS_ERROR:
        st.error(f"The Line Map couldn't be built: {line.DEPS_ERROR}. Check that dependency.py is in the folder.")
        return
    names = line.decoder.MACHINE_NAMES
    st.write(
        "**Which machine stops or slows which, learned two ways.** From the data: every time a machine's "
        "fault light came on, which others stopped or slowed in the next 30 s. From the PLC code: which "
        "other machines each program checks, and what it does then."
    )
    st.graphviz_chart(dependency.graph_dot(edges, names), use_container_width=True)
    st.caption(
        "🟢 confirmed (data and code agree) · 🟠 seen in the data only (a physical knock-on no program "
        "describes) · 🔵 dashed: in the code, not triggered yet · grey dots: the conveyor order · "
        "thicker = seen more often"
    )
    if edges:
        rows = []
        for e in edges:
            d, c = e["data"], e["code"]
            rows.append(
                {
                    "From": names[e["source"]],
                    "To": names[e["target"]],
                    "Effect": e["effect"],
                    "In the data": (f"{d['stops'] + d['slows']} of {d['faults']} faults" if d else "–"),
                    "In the PLC code": (
                        f"{c['file']}: when {c['when']}" + (f" → {c['speed']:g}/min" if c.get("speed") else "")
                        if c
                        else "–"
                    ),
                    "Status": e["status"],
                }
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        hidden = [e for e in edges if e["status"] == "seen in data"]
        if hidden:
            st.info(
                "🔎 Found in the data but written in no PLC program: "
                + ", ".join(f"{names[e['source']]} → {names[e['target']]}" for e in hidden)
                + ". These are physical knock-ons (bottles or cases backing up or running out), the kind of "
                "dependency nobody documented."
            )
    else:
        st.success("No machine has affected another one yet.")


@st.fragment(run_every=f"{TAB_REFRESH}s")
def work_tab():
    """Draw the Work Orders tab and the work-order / shift-report downloads."""
    data = current()
    if data is None or len(data[0]) < MIN_ROWS:
        st.info("Work orders appear once the line has been watched for about 3 simulated minutes.")
        return
    import maintenance

    df, dec, warns = data
    cards = line.alert_cards(df, dec)
    orders = line.work_orders(df, dec, cards)
    now = df["timestamp"].iloc[-1]
    st.write(
        f"**What to do, most urgent first** · line time {now:%H:%M} · priority = confidence × money at "
        f"risk ÷ time left · one line-minute costs {line.rupees(line.line_cost_per_min(df, dec))}"
    )
    if not orders:
        st.success("Nothing to do: no machine has an alert or a fault.")
    for i, o in enumerate(orders, 1):
        icon = {"N": "🔴", "B": "🟠", "W": "⚪"}.get(o["when"][0], "🟡")
        with st.container(border=True):
            conf = (
                "fault is on"
                if o["fault"]
                else (f"{o['confidence']}% confident" if o["confidence"] is not None else "")
            )
            st.markdown(f"**{icon} {i}. {o['machine']}: {o['problem']}** · {o['when']} · {conf}")
            st.write(o["slot"])
            c1, c2, c3 = st.columns(3)
            c1.metric("Planned repair", f"{o['planned_min']} min")
            c2.metric("If it breaks first", f"{o['unplanned_min']} min")
            c3.metric(
                "Money at risk" if not o["fault"] else "Line cost per minute",
                line.rupees(o["at_risk"]) if not o["fault"] else line.rupees(o["at_risk"] / o["unplanned_min"]),
            )
            with st.expander("Checklist, parts and tools"):
                st.markdown("\n".join(f"- ☐ {x}" for x in o["checklist"]))
                st.markdown(f"**Parts:** {', '.join(o['parts']) or '–'}  \n**Tools:** {', '.join(o['tools'])}")
                if o["evidence"]:
                    st.caption("Evidence: " + " · ".join(o["evidence"]))
    st.divider()
    names = line.decoder.MACHINE_NAMES
    wo = maintenance.work_order_html(orders, now, line.rupees)
    rep = maintenance.shift_report_html(
        df,
        cards,
        orders,
        line.downtime(df, dec, line.PRICE),
        line.events(df, dec),
        warns,
        line.rupees,
        names,
        line.predictions(df, dec),
    )
    d1, d2 = st.columns(2)
    d1.download_button(
        "⬇ Work orders (print or save as PDF)",
        wo,
        file_name=f"work_orders_{now:%H%M}.html",
        mime="text/html",
        use_container_width=True,
    )
    d2.download_button(
        "⬇ Shift report (print or save as PDF)",
        rep,
        file_name=f"shift_report_{now:%H%M}.html",
        mime="text/html",
        use_container_width=True,
    )
    st.caption(
        "Open the file in a browser, then Print → Save as PDF. Repair times and parts are typical figures "
        "for a small bottling line; a plant would put its own in maintenance.py."
    )


with tab_work:
    work_tab()

with tab_tags:
    tag_translator()

with tab_map:
    line_map()

with tab_plc:
    plc_logic()


with tab_ask:
    st.write("Ask about the line in plain words. Answers come only from the live data and list the signals used.")
    st.caption(
        "Try: *Why did the filler slow down?* · *Which machine needs attention?* · *Why did the line stop?* "
        "· *Why did the cobot slow down?* · *Is the capper OK?* · *How much has downtime cost?* · *When does the filler's program trip?*"
    )
    if "chat" not in st.session_state:
        st.session_state.chat = []
    with st.form("ask", clear_on_submit=True):
        q = st.text_input("Your question")
        sent = st.form_submit_button("Ask")
    if sent and q.strip():
        data = current()
        if data is None or len(data[0]) < MIN_ROWS:
            st.session_state.chat.append((q, "I need about 3 simulated minutes of data before I can answer.", "–", ""))
        else:
            with st.spinner("Thinking…"):
                ans, src = line.ask(q, *data)
            st.session_state.chat.append((q, ans, src, f"{data[0]['timestamp'].iloc[-1]:%H:%M:%S}"))
    for q, a, src, at in reversed(st.session_state.chat):
        with st.chat_message("user"):
            st.write(q + (f"  \n_asked at line time {at}_" if at else ""))
        with st.chat_message("assistant"):
            st.write(a)
            st.caption(f"answered by: {src}")
