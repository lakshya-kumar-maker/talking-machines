# Talking Machines

**An AI layer that lets old factory machines explain themselves, and warns before they break.**

Team Raphael · Shubhankar Jaiswal & Lakshya Kumar · Jigyasa University
ALGONEX 1.0 · Artificial Intelligence track

---

## The problem

Most factories still run machines that are 15–30 years old. Their PLCs (the small computers that control them)
are full of tags like `DB102.DBD24`, `F8:37` and `M4_FLT`, with no documentation, and the engineers who wrote the
code have often retired. When a machine slows down or stops, nobody can quickly tell **what happened, why, or what
will fail next**. Replacing the machines costs crores. Talking Machines adds understanding without replacing or
touching anything.

## What it does

| | Feature | What it means |
|---|---|---|
| 🔌 | **Listens safely** | Reads the PLC over **Modbus TCP**. The collector only sends read requests; a write is refused. |
| 🔍 | **Tag Translator** | Works out what each cryptic tag means **from how it behaves**: it shakes → vibration; it's on while the counter climbs → running. No manual needed. |
| 📜 | **PLC Logic translator** | Reads each machine's own control program (IEC 61131-3 Structured Text, no comments) and explains every rule in plain English, using the decoded names. |
| ⚠️ | **Two detectors** | A per-signal drift check, plus an **Isolation Forest** that watches all of a machine's signals together and often notices 1–2 minutes earlier. |
| ⏱ | **Time until failure** | Fits the drift's trend and extends it to the trip limit **read from the PLC program**: *"Filler trips at about 09:33."* |
| 🎯 | **Measured confidence** | Every alert gets a %, from a model tested on runs it never saw, including harmless look-alikes. |
| 💰 | **Downtime cost** | Lost output in ₹, per stoppage and per cause. |
| 🛠 | **Work orders** | What to check, parts, tools and when, ranked by confidence × money at risk ÷ time left. Downloadable work orders and shift report. |
| 🗺 | **Line map** | Which machine stops or slows which, learned from the data **and** from the PLC code. Finds dependencies nobody documented. |
| 💬 | **Ask the Line** | *"Why did the line stop?" · "When will the filler fail?" · "What should I fix first?"* Answers come from the data, list their signals, and work offline. |
| 🔧 | **Break a machine** | Live demo: pick a fault (bearing, glue heater, cap jam, person near the cobot) and watch it get caught. |
| 🏭 | **Real PLC runtime** | The machines' control code also runs on **OpenPLC**; its own timers trip the faults. See [OPENPLC_SETUP.md](OPENPLC_SETUP.md). |

## Example

The filler's PLC code, exactly as an engineer sees it:

```
TON_VIB(IN := DB102.DBD24 > 7.2, PT := T#10S);
IF TON_VIB.Q THEN
    DB102.DBX0.1 := TRUE;
END_IF;
```

Talking Machines:

> If the filler vibration stays above 7.2 mm/s for 10 seconds: raise the filler fault.

The code never says "filler" or "vibration". Those names were learned from the live data.

## Results

Every number is checked against the simulator's answer key, which the system never reads.

| Measure | Result |
|---|---|
| Tags decoded correctly | **53 / 53** on 24 test runs, including 9 where a machine broke early |
| Warning before the filler trips | **about 12 minutes** (single-signal check); the Isolation Forest is 1–2 minutes earlier still |
| Trip-time forecast, made 7 minutes ahead | average error **about 45 s**; the real trip was inside the predicted range in **20 / 20** runs |
| Trip-time forecast, 3 minutes ahead | average error **about 20 s**, 20 / 20 inside the range |
| False forecasts on healthy machines | **0** (checked every minute of 12 runs) |
| Alerts scored 90 %+ confidence | **157 / 157** were real faults (20 unseen runs with harmless look-alikes) |
| Alerts scored under 50 % | 76 of 85 were indeed harmless |
| Failure model on AI4I 2020 (real public dataset) | precision 0.83 · recall 0.84 |
| Real PLC runtime | a full 45-minute shift on OpenPLC; its own timer tripped the filler |
| Writes the collector can make to the machine | **0** (read-only by design) |

## How it works

```
 Machines (simulator.py, or plant.py + OpenPLC)  ──Modbus TCP, read-only──►  collector.py  ──►  data/stream.csv
                                                                                                    │
     decoder.py      what each tag means, from behaviour                                            │
     translator.py   PLC programs → plain English; trip limits                                      ▼
     warning.py      per-signal drift warnings (+ AI4I failure model)
     anomaly.py      Isolation Forest: all of a machine's signals together
     prediction.py   time until the PLC trip limit
     confidence.py   how sure each alert is (tested on unseen runs)
     dependency.py   which machine stops or slows which (data + PLC code)
     maintenance.py  work orders and shift report
     line.py         machine status, cost, alert cards, Ask the Line (Groq → Gemini → offline rules)
                                                                                                    │
                                                                                                    ▼
                                                           app.py  (Streamlit dashboard)
```

The line: **depalletiser → filler → capper → labeller → case packer → palletising cobot**, 53 signals with real tag
styles from Siemens (`DB102.DBD24`), Allen-Bradley (`F8:37`, `B3:3/0`), legacy (`M4_FLT`) and robot (`ROB1.AI3`)
controllers. Every run also contains harmless look-alikes (a heavier product batch, a forklift bump, a warm
afternoon), so the alerts are tested honestly.

## Run it

Needs Python 3.12+ on Windows, macOS or Linux.

```powershell
git clone https://github.com/Lakshya-Kumar-maker/talking-machines.git
cd talking-machines
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Optional, for AI-worded answers: create a file called `.env` (never commit it):

```
GROQ_API_KEY=your_key_here
GEMINI_API_KEY=your_key_here
```

Start the dashboard. It starts the simulated line and PLC by itself, beginning at 09:00:

```powershell
python -m streamlit run app.py
```

Or run each part by hand:

```powershell
python simulator.py --fast --seed 42      # a whole 45-minute shift in a few seconds
python decoder.py --score                 # how many tags were decoded correctly
python translator.py                      # every PLC program in plain English
python warning.py stream                  # early warnings with reasons
python anomaly.py                         # Isolation Forest findings
python prediction.py                      # time until a PLC trip limit
python prediction.py --test               # replay 20 runs and measure forecast accuracy
python dependency.py                      # the line map
python confidence.py --build              # replay 50 runs (a few minutes)…
python confidence.py --train              # …and print the tested confidence table

python simulator.py --plc --speed 20      # act as a live PLC on 127.0.0.1:5020
python collector.py                       # read it over Modbus TCP
python collector.py --test-write          # prove writes are refused
```

`--seed` changes the run: tag order, tag names, when the jams happen, when the filler wears out, and which harmless
disturbances appear. `--calm` turns the disturbances off.

### Real PLC (OpenPLC)

`openplc_export.py` turns the machines' programs into one OpenPLC program; `plant.py` plays the machines and their
sensors; the collector reads OpenPLC exactly as it reads any PLC. Step by step: [OPENPLC_SETUP.md](OPENPLC_SETUP.md).

### Run the tests

```powershell
pytest -q
```

## Safety by design

- **Read-only:** the collector only sends Modbus read requests (03/04). The built-in PLC refuses any write with
  exception 01 "illegal function".
- **No hidden guessing:** every answer lists the signals it used; unknown tags are marked unknown; low-confidence
  alerts say so.
- **Secrets stay out of code:** API keys live only in `.env`, which is in `.gitignore`.

## Project structure

| File | Job |
|---|---|
| `app.py` | Streamlit dashboard: Live line, Work Orders, Tag Translator, PLC Logic, Line Map, Ask the Line |
| `simulator.py` | Bottling line + cobot with jams, wear, failures and harmless look-alikes; can act as a Modbus PLC |
| `plc.py` | Minimal Modbus TCP server and client, standard library only |
| `collector.py` | Reads the PLC and keeps the history file, like a plant historian (read-only) |
| `plc_programs.py` | Each machine's control program in Structured Text |
| `decoder.py` | Works out what each tag means from behaviour |
| `translator.py` | PLC code → plain English; reads the trip limits |
| `warning.py` | Per-signal drift warnings + AI4I 2020 failure model |
| `anomaly.py` | Isolation Forest second opinion |
| `prediction.py` | Time until a PLC trip limit |
| `confidence.py` + `confidence_model.json` | Alert confidence model and its test results |
| `dependency.py` | Machine dependency map |
| `maintenance.py` | Failure patterns, work orders, shift report |
| `line.py` | Machine status, cost, alert cards, Ask the Line |
| `openplc_export.py` · `plant.py` · `softplc.py` | Real-PLC mode: OpenPLC program, plant model, a stand-in PLC for testing |
| `OPENPLC_SETUP.md` | How to run against OpenPLC |
| `tests/` | pytest tests that lock in the behaviour above |

## Built with

Python · pandas · NumPy · scikit-learn · Streamlit · Altair · Modbus TCP · OpenPLC (IEC 61131-3) · Groq
(`gpt-oss-120b`) · Google Gemini · AI4I 2020 dataset (UCI)

## Honest limits

- The machines are simulated. The PLC logic has been run on a real PLC runtime (OpenPLC); connecting to a real
  line's PLC is the next step.
- Decoding is strongest after the line has had at least one stoppage, and the cobot's safety-zone flag needs one
  visit. Before that, look-alike signals are marked low confidence.
- Repair times and parts in the work orders are typical figures for a small bottling line; a plant puts in its own.
- The PLC translator covers the Structured Text used here (IF/ELSIF/ELSE, assignments, maths, comparisons, TON
  timers). Ladder logic exported as text is the next step.
- Streamlit is pinned to 1.67; newer versions replace `use_container_width` with `width='stretch'`.