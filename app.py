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
MIN_ROWS = 180                      # the decoder needs ~3 simulated minutes before names mean anything
RUN_MINUTES = 45
REFRESH = 3                         # seconds between live updates

st.set_page_config(page_title="Talking Machines", page_icon="🏭", layout="wide")
st.markdown("""<style>
.stale-element, [data-stale="true"] {opacity: 1 !important; filter: none !important; transition: none !important;}
[data-testid="stStatusWidget"] {visibility: hidden;}
</style>""", unsafe_allow_html=True)


# ── the simulated line, running in the background ─────────────────────────
LOGS = {"proc": "data/simulator.log", "collector": "data/collector.log"}


@st.cache_resource
def line_process():
    """The simulated PLC (and the collector reading it), kept between page refreshes."""
    atexit.register(stop_line)                       # once per server, not once per script rerun
    return {"proc": None, "collector": None, "speed": 20, "seed": 7, "mode": "PLC (Modbus TCP)"}


def _spawn(key, args):
    """Start a helper process; its output goes to a log file so a crash can be shown on the page."""
    log = open(LOGS[key], "w", encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    line_process()[key] = subprocess.Popen([sys.executable] + args, stdout=log, stderr=subprocess.STDOUT, env=env)
    log.close()                                      # the child keeps its own handle


def start_line(speed, seed, mode):
    lp = line_process()
    stop_line()
    os.makedirs("data", exist_ok=True)
    for f in ("data/stream.csv", "data/plc_map.json"):
        if os.path.exists(f):
            os.remove(f)
    args = ["simulator.py", "--speed", str(speed), "--seed", str(seed), "--minutes", str(RUN_MINUTES)]
    if mode.startswith("PLC"):
        _spawn("proc", args + ["--plc"])
        _spawn("collector", ["collector.py", "--rate", str(max(50, speed * 3))])
    else:
        _spawn("proc", args)
    lp["speed"], lp["seed"], lp["mode"] = speed, seed, mode
    time.sleep(1.0)                                  # let the first rows arrive


def stop_line():
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


if line_process()["proc"] is None and not line_process().get("stopped"):   # first run: start from 09:00
    start_line(line_process()["speed"], line_process()["seed"], line_process()["mode"])

with st.sidebar:
    st.header("Simulated line")
    lp = line_process()
    speed = st.select_slider("Speed (simulated seconds per real second)", [5, 10, 20, 30, 60], value=lp["speed"])
    seed = st.number_input("Seed (changes tag names and noise)", value=lp["seed"], step=1)
    modes = ["PLC (Modbus TCP)", "File only"]
    mode = st.radio("How Talking Machines reads the line", modes, index=modes.index(lp["mode"]),
                    help="PLC: the simulator acts as a PLC on Modbus TCP port 5020 and a collector reads it, "
                         "read-only, like in a real plant. File: the simulator writes the data file directly.")
    if st.button("▶ Restart line from 09:00", use_container_width=True):
        lp["stopped"] = False
        start_line(speed, int(seed), mode)
        st.rerun()
    if st.button("■ Stop line", use_container_width=True):
        lp["stopped"] = True
        stop_line()
    alive = lambda p: p is not None and p.poll() is None
    running = alive(lp["proc"]) and (alive(lp["collector"]) or not lp["mode"].startswith("PLC"))
    st.caption(("🟢 running" if running else "⚪ stopped") + f" · {lp['speed']}× speed · seed {lp['seed']}")
    if lp["mode"].startswith("PLC"):
        st.caption("Source: PLC at 127.0.0.1:5020 · Modbus TCP · read-only (writes are refused)")
    st.caption(f"A full run is {RUN_MINUTES} simulated minutes: about "
               f"{RUN_MINUTES * 60 / lp['speed'] / 60:.1f} real minutes at this speed.")

st.title("🏭 Talking Machines")
st.caption("Bottling line · read-only: it watches the machines and never controls them")


# ── reading the data ───────────────────────────────────────────────────────
@st.cache_data(show_spinner=False, max_entries=3)
def snapshot(bucket):
    df, dec = line.load(PATH)
    warns = line.warning_lines(df, dec) if len(df) >= MIN_ROWS else []
    return df, dec, warns


def current():
    if not os.path.exists(PATH):
        return None
    try:
        # reload every few seconds; Windows doesn't update the file's time while the simulator writes it
        return snapshot(int(time.time() // REFRESH))
    except Exception as e:                           # file being rewritten: try again on the next refresh
        st.info(f"Reading the line… ({e.__class__.__name__})")
        return None


def clock(df):
    now = df["timestamp"].iloc[-1]
    start = df["timestamp"].iloc[0]
    done = (now - start).total_seconds() / (RUN_MINUTES * 60)
    lp = line_process()
    live_now = lp["proc"] is not None and lp["proc"].poll() is None
    c1, c2 = st.columns([1, 3])
    c1.metric("🕘 Line time", f"{now:%H:%M:%S}")
    with c2:
        st.write("")
        st.progress(min(done, 1.0), text=f"Shift progress · {'live' if live_now else 'stopped'} · "
                                         f"{lp['speed']}× speed")


tab_live, tab_tags, tab_plc, tab_ask = st.tabs(["Live line", "Tag Translator", "PLC Logic", "Ask the Line"])


@st.fragment(run_every=f"{REFRESH}s")
def live():
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
                st.caption(f"speed setting {r['override']:.0f}% · "
                           + ("🧍 person in safety zone" if r["zone"] else "safety zone clear"))
            else:
                st.caption(f"{r['current_A']} A · {r['temp_C']} °C · vib {r['vibration']} mm/s")

    st.subheader("Why this alert")
    cards = line.alert_cards(df, dec)
    if not cards:
        st.success("No alerts: every machine looks normal.")
    for c in cards:
        with st.container(border=True):
            badge = {"Critical": "🔴", "High": "🟠", "Medium": "🟡"}[c["severity"]]
            st.markdown(f"**{badge} {c['title']}**  ·  {c['severity']} severity  ·  {c['confidence']} confidence")
            if c["changed"]:
                st.dataframe(pd.DataFrame([{"Signal": ch["signal"], "Normal": ch["normal"], "Now": ch["now"],
                                            "Unit": ch["unit"], "Change": f"{ch['change_pct']:+}%",
                                            "Since": f"{ch['since']:%H:%M}"} for ch in c["changed"]]),
                             hide_index=True, use_container_width=True)
            st.markdown(f"**Why:** {c['cause']}  \n**Do:** {c['action']}")
    with st.expander("All warnings, newest first"):
        for w in reversed(warns):
            st.write("⚠ " + w)

    st.subheader("Signals")
    machines = line.by_machine(dec)
    labels = {line.decoder.MACHINE_NAMES.get(m, m): m for m in line.MACHINE_ORDER if m in machines}
    pick = st.selectbox("Machine", list(labels), index=1 if len(labels) > 1 else 0, key="pick_machine")
    sig = machines[labels[pick]]
    wanted = [s for s in ("motor_current", "motor_temp", "vibration") if s in sig] or \
             [s for s in ("j2_torque", "j3_torque", "j1_torque") if s in sig]
    recent = df.tail(15 * 60).set_index("timestamp")
    def short(name):  # "Cobot joint 1 (base) torque" -> "Joint 1 (base) torque"
        return name[len(pick):].strip().capitalize() if name.lower().startswith(pick.lower() + " ") else name
    chart = recent[[sig[s] for s in wanted]].rename(columns={sig[s]: short(dec[sig[s]]["name"]) for s in wanted})
    long = chart.reset_index().melt("timestamp", var_name="Signal", value_name="Value")
    st.altair_chart(
        alt.Chart(long).mark_line().encode(
            x=alt.X("timestamp:T", title=None),
            y=alt.Y("Value:Q", title=None),
            color=alt.Color("Signal:N", sort=list(chart.columns),
                            legend=alt.Legend(orient="bottom", direction="vertical", title=None, labelLimit=0)),
        ).properties(height=280),
        use_container_width=True)
    if "speed" in sig:
        st.line_chart(recent[[sig["speed"]]].rename(columns={sig["speed"]: dec[sig["speed"]]["name"]}), height=160)


with tab_live:
    live()


with tab_tags:
    data = current()
    if data is None or len(data[0]) < MIN_ROWS:
        st.info("The Tag Translator needs about 3 simulated minutes of data. Switch tabs again in a moment.")
    else:
        df, dec, _ = data
        last = df.iloc[-1]
        table = pd.DataFrame([{"Tag": t, "Means": r["name"], "Unit": r["unit"], "Confidence": r["confidence"],
                               "Latest value": last[t], "Why": r["reason"]} for t, r in dec.items()])
        table = table.sort_values(["Means"]).reset_index(drop=True)
        low = int((table["Confidence"] == "low").sum())
        st.write(f"**{len(table)} signals decoded from how they behave** · {low} marked low confidence "
                 f"· as of {df['timestamp'].iloc[-1]:%H:%M:%S}")
        st.dataframe(table, use_container_width=True, hide_index=True, height=560)


with tab_plc:
    import translator
    data = current()
    folder = "data/plc_programs"
    files = sorted(f for f in os.listdir(folder) if f.endswith(".st")) if os.path.isdir(folder) else []
    if data is None or len(data[0]) < MIN_ROWS or not files:
        st.info("The PLC Logic translator needs the decoded tag names (about 3 simulated minutes of data).")
    else:
        dec = data[1]
        progs = translator.translate_all(folder, dec)
        label = lambda f: f"{f}  →  {line.decoder.MACHINE_NAMES.get(progs[f]['machine'], 'unknown machine')}"
        st.write("Each machine's own control program, read straight from the PLC export. The code uses the plant's "
                 "cryptic tags and has no comments; we translate it rule by rule using the decoded names.")
        pick = st.selectbox("Program", files, format_func=label, key="pick_program")
        t = progs[pick]
        for n, r in enumerate(t["rules"], 1):
            left, right = st.columns([1, 1])
            left.code(r["code"], language="pascal")
            right.markdown(f"**Rule {n}.** {r['english']}")
        if t["unknown"]:
            st.caption("Not decoded (internal flags with no live data): " + ", ".join(t["unknown"]))
        if st.button("Summarise this program with AI"):
            with st.spinner("Summarising…"):
                summary = translator.summarise(t)
            if summary:
                st.success(summary)
            else:
                st.warning("No AI service reachable; the rule-by-rule translation above is exact.")


with tab_ask:
    st.write("Ask about the line in plain words. Answers come only from the live data and list the signals used.")
    st.caption("Try: *Why did the filler slow down?* · *Which machine needs attention?* · *Why did the line stop?* "
               "· *Why did the cobot slow down?* · *Is the capper OK?* · *When does the filler's program trip?*")
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