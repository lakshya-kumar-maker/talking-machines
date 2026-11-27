"""Talking Machines — dashboard.   Run:  python -m streamlit run app.py
The simulated bottling line starts by itself in the background from 09:00; use the sidebar to restart it.
"""
import atexit
import os
import subprocess
import sys
import time

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
@st.cache_resource
def line_process():
    """One simulator for the whole app, kept between page refreshes."""
    return {"proc": None, "speed": 20, "seed": 7}


def start_line(speed, seed):
    lp = line_process()
    stop_line()
    os.makedirs("data", exist_ok=True)
    lp["proc"] = subprocess.Popen([sys.executable, "simulator.py", "--speed", str(speed), "--seed", str(seed),
                                   "--minutes", str(RUN_MINUTES)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lp["speed"], lp["seed"] = speed, seed
    time.sleep(0.5)                                  # let it write its first rows


def stop_line():
    p = line_process().get("proc")
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


atexit.register(stop_line)
if line_process()["proc"] is None:                   # first time the app runs: start the line from 09:00
    start_line(line_process()["speed"], line_process()["seed"])

with st.sidebar:
    st.header("Simulated line")
    lp = line_process()
    speed = st.select_slider("Speed (simulated seconds per real second)", [5, 10, 20, 30, 60], value=lp["speed"])
    seed = st.number_input("Seed (changes tag names and noise)", value=lp["seed"], step=1)
    if st.button("▶ Restart line from 09:00", use_container_width=True):
        start_line(speed, int(seed))
        st.rerun()
    if st.button("■ Stop line", use_container_width=True):
        stop_line()
    running = lp["proc"] is not None and lp["proc"].poll() is None
    st.caption(("🟢 running" if running else "⚪ stopped") + f" · {lp['speed']}× speed · seed {lp['seed']}")
    st.caption(f"A full run is {RUN_MINUTES} simulated minutes: about "
               f"{RUN_MINUTES * 60 / lp['speed'] / 60:.1f} real minutes at this speed.")

st.title("🏭 Talking Machines")
st.caption("Bottling line · read-only: it watches the machines and never controls them")


# ── reading the data ───────────────────────────────────────────────────────
@st.cache_data(show_spinner=False, max_entries=3)
def snapshot(bucket):
    df, dec = line.load(PATH)
    warns = line.warning_lines(PATH, dec) if len(df) >= MIN_ROWS else []
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


tab_live, tab_tags, tab_ask = st.tabs(["Live line", "Tag Translator", "Ask the Line"])


@st.fragment(run_every=f"{REFRESH}s")
def live():
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
            else:
                st.success(f"**{r['machine']}**  \nRunning")
            st.metric("Speed (bottles/min)", f"{r['speed']:.0f}" if r["speed"] is not None else "–")
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
    wanted = [s for s in ("motor_current", "motor_temp", "vibration") if s in sig]
    recent = df.tail(15 * 60).set_index("timestamp")
    chart = recent[[sig[s] for s in wanted]].rename(columns={sig[s]: dec[sig[s]]["name"] for s in wanted})
    st.line_chart(chart, height=280)
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


with tab_ask:
    st.write("Ask about the line in plain words. Answers come only from the live data and list the signals used.")
    st.caption("Try: *Why did the filler slow down?* · *Which machine needs attention?* · *Why did the line stop?*")
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