#!/usr/bin/env python3
"""v3 Offline map builder: sync ThreadPoolExecutor + exact line_cov + sym_cov + const_refs + fixtures."""
import ast
import gzip
import json
import pickle
import math
import os
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

SQLGLOT_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
WORK_DIR = Path(".build_cache/sqlglot")
OUT_PATH = Path("artifacts/maps.pkl")
CONST_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")

SYNC_WRAPPER = """
import concurrent.futures, pickle, sys

def _rt(x):
    # Emulate the pickling a real worker process does. Expression.__reduce__ calls
    # sqlglot.serde.dump/load, so without this serde is invisible to coverage.
    try:
        return pickle.loads(pickle.dumps(x))
    except Exception:
        return x

class _SyncFuture:
    def __init__(self, fn, *args, **kwargs):
        self._exc = None
        self._res = None
        try:
            args, kwargs = _rt(args), _rt(kwargs)
            self._res = _rt(fn(*args, **kwargs))
        except BaseException as e:
            self._exc = e
    def result(self, timeout=None):
        if self._exc is not None:
            raise self._exc
        return self._res
    def done(self): return True
    def exception(self, timeout=None): return self._exc
    def add_done_callback(self, fn): fn(self)

class _SyncPool:
    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        # test_executor relies on initializer() to set module globals (_schema/_tables)
        if initializer is not None:
            initializer(*initargs)
    def submit(self, fn, *args, **kwargs): return _SyncFuture(fn, *args, **kwargs)
    def map(self, fn, *iterables, **kw):
        return iter([_SyncFuture(fn, *a).result() for a in zip(*iterables)])
    def shutdown(self, wait=True, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

concurrent.futures.ThreadPoolExecutor = _SyncPool
concurrent.futures.ProcessPoolExecutor = _SyncPool
concurrent.futures.wait = lambda fs, **kw: (set(fs), set())
concurrent.futures.as_completed = lambda fs, **kw: iter(list(fs))

import pytest
sys.exit(pytest.main(sys.argv[1:]))
"""


def run(cmd, cwd=None, check=True):
    print(f"+ {' '.join(cmd)}", file=sys.stderr)
    subprocess.run(cmd, cwd=cwd, check=check)


def ensure_sqlglot():
    if not WORK_DIR.exists():
        WORK_DIR.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "https://github.com/tobymao/sqlglot.git", str(WORK_DIR)])
    run(["git", "checkout", "--force", SQLGLOT_COMMIT], cwd=WORK_DIR)


def build_ast_and_const_refs(repo: Path):
    ast_spans = {}
    const_refs = defaultdict(set)
    for py_file in sorted(repo.rglob("*.py")):
        rel = py_file.relative_to(repo).as_posix()
        src = py_file.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()
        spans = []

        def visit(node, prefix=""):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    qname = f"{prefix}.{child.name}" if prefix else child.name
                    start = child.lineno
                    end = getattr(child, "end_lineno", start)
                    spans.append({"name": qname, "start": start, "end": end})
                    if rel.startswith("sqlglot/") and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        body = "\n".join(lines[start - 1 : end])
                        for ident in CONST_RE.findall(body):
                            const_refs[ident].add(f"{rel}::{qname}")
                    visit(child, qname)

        visit(tree)
        ast_spans[rel] = spans

    filtered_refs = {k: sorted(v) for k, v in const_refs.items() if 1 <= len(v) <= 40}
    return ast_spans, filtered_refs


def build_static_import_graph(repo: Path, test_files: dict[str, list[int]]):
    module_to_file = {}
    file_imports = defaultdict(set)
    for py_file in repo.rglob("*.py"):
        rel = py_file.relative_to(repo).as_posix()
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[:-9]
        module_to_file[mod] = rel

    for py_file in repo.rglob("*.py"):
        rel = py_file.relative_to(repo).as_posix()
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    file_imports[rel].add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                file_imports[rel].add(node.module)

    rev = defaultdict(set)
    for src_file, mods in file_imports.items():
        for m in mods:
            parts = m.split(".")
            for i in range(1, len(parts) + 1):
                prefix = ".".join(parts[:i])
                if prefix in module_to_file:
                    rev[module_to_file[prefix]].add(src_file)

    file_to_tests = {}
    for mod_file in module_to_file.values():
        seen, stack, tnums = {mod_file}, [mod_file], set()
        while stack:
            cur = stack.pop()
            if cur in test_files:
                tnums.update(test_files[cur])
            for nxt in rev.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        if tnums:
            file_to_tests[mod_file] = sorted(tnums)
    return file_to_tests


def build_fixture_map(repo: Path, tests_list: list[dict]):
    fixture_map = {}
    fixtures_dir = repo / "tests" / "fixtures"
    if not fixtures_dir.exists():
        return fixture_map
    for fix_file in sorted(fixtures_dir.rglob("*")):
        if not fix_file.is_file():
            continue
        rel = fix_file.relative_to(repo).as_posix()
        stem = fix_file.stem.split("__")[0].lower()
        stem_sing = stem.rstrip("s")
        parent = fix_file.parent.name.lower()
        matched = [
            t["number"]
            for t in tests_list
            if (stem in t["id"].lower() or stem_sing in t["id"].lower())
            and (parent in t["file"].lower() or parent == "fixtures")
        ]
        if not matched:
            matched = [
                t["number"]
                for t in tests_list
                if stem in t["id"].lower() or stem_sing in t["id"].lower()
            ]
        if matched:
            fixture_map[rel] = sorted(set(matched))
    return fixture_map


def collect_coverage(repo: Path, tests_json: Path):
    cov_file = repo / ".coverage"
    if cov_file.exists():
        cov_file.unlink()

    wrapper = repo / "_run_sync_pytest.py"
    wrapper.write_text(SYNC_WRAPPER, encoding="utf-8")
    try:
        tests_data = json.loads(tests_json.read_text(encoding="utf-8"))
        skipped = set(tests_data.get("skipped", []))
        cmd = [
            sys.executable, "_run_sync_pytest.py",
            "--cov=sqlglot",
            "--cov-context=test",
            "-q",
            "--no-header",
            "--tb=no",
        ]
        for s in skipped:
            cmd.extend(["--deselect", s])
        run(cmd, cwd=repo, check=False)
    finally:
        if wrapper.exists():
            wrapper.unlink()

    id_to_num = {t["id"]: t["number"] for t in tests_data["tests"]}
    test_files = defaultdict(list)
    for t in tests_data["tests"]:
        test_files[t["file"]].append(t["number"])

    conn = sqlite3.connect(cov_file)
    cur = conn.cursor()

    ctx_to_num = {}
    for cid, ctx in cur.execute("SELECT id, context FROM context"):
        node_id = ctx.split("|")[0]
        if node_id in id_to_num:
            ctx_to_num[cid] = id_to_num[node_id]

    file_id_to_rel = {}
    repo_prefix = str(repo.resolve()) + os.sep
    for fid, path in cur.execute("SELECT id, path FROM file"):
        if path.startswith(repo_prefix):
            file_id_to_rel[fid] = path[len(repo_prefix):].replace("\\", "/")

    ast_spans, const_refs = build_ast_and_const_refs(repo)
    file_cov = defaultdict(set)
    sym_cov = defaultdict(set)
    line_cov = defaultdict(lambda: defaultdict(set))

    line_to_sym = {}
    for rel, spans in ast_spans.items():
        lmap = {}
        for sp in sorted(spans, key=lambda s: (s["end"] - s["start"]), reverse=True):
            for ln in range(sp["start"], sp["end"] + 1):
                lmap[ln] = sp["name"]
        line_to_sym[rel] = lmap

    for fid, cid, numbits in cur.execute("SELECT file_id, context_id, numbits FROM line_bits"):
        if fid not in file_id_to_rel or cid not in ctx_to_num:
            continue
        rel = file_id_to_rel[fid]
        tnum = ctx_to_num[cid]
        file_cov[rel].add(tnum)
        lmap = line_to_sym.get(rel, {})
        lcov_rel = line_cov[rel]
        for byte_idx, byte_val in enumerate(numbits):
            if byte_val == 0:
                continue
            for bit in range(8):
                if byte_val & (1 << bit):
                    ln = byte_idx * 8 + bit
                    lcov_rel[ln].add(tnum)
                    sym = lmap.get(ln)
                    if sym:
                        sym_cov[f"{rel}::{sym}"].add(tnum)

    conn.close()

    n_tests = len(tests_data["tests"])
    sym_idf = {
        k: round(math.log((n_tests + 1) / (len(v) + 1)) + 1.0, 4)
        for k, v in sym_cov.items()
    }

    # Interned line_cov: identical test-sets are stored once. {file: {line: set_id}}
    set_ids, sets, compact_line_cov = {}, [], {}
    for rel, ldict in line_cov.items():
        sub = {}
        for ln, ts in ldict.items():
            key = tuple(sorted(ts))
            sid = set_ids.get(key)
            if sid is None:
                sid = set_ids[key] = len(sets)
                sets.append(key)
            sub[ln] = sid
        if sub:
            compact_line_cov[rel] = sub

    payload = {
        "num_tests": n_tests,
        "test_files": dict(test_files),
        "fixture_map": build_fixture_map(repo, tests_data["tests"]),
        "ast_spans": ast_spans,
        "const_refs": const_refs,
        "import_graph": build_static_import_graph(repo, test_files),
        "file_cov": {k: sorted(v) for k, v in file_cov.items()},
        "sym_cov": {k: sorted(v) for k, v in sym_cov.items()},
        "sym_idf": sym_idf,
        "line_sets": sets,
        "line_cov": compact_line_cov,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "wb") as f:
        pickle.dump(payload, f, protocol=4)  # protocol 4: loads on py3.8+ (Docker uses 3.11)

    size_mb = OUT_PATH.stat().st_size / (1024 * 1024)
    print(
        f"Wrote {OUT_PATH} ({size_mb:.2f} MB) | mapped {len(ctx_to_num)}/{n_tests} tests | "
        f"fixtures={len(payload['fixture_map'])} | const_refs={len(const_refs)} | "
        f"line_cov_files={len(compact_line_cov)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    ensure_sqlglot()
    collect_coverage(WORK_DIR, Path("sample/tests.json"))
