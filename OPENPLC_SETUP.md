# Running Talking Machines against a real soft PLC (OpenPLC)

In this mode the **machines' control programs run inside OpenPLC**, an open-source IEC 61131-3 PLC runtime.
The filler's vibration trip, the capper's jam trip and every stop/slow rule are executed by OpenPLC's own
scan cycle and TON timers, not by Python.

```
plant.py  (the machines and their sensors)           OpenPLC  (our Structured Text)          Talking Machines
  writes sensor values to %MW  ─── Modbus TCP ───►   reads inputs, runs logic, timers   ◄── collector.py reads,
  reads run/stop/speed from %QW ◄────────────────    writes outputs                          never writes
```

`plant.py` plays the role of the I/O modules and the physical machines. `collector.py` is the same read-only
collector as before.

---

## 1. Install OpenPLC Runtime v3 (once, about 20 minutes)

The most reliable way on Windows is **WSL (Ubuntu inside Windows)**.

1. Open **PowerShell as administrator** and run:
   ```powershell
   wsl --install -d Ubuntu
   ```
   Restart if asked, then open **Ubuntu** from the Start menu and create a user name and password.
2. In the Ubuntu window:
   ```bash
   sudo apt update && sudo apt install -y git
   git clone https://github.com/thiagoralves/OpenPLC_v3.git
   cd OpenPLC_v3
   ./install.sh linux
   ```
   This compiles OpenPLC and takes 10–20 minutes. Keep the laptop plugged in.
3. Start it (leave this window open while you use it):
   ```bash
   sudo ./start_openplc.sh
   ```
4. In a browser on Windows open **http://localhost:8080** and log in with **openplc / openplc**.
5. **Settings**: make sure **Modbus Server** is enabled on **port 502** (it is by default). Save.

If `localhost` doesn't reach it, run `hostname -I` in Ubuntu and use that IP address instead of 127.0.0.1
everywhere below.

## 2. Make the PLC program and upload it

In the project folder (PowerShell, with the venv active):

```powershell
python openplc_export.py --seed 7
```

This writes `data/openplc/talking_machines.st`: all six controllers in one program, with the same logic as
the PLC Logic tab.

In the OpenPLC web page:
1. **Programs** → **Choose File** → pick `data/openplc/talking_machines.st` → **Upload Program**.
2. Give it a name (e.g. *Talking Machines seed 7*) → **Upload program**.
3. Wait for the compile to finish (about a minute). If it shows errors, copy the log lines and send them.
4. **Dashboard** → **Start PLC**. The status should say **Running**.

The program is made for one seed because the tag names change with the seed. To use another seed, export
and upload again.

## 3. Run it

**From the dashboard:** sidebar → *How Talking Machines reads the line* → **OpenPLC (real soft PLC)** →
address `127.0.0.1:502` → speed **5** → **▶ Restart line from 09:00**.

**Or by hand**, in two PowerShell windows:

```powershell
python plant.py --port 502 --speed 5
python collector.py --port 502 --map data/openplc/map.json --rate 4
```

`plant.py` should print `check OK: the PLC keeps the sensor values written at registers 1024..`.
At 5× a full shift takes 9 minutes; the filler trips around minute 6–7 of real time.

**Keep the speed at 10 or below.** OpenPLC's timers count real seconds: the jam timer needs a jam to last
2 real seconds, and at 5× a 15-second jam lasts 3.

## 4. Show it to the judges

* OpenPLC → **Monitoring**: the PLC's own variables, live. `DB102_DBD24` is the filler vibration,
  `DB102_DBX0_1` the filler fault bit (names depend on the seed; the Tag Translator tab tells you which
  is which). Watch the fault bit flip when the vibration timer trips. That decision is made by the PLC.
* Dashboard → **PLC Logic**: the same code, translated into English.
* Sidebar → **🔧 Break a machine** works here too: the plant breaks the machine and **OpenPLC's rules**
  catch it.

> Don't run `collector.py --test-write` against OpenPLC. A real PLC accepts writes (that's how the plant
> model feeds it), so the test would say "accepted". Talking Machines stays read-only because the
> collector never sends a write request, not because the PLC refuses one. Show the read-only proof with the
> built-in PLC mode instead.

## 5. If something doesn't work

| What you see | What to do |
|---|---|
| `waiting for the PLC at 127.0.0.1:502` | OpenPLC isn't running, the PLC isn't started, or Modbus is off in Settings. In WSL try the IP from `hostname -I`. |
| `The PLC did not keep the sensor values ...` | The program isn't running (press Start PLC). Or your OpenPLC maps `%MW0` elsewhere: newer OpenPLC versions show this in the Modbus Server's *Address Mapping Reference*. Export again with `python openplc_export.py --seed 7 --mw-base ADDRESS` and upload again. |
| The capper never jams | Plant speed is too high for the PLC's real-time 2 s timer: use 5. |
| Compile error on upload | Send the log lines; it's usually a small syntax difference between OpenPLC versions. |

## No OpenPLC at hand?

`python softplc.py --port 5502` is a small stand-in with the same register layout that runs the same
Structured Text with real TON timers. Then use port 5502 instead of 502. It's for testing; for the demo,
the point is that a real PLC runtime runs the code.
