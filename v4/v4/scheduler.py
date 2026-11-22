"""Expected-cost minimization scheduler (exact whole-ms arithmetic per SCORING.md)."""
from __future__ import annotations

MISS_PENALTY_MS = 300_000
WORKER_COST_MS = 2_000
MAX_WORKERS = 8


def lpt_schedule(tests: list[tuple[int, int]], k: int) -> tuple[list[list[int]], int]:
    if not tests or k <= 0:
        return [], 0
    k = min(k, len(tests), MAX_WORKERS)
    workers: list[list[int]] = [[] for _ in range(k)]
    loads = [0] * k
    for tnum, dur in sorted(tests, key=lambda x: x[1], reverse=True):
        idx = min(range(k), key=loads.__getitem__)
        workers[idx].append(tnum)
        loads[idx] += dur
    non_empty = [w for w in workers if w]
    makespan = max(loads) if loads else 0
    return non_empty, makespan


def expected_miss_prob(excluded_probs: list[float]) -> float:
    surv = 1.0
    for p in excluded_probs:
        surv *= (1.0 - max(0.0, min(1.0, p)))
    return 1.0 - surv


def decide(
    probs: dict[int, float],
    durations_ms: dict[int, int],
    safety_lambda: float = 1.5,
) -> list[list[int]]:
    scaled = {
        t: min(0.9999, p * safety_lambda)
        for t, p in probs.items()
        if p > 1e-6 and t in durations_ms
    }
    if not scaled:
        return []

    ranked = sorted(
        scaled.keys(),
        key=lambda t: scaled[t] / max(durations_ms[t], 1),
        reverse=True,
    )

    best_cost = MISS_PENALTY_MS * expected_miss_prob(list(scaled.values()))
    best_workers: list[list[int]] = []

    n = len(ranked)
    suffix_surv = [1.0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_surv[i] = suffix_surv[i + 1] * (1.0 - scaled[ranked[i]])

    cutoffs = set()
    for i, t in enumerate(ranked):
        if scaled[t] * MISS_PENALTY_MS >= durations_ms[t] / MAX_WORKERS:
            cutoffs.add(i + 1)
    cutoffs.add(n)

    for m in sorted(cutoffs):
        selected = [(t, durations_ms[t]) for t in ranked[:m]]
        p_miss = 1.0 - suffix_surv[m]
        miss_cost = MISS_PENALTY_MS * p_miss
        if miss_cost >= best_cost:
            continue

        max_d = max(d for _, d in selected)
        total_d = sum(d for _, d in selected)
        k_max = min(MAX_WORKERS, m, max(1, (total_d + max_d - 1) // max(max_d, 1)))
        for k in range(1, k_max + 1):
            workers, makespan = lpt_schedule(selected, k)
            cost = makespan + WORKER_COST_MS * len(workers) + miss_cost
            if cost < best_cost:
                best_cost = cost
                best_workers = workers

    return best_workers
