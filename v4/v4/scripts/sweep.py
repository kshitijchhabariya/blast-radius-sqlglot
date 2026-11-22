#!/usr/bin/env python3
"""Run from the kit root (where sample/ and score.py live):  python3 scripts/sweep.py"""
import itertools, os, re, subprocess, sys
base = {"DATA_DIR": "sample", "MAPS_PATH": "artifacts/maps.pkl", "PYTHONHASHSEED": "0", "PATH": os.environ["PATH"]}
rows = []
for lam, ph in itertools.product([0.5, 1.0, 1.5, 2.0], [0.02, 0.05, 0.12]):
    env = dict(base, SAFETY_LAMBDA=str(lam), P_HOT=str(ph))
    out = subprocess.check_output([sys.executable, "selector.py"], env=env)
    open("tmp_out.jsonl", "wb").write(out)
    res = subprocess.check_output([sys.executable, "score.py", "sample", "sample/answers.json", "tmp_out.jsonl"], text=True)
    sim = float(re.search(r"Simulated:\s+([0-9.]+)", res).group(1)); miss = int(re.search(r"Misses:\s+(\d+)", res).group(1))
    rows.append((sim, miss, lam, ph))
for sim, miss, lam, ph in sorted(rows, key=lambda r: (r[1], r[0])):
    print(f"misses={miss}  simulated={sim:8.2f}s  non-miss={sim-300*miss:7.2f}s  lambda={lam}  P_HOT={ph}")
