# Talking Machines

**An AI layer that lets old factory machines explain themselves.**

Team Raphael · Shubhankar Jaiswal & Lakshya Kumar · Jigyasa University
ALGONEX 1.0 · Artificial Intelligence track

---

## The problem

Most factories still run machines that are 15–30 years old. Their PLCs (the small computers that control them) are full of tags like `DB102.DBD24`, `F8:37` and `M4_FLT`, with no documentation. The engineers who wrote the code have often retired. So when a machine slows down or stops, nobody can quickly tell **what happened, why, or what will fail next.**

Replacing the machines costs crores. Talking Machines adds understanding without replacing or touching anything.

## What it does

| | Feature | What it means |
|---|---|---|
| 🔌 | **Listens safely** | Reads the PLC over **Modbus TCP**. The link is **read-only**: every write request is refused, so it can never change a machine. |
| 🔍 | **Tag Translator** | Works out what each cryptic tag means **from how it behaves** (it shakes → vibration; it flips when the machine stops → running flag). No manual needed. |
| 📜 | **PLC Logic translator** | Reads each machine's own control program (IEC 61131-3 Structured Text) and explains every rule in plain English, using the decoded names. |
| ⚠️ | **Early warning** | Spots signals drifting away from normal (e.g. vibration rising, then motor heating) and warns **10+ minutes before a failure**. |
| 💬 | **Ask the Line** | Ask in plain words: *"Why did the filler stop?"* Answers come only from the line's data and name the signals used. |
| 🤖 | **Cobot monitoring** | Watches a palletising robot arm, including its safety zone. It knows that slowing down for a person is normal, not a fault. |

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

| Measure | Result |
|---|---|
| Tags decoded correctly (unseen seed) | __ / 53 |
| Warning before filler failure | __ minutes |
| Ask the Line test questions answered correctly | __ / 5 |
| Failure model on AI4I 2020 (real public dataset) | precision 0.83 · recall 0.84 |
| Writes possible to the machine | **0** (read-only by design) |

## How it works

```
 Simulated bottling line + cobot  ──Modbus TCP (read-only)──►  collector.py  ──►  data/stream.csv
 (simulator.py acts as the PLC)                                                        │
                                                                                       ▼
                     decoder.py  →  what each tag means (data/decoded.json)
                     translator.py → PLC programs in plain English
                     warning.py   →  drift early warnings + AI4I failure model
                     line.py      →  causes, alerts, answers (Groq → Gemini → rule-based offline)
                                                                                       │
                                                                                       ▼
                                                app.py  (Streamlit dashboard)
```

The line: **depalletiser → filler → capper → labeller → case packer → palletising cobot**, 53 signals in total, using real tag styles from Siemens (`DB102.DBD24`), Allen-Bradley (`F8:37`, `B3:3/0`), legacy (`M4_FLT`) and robot (`ROB1.AI3`) controllers.

**Decoder:** gives every signal a behaviour fingerprint (on/off or continuous, noise, trend, how it changes when the machine stops), groups tags by controller address, then works out which machine each group belongs to from the order faults travel along the line.

**PLC translator:** a real parser for Structured Text (tokenizer → syntax tree → English). It is not an AI guess: the same code always gives the same English. Tags without live data are marked "not decoded" instead of guessed.

**Early warning:** learns each signal's normal range from the first minutes, then raises an alert when a smoothed signal moves far outside it and stays there.

**Ask the Line:** a rule engine works out the facts (who had a fault, when, and what came before it). The language model only words the answer from those facts. With no internet, the rule-based answers still work.

## Run it

Needs Python 3.12+ on Windows, macOS or Linux.

```powershell
git clone https://github.com/Lakshya-Kumar-maker/talking-machines.git
cd talking-machines
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Optional, for AI answers: create a file called `.env` (never commit it):

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
python warning.py ai4i                    # failure model on the AI4I 2020 dataset

python simulator.py --plc --speed 20      # act as a live PLC on 127.0.0.1:5020
python collector.py                       # read it over Modbus TCP
python collector.py --test-write          # prove writes are refused
```

`--seed` changes the run: tag order, tag names, when the jams happen and when the filler wears out.

## Safety by design

- **Read-only:** only Modbus read functions (03/04) are used. Any write gets exception 01 "illegal function".
- **No hidden guessing:** every answer lists the signals it used; unknown tags are marked as unknown.
- **Secrets stay out of code:** API keys live only in `.env`, which is in `.gitignore`.

## Files

| File | Job |
|---|---|
| `app.py` | Streamlit dashboard: Live line, Tag Translator, PLC Logic, Ask the Line |
| `simulator.py` | Bottling line + cobot with jams, wear and failure; can act as a Modbus PLC |
| `plc.py` | Minimal Modbus TCP server and client, standard library only, read-only |
| `collector.py` | Reads the PLC and keeps the history file, like a plant historian |
| `plc_programs.py` | Each machine's control program in Structured Text |
| `decoder.py` | Works out what each tag means from behaviour |
| `translator.py` | PLC code → plain English |
| `warning.py` | Drift early warning + AI4I 2020 failure model |
| `line.py` | Machine status, causes, alert cards, Ask the Line |

## Built with

Python · pandas · NumPy · scikit-learn · Streamlit · Modbus TCP · Groq (`gpt-oss-120b`) · Google Gemini · AI4I 2020 dataset (UCI)

## Honest limits

- The line is simulated, built to behave like a real one. Pointing `collector.py` at a real PLC's IP address is the next step.
- Decoding is strongest after the line has had at least one stoppage. Before that, look-alike machines are marked low confidence.
- The PLC translator covers the Structured Text used here (IF/ELSIF/ELSE, assignments, maths, comparisons, TON timers). Ladder logic exported as text is the next step.