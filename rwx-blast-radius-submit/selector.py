#!/usr/bin/env python3
"""v3 Competitive Blast Radius selector — pure Python stdlib, line+symbol coverage."""
from __future__ import annotations

import gzip
import json
import pickle
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from scheduler import decide, lpt_schedule

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MAPS_PATH = Path(os.environ.get("MAPS_PATH", "/app/artifacts/maps.pkl"))
P_HOT = float(os.environ.get("P_HOT", "0.05"))  # edited line hit by >300 tests
SAFETY_LAMBDA = float(os.environ.get("SAFETY_LAMBDA", "1.5"))

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
CONST_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
PRAGMA_RE = re.compile(r"(type:|noqa|pragma|coding[:=])", re.IGNORECASE)


def parse_patch(patch_text: str) -> dict[str, dict]:
    """Parse unified diff into exact edited old-file line numbers and added/removed lines."""
    files: dict[str, dict] = {}
    cur_file = None
    old_ln = 0
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            cur_file = line[6:].strip()
            files.setdefault(cur_file, {"edited_lines": set(), "hunk_lines": set(), "added": [], "removed": [], "binary": False})
        elif line.startswith("Binary files ") and cur_file:
            files[cur_file]["binary"] = True
        elif cur_file:
            m = HUNK_RE.match(line)
            if m:
                old_ln = int(m.group(1))
                old_len = int(m.group(2) or "1")
                for ln in range(old_ln, old_ln + max(old_len, 1)):
                    files[cur_file]["hunk_lines"].add(ln)
            elif line.startswith("+") and not line.startswith("+++"):
                files[cur_file]["added"].append(line[1:])
                files[cur_file]["edited_lines"].add(old_ln)
            elif line.startswith("-") and not line.startswith("---"):
                files[cur_file]["removed"].append(line[1:])
                files[cur_file]["edited_lines"].add(old_ln)
                old_ln += 1
            elif line.startswith(" "):
                old_ln += 1
    return files


def is_pure_safe_comment_edit(info: dict) -> bool:
    """True only if all added/removed lines are blank or plain # comments without pragmas."""
    changed = info["added"] + info["removed"]
    if not changed:
        return True
    for ln in changed:
        s = ln.strip()
        if not s:
            continue
        if not s.startswith("#") or PRAGMA_RE.search(s):
            return False
    return True


def score_commit(patch_text: str, maps: dict, dur_map: dict[int, int]) -> list[list[int]]:
    if not patch_text.strip():
        return []
    parsed = parse_patch(patch_text)
    if not parsed:
        return []

    ast_spans = maps["ast_spans"]
    sym_cov = maps["sym_cov"]
    sym_idf = maps["sym_idf"]
    file_cov = maps["file_cov"]
    line_cov = maps.get("line_cov", {})
    line_sets = maps.get("line_sets", [])
    import_graph = maps["import_graph"]
    test_files = maps["test_files"]
    fixture_map = maps.get("fixture_map", {})
    const_refs = maps.get("const_refs", {})

    probs: dict[int, float] = defaultdict(float)

    for rel_path, info in parsed.items():
        # 1. Fixture file edit
        if rel_path in fixture_map:
            for t in fixture_map[rel_path]:
                probs[t] = max(probs[t], 0.95)
            continue

        # 2. Test file edit
        if rel_path in test_files:
            for t in test_files[rel_path]:
                probs[t] = max(probs[t], 0.90)
            continue

        # 3. Pure safe # comment / whitespace edit (still checked for pragmas)
        if rel_path.endswith(".py") and is_pure_safe_comment_edit(info):
            continue

        # 4. Exact line-level coverage on edited lines (+/- 1 line for branch/multiline edits)
        lcov_file = line_cov.get(rel_path, {})
        hit_line_tests = set()
        for ln in info["edited_lines"]:
            for delta in (0, -1, 1):
                sid = lcov_file.get(ln + delta)
                if sid is not None:
                    hit_line_tests.update(line_sets[sid])

        if hit_line_tests:
            n_hit = len(hit_line_tests)
            p_line = 0.35 if n_hit <= 40 else (0.12 if n_hit <= 300 else P_HOT)
            for t in hit_line_tests:
                probs[t] = max(probs[t], p_line)

        # 5. Enclosing AST symbols + edited UPPER_CASE constant references
        matched_syms = set()
        for sp in ast_spans.get(rel_path, ()):
            if any(sp["start"] <= ln <= sp["end"] for ln in info["edited_lines"]):
                matched_syms.add(f"{rel_path}::{sp['name']}")

        changed_text = "\n".join(info["added"] + info["removed"])
        for ident in CONST_RE.findall(changed_text):
            for ref_sym in const_refs.get(ident, ()):
                matched_syms.add(ref_sym)

        hit_any_sym = False
        for sym_key in sorted(matched_syms):
            tlist = sym_cov.get(sym_key)
            if tlist:
                hit_any_sym = True
                idf = sym_idf.get(sym_key, 1.0)
                # If we already have tight line_cov, give sym_cov a lighter backup weight
                base = 0.03 if hit_line_tests else 0.08
                p = min(0.85, base * idf)
                for t in tlist:
                    probs[t] = 1.0 - (1.0 - probs[t]) * (1.0 - p)

        # 6. Fallback for module-level edits, unseen files, or binary hunks
        if not hit_line_tests and not hit_any_sym:
            if rel_path in file_cov:
                for t in file_cov[rel_path]:
                    probs[t] = max(probs[t], 0.15)
            elif rel_path in import_graph:
                for t in import_graph[rel_path]:
                    probs[t] = max(probs[t], 0.20)
            elif rel_path.endswith(".py"):
                for t in dur_map:
                    probs[t] = max(probs[t], 0.05)

    return decide(probs, dur_map, safety_lambda=SAFETY_LAMBDA)


def safe_fallback(dur_map: dict[int, int]) -> list[list[int]]:
    workers, _ = lpt_schedule(list(dur_map.items()), 8)
    return workers


def main():
    tests_data = json.loads((DATA_DIR / "tests.json").read_text(encoding="utf-8"))
    dur_map = {t["number"]: round(t["seconds"] * 1000) for t in tests_data["tests"]}

    maps = None
    if MAPS_PATH.exists():
        if MAPS_PATH.suffix == ".pkl":
            with open(MAPS_PATH, "rb") as f:
                loaded = pickle.load(f)
        else:
            with gzip.open(MAPS_PATH, "rt", encoding="utf-8") as f:
                loaded = json.load(f)
        if loaded.get("num_tests") == len(dur_map):
            maps = loaded
        else:
            print("WARNING: artifact test count mismatch; using fallback", file=sys.stderr)

    commits_dir = DATA_DIR / "commits"
    for patch_file in sorted(commits_dir.glob("*.patch")):
        cid = patch_file.stem
        try:
            if maps is None:
                workers = safe_fallback(dur_map)
            else:
                patch_text = patch_file.read_text(encoding="utf-8", errors="replace")
                workers = score_commit(patch_text, maps, dur_map)
        except Exception as exc:
            print(f"ERROR on commit {cid}: {exc}", file=sys.stderr)
            workers = safe_fallback(dur_map)

        sys.stdout.write(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
