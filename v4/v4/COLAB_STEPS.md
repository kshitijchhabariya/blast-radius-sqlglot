# Colab steps (≈10 min)
1. Upload this zip's files over `kit/` (overwrite `scripts/build_maps.py`, `selector.py`, `Dockerfile`; add `scripts/sweep.py`).
2. (Optional but recommended – removes the 5 "harmless" failures) `!pip install -q duckdb==2.0.0`
3. `!cd kit && rm -rf artifacts/maps.* && python3 scripts/build_maps.py`   # ~7 min, writes artifacts/maps.pkl
4. `!cd kit && python3 scripts/sweep.py`   # pick lowest-miss, lowest-cost row
5. Put the winning values into the Dockerfile ENV (SAFETY_LAMBDA, P_HOT), then `time` one run for CPU.
6. Submit: Dockerfile, scheduler.py, selector.py, artifacts/maps.pkl
