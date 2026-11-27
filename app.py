"""Talking Machines — dashboard.   Run:  streamlit run app.py
(start the simulator in another terminal first:  python simulator.py --speed 20)
"""
import os

import pandas as pd
import streamlit as st

import line

PATH = "data/stream.csv"
MIN_ROWS = 180                      

st.set_page_config(page_title="Talking Machines", page_icon="🏭", layout="wide")
st.title("🏭 Talking Machines")
st.caption("Bottling line · read-only: it watches the machines and never controls them")


@st.cache_data(show_spinner=False)
def snapshot(mtime):
    df, dec = line.load(PATH)
    warns = line.warning_lines(PATH, dec) if len(df) >= MIN_ROWS else []
    return df, dec, warns


def current():
    if not os.path.exists(PATH):
        return None
    try:
        return snapshot(os.path.getmtime(PATH))
    except Exception as e:                       
        st.info(f"Reading the line… ({e.__class__.__name__})")
        return None


tab_live, tab_tags, tab_ask = st.tabs(["Live line", "Tag Translator", "Ask the Line"])


@st.fragment(run_every="2s")
def live():
    data = current()
    if data is None:
        st.warning("No data yet. Start the simulator:  python simulator.py --speed 20")
        return
    df, dec, warns = data
    now = df["timestamp"].iloc[-1]
    if len(df) < MIN_ROWS:
        st.info(f"Listening… {len(df)} seconds of data so far. Names appear after about 3 minutes.")
        return
    stat = line.status(df, dec)
    cols = st.columns(len(stat))
    for col, (_, r) in zip(cols, stat.iterrows()):
        with col:
            if r["fault"]:
                st.error(f"**{r['machine']}**  \nFAULT")
            elif not r["running"]:
                st.warning(f"**{r['machine']}**  \nStopped")
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
    st.subheader(f"Signals · {now:%H:%M:%S}")
    machines = line.by_machine(dec)
    labels = {line.decoder.MACHINE_NAMES.get(m, m): m for m in line.MACHINE_ORDER if m in machines}
    pick = st.selectbox("Machine", list(labels), index=1 if len(labels) > 1 else 0, key="pick_machine")
    sig = machines[labels[pick]]
    wanted = [s for s in ("motor_current", "motor_temp", "vibration") if s in sig]
    recent = df.tail(15 * 60).set_index("timestamp")
    chart = recent[[sig[s] for s in wanted]].rename(columns={sig[s]: dec[sig[s]]["name"] for s in wanted})
    st.line_chart(chart, height=280)
    speed = recent[[sig["speed"]]].rename(columns={sig["speed"]: dec[sig["speed"]]["name"]}) \
        if "speed" in sig else None
    if speed is not None:
        st.line_chart(speed, height=160)


with tab_live:
    live()


with tab_tags:
    data = current()
    if data is None or len(data[0]) < MIN_ROWS:
        st.info("The Tag Translator needs about 3 minutes of data.")
    else:
        df, dec, _ = data
        last = df.iloc[-1]
        table = pd.DataFrame([{"Tag": t, "Means": r["name"], "Unit": r["unit"], "Confidence": r["confidence"],
                               "Latest value": last[t], "Why": r["reason"]} for t, r in dec.items()])
        table = table.sort_values(["Means"]).reset_index(drop=True)
        low = int((table["Confidence"] == "low").sum())
        st.write(f"**{len(table)} signals decoded from how they behave** · {low} marked low confidence")
        st.dataframe(table, use_container_width=True, hide_index=True, height=560)


with tab_ask:
    st.write("Ask about the line in plain words. Answers come only from the live data and list the signals used.")
    st.caption("Try: *Why did the filler slow down?* · *Which machine needs attention?* · *Is the capper OK?*")
    if "chat" not in st.session_state:
        st.session_state.chat = []
    with st.form("ask", clear_on_submit=True):
        q = st.text_input("Your question")
        sent = st.form_submit_button("Ask")
    if sent and q.strip():
        data = current()
        if data is None or len(data[0]) < MIN_ROWS:
            st.session_state.chat.append((q, "I need about 3 minutes of data before I can answer.", "–"))
        else:
            with st.spinner("Thinking…"):
                ans, src = line.ask(q, *data)
            st.session_state.chat.append((q, ans, src))
    for q, a, src in reversed(st.session_state.chat):
        with st.chat_message("user"):
            st.write(q)
        with st.chat_message("assistant"):
            st.write(a)
            st.caption(f"answered by: {src}")