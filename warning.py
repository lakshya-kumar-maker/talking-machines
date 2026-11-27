"""Talking Machines — Early Warning (Person B).

Part 1  python warning.py ai4i     train a failure model on the AI4I 2020 dataset, print honest scores
Part 2  python warning.py stream   watch data/stream.csv for drifting signals and print warnings with reasons
"""
import sys

import numpy as np
import pandas as pd


# ── Part 1: failure model on AI4I 2020 ─────────────────────────────────────
def train_ai4i(path="data/ai4i2020.csv"):
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import classification_report, confusion_matrix
    from sklearn.model_selection import train_test_split
    import joblib

    d = pd.read_csv(path)
    y = d["Machine failure"]
    X = d.drop(columns=["UDI", "Product ID", "Machine failure", "TWF", "HDF", "PWF", "OSF", "RNF"])
    X = pd.get_dummies(X, columns=["Type"])                      
    X["Power [W]"] = X["Torque [Nm]"] * X["Rotational speed [rpm]"] * 2 * np.pi / 60
    X["Temp diff [K]"] = X["Process temperature [K]"] - X["Air temperature [K]"]

    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, stratify=y, random_state=42)
    model = RandomForestClassifier(n_estimators=300, class_weight="balanced", min_samples_leaf=2,
                                   random_state=42, n_jobs=-1).fit(Xtr, ytr)
    pred = model.predict(Xte)
    print(f"Failures in data: {y.sum()} of {len(y)} ({y.mean():.1%}) — so accuracy alone means nothing")
    print(classification_report(yte, pred, target_names=["ok", "failure"], digits=3))
    print("Confusion matrix [[ok→ok, ok→fail], [fail→ok, fail→fail]]:")
    print(confusion_matrix(yte, pred))
    imp = pd.Series(model.feature_importances_, index=X.columns).sort_values(ascending=False)
    print("\nWhat predicts failure most:")
    print(imp.head(6).round(3).to_string())
    joblib.dump({"model": model, "columns": list(X.columns)}, "data/ai4i_model.joblib")
    print("\nsaved data/ai4i_model.joblib")

    proba = model.predict_proba(Xte)[:, 1]
    for th in (0.5, 0.4, 0.3, 0.2):
        p = (proba >= th).astype(int)
        caught = ((p == 1) & (yte == 1)).sum()
        false = ((p == 1) & (yte == 0)).sum()
        print(f"threshold {th}: caught {caught}/{yte.sum()} failures, {false} false alarms")

BASE = (3, 8)        
SMOOTH = 60         
Z_ALERT = 6          
HOLD = 55            


def drift_warnings(path="data/stream.csv", names=None):
    """names: optional {tag: readable name} from the decoder, used only to make reasons readable."""
    d = pd.read_csv(path, parse_dates=["timestamp"]).set_index("timestamp")
    num = d.loc[:, d.nunique() > 2]                                          
    num = num.loc[:, [c for c in num if not num[c].is_monotonic_increasing]]  
    sm = num.rolling(SMOOTH, min_periods=1).mean()
    healthy = sm.iloc[BASE[0] * 60:BASE[1] * 60]
    mu = healthy.mean()
    sd = healthy.std().clip(lower=0.02 * mu.abs().clip(lower=1))            
    z = (sm - mu) / sd
    abnormal = (z.abs() > Z_ALERT).rolling(SMOOTH).sum() >= HOLD

    alerts, active = [], set()
    watch_from = d.index[0] + pd.Timedelta(minutes=BASE[1])                 
    for ts, row in abnormal.iterrows():
        if ts < watch_from:
            continue
        now = set(row[row].index)
        for tag in sorted(now - active):                                     
            change = (sm.at[ts, tag] - mu[tag]) / (abs(mu[tag]) or 1) * 100
            alerts.append({"time": ts, "tag": tag, "name": (names or {}).get(tag, tag),
                           "change_pct": round(change, 1), "z": round(z.at[ts, tag], 1)})
        active = now
    return pd.DataFrame(alerts)


def explain(alerts, window_s=180):
    """Group alerts that start close together into one readable warning."""
    if alerts.empty:
        print("No warnings: everything looks normal.")
        return
    group, start = [], None
    for _, a in alerts.iterrows():
        if start is None or (a["time"] - start).total_seconds() > window_s:
            if group:
                _print_group(group)
            group, start = [], a["time"]
        group.append(a)
    _print_group(group)


def _print_group(group, top=3):
    best = {}
    for g in group:                                   
        if g["tag"] not in best or abs(g["change_pct"]) > abs(best[g["tag"]]["change_pct"]):
            best[g["tag"]] = g
    ranked = sorted(best.values(), key=lambda g: -abs(g["z"]))
    parts = [f"{g['name']} {'up' if g['change_pct'] > 0 else 'down'} {abs(g['change_pct'])}%" for g in ranked[:top]]
    more = f" (+{len(ranked) - top} more signals)" if len(ranked) > top else ""
    print(f"⚠ {group[0]['time']:%H:%M}  " + ", ".join(parts) + more)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "stream"
    if what == "ai4i":
        train_ai4i()
    else:
        explain(drift_warnings())