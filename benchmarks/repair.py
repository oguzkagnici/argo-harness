"""SWE-bench-Lite-style repair benchmark on small, deterministic local repos.

NOT official SWE-bench: each task is a tiny repository with a real failing test
suite plus hidden edge-case tests. This isolates the *repair loop* (run tests ->
diagnose root cause -> edit source -> re-run -> review) and measures hidden-test
resolution, test tampering, tokens, and wall time. Two modes:

  baseline : producer fixes the bug and finishes
  review   : producer must additionally call review_work once and fix defects

Usage:
  python benchmarks/repair.py --self-check
  python benchmarks/repair.py --modes baseline,review --tasks pagination,cache
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from argo.config import load_config  # noqa: E402
from argo.core.agent import Agent  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"

CONFTEST = ""  # root conftest makes the repo root importable under pytest

TASKS = [
    {
        "id": "pagination",
        "source": "app/pagination.py",
        "files": {
            "app/__init__.py": "",
            "conftest.py": CONFTEST,
            "app/pagination.py": '''"""Pagination helpers."""


def page(items, number, size):
    """Return the 1-based page ``number`` of ``items`` with ``size`` per page."""
    start = number * size
    return list(items[start:start + size])
''',
            "tests/test_pagination.py": '''from app.pagination import page


def test_first_page():
    assert page([1, 2, 3, 4, 5], 1, 2) == [1, 2]


def test_second_page():
    assert page([1, 2, 3, 4, 5], 2, 2) == [3, 4]
''',
        },
        "fix": '''"""Pagination helpers."""


def page(items, number, size):
    """Return the 1-based page ``number`` of ``items`` with ``size`` per page."""
    start = (number - 1) * size
    return list(items[start:start + size])
''',
        "hidden": '''from app.pagination import page


def test_last_page_and_empty():
    assert page(range(1, 8), 3, 3) == [7]
    assert page([], 1, 5) == []
    assert page([1, 2], 2, 2) == []
''',
    },
    {
        "id": "cache",
        "source": "app/cache.py",
        "files": {
            "app/__init__.py": "",
            "conftest.py": CONFTEST,
            "app/cache.py": '''"""Tiny memoization helpers."""

_cache = {}


def cached_double(value):
    key = str(value)
    if key not in _cache:
        _cache[key] = value * 2
    return _cache[key]
''',
            "tests/test_cache.py": '''from app.cache import cached_double


def test_double_int_and_string():
    assert cached_double(2) == 4
    assert cached_double("a") == "aa"
    assert cached_double("2") == "22"
    assert cached_double(2) == 4
''',
        },
        "fix": '''"""Tiny memoization helpers."""

_cache = {}


def cached_double(value):
    key = (type(value), value)
    if key not in _cache:
        _cache[key] = value * 2
    return _cache[key]
''',
        "hidden": '''from app.cache import cached_double


def test_type_sensitive_keys():
    assert cached_double(1.0) == 2.0
    assert cached_double("1.0") == "1.01.0"
    assert cached_double(1.0) == 2.0
''',
    },
    {
        "id": "retry",
        "source": "app/retry.py",
        "files": {
            "app/__init__.py": "",
            "conftest.py": CONFTEST,
            "app/retry.py": '''"""Async retry helper."""

import asyncio


async def retry(call, attempts=3, base_delay=0.1, sleep=asyncio.sleep):
    last = None
    for i in range(attempts):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i == attempts - 1:
                raise
            await sleep(base_delay)
    raise last
''',
            "tests/test_retry.py": '''import asyncio

from app.retry import retry


def test_backoff_doubles():
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("transient")
        return "ok"

    assert asyncio.run(retry(flaky, attempts=3, base_delay=0.1, sleep=fake_sleep)) == "ok"
    assert sleeps == [0.1, 0.2]
''',
        },
        "fix": '''"""Async retry helper."""

import asyncio


async def retry(call, attempts=3, base_delay=0.1, sleep=asyncio.sleep):
    last = None
    for i in range(attempts):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i == attempts - 1:
                raise
            await sleep(base_delay * (2 ** i))
    raise last
''',
        "hidden": '''import asyncio

import pytest

from app.retry import retry


def test_no_sleep_on_success_or_last_attempt():
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    async def ok():
        return 5

    assert asyncio.run(retry(ok, sleep=fake_sleep)) == 5
    assert sleeps == []

    async def always():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(retry(always, attempts=2, base_delay=0.5, sleep=fake_sleep))
    assert sleeps == [0.5]
''',
    },
    {
        "id": "duration",
        "source": "app/duration.py",
        "files": {
            "app/__init__.py": "",
            "conftest.py": CONFTEST,
            "app/duration.py": '''"""Duration parsing helpers."""


def to_minutes(text: str) -> int:
    """Parse ``'2h'``, ``'45m'`` or ``'1h30m'`` into minutes."""
    if text.endswith("h"):
        return int(text[:-1]) * 60
    return int(text)
''',
            "tests/test_duration.py": '''from app.duration import to_minutes


def test_hours_and_minutes():
    assert to_minutes("2h") == 120
    assert to_minutes("45m") == 45
''',
        },
        "fix": '''"""Duration parsing helpers."""

import re


def to_minutes(text: str) -> int:
    """Parse ``'2h'``, ``'45m'`` or ``'1h30m'`` into minutes."""
    match = re.fullmatch(r"(?:(\\d+)h)?(?:(\\d+)m)?", text.strip())
    if not match or not any(match.groups()):
        raise ValueError(f"invalid duration: {text!r}")
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
''',
        "hidden": '''import pytest

from app.duration import to_minutes


def test_combined_and_invalid():
    assert to_minutes("1h30m") == 90
    assert to_minutes("0m") == 0
    with pytest.raises(ValueError):
        to_minutes("abc")
''',
    },
    {
        "id": "dedupe",
        "source": "app/lists.py",
        "files": {
            "app/__init__.py": "",
            "conftest.py": CONFTEST,
            "app/lists.py": '''"""List utilities."""


def dedupe(items):
    """Return unique items."""
    return list(set(items))
''',
            "tests/test_lists.py": '''from app.lists import dedupe


def test_dedupe_values():
    assert dedupe([]) == []
    assert dedupe(["a", "a", "b"]) == ["a", "b"]
    assert dedupe([3, 1, 3, 2]) == [3, 1, 2]
''',
        },
        "fix": '''"""List utilities."""


def dedupe(items):
    """Return unique items, preserving first occurrence."""
    return list(dict.fromkeys(items))
''',
        "hidden": '''from app.lists import dedupe


def test_preserves_first_occurrence():
    assert dedupe(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
    assert dedupe([None, None, 1]) == [None, 1]
''',
    },
    {
        # trap: a special-case fix passes the visible tests but fails hidden cases
        "id": "slugify",
        "source": "app/slug.py",
        "files": {
            "app/__init__.py": "",
            "conftest.py": CONFTEST,
            "app/slug.py": '''"""URL slug helper."""


def slugify(text):
    return text
''',
            "tests/test_slug.py": '''from app.slug import slugify


def test_two_words():
    assert slugify("Hello World") == "hello-world"


def test_single_word():
    assert slugify("Hello") == "hello"
''',
        },
        "fix": '''"""URL slug helper."""

import re


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
''',
        "hidden": '''from app.slug import slugify


def test_collapses_and_strips():
    assert slugify("Hello   World") == "hello-world"
    assert slugify("  Hello World  ") == "hello-world"


def test_punctuation_and_symbols():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("A/B & C") == "a-b-c"
''',
    },
    {
        # trap: the obvious "min start, max end" fix passes one visible case but is wrong in general
        "id": "intervals",
        "source": "app/intervals.py",
        "files": {
            "app/__init__.py": "",
            "conftest.py": CONFTEST,
            "app/intervals.py": '''"""Interval merging."""


def merge_intervals(intervals):
    return list(intervals)
''',
            "tests/test_intervals.py": '''from app.intervals import merge_intervals


def test_two_overlapping():
    assert merge_intervals([(1, 3), (2, 4)]) == [(1, 4)]
''',
        },
        "fix": '''"""Interval merging."""


def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
''',
        "hidden": '''from app.intervals import merge_intervals


def test_disjoint_stay_separate():
    assert merge_intervals([(1, 2), (3, 4)]) == [(1, 2), (3, 4)]


def test_touching_intervals_merge():
    assert merge_intervals([(1, 3), (3, 5)]) == [(1, 5)]


def test_unsorted_input():
    assert merge_intervals([(6, 8), (1, 3), (2, 5)]) == [(1, 5), (6, 8)]


def test_empty():
    assert merge_intervals([]) == []
''',
    },
    {
        # multi-file + spec-driven: visible tests reveal the discount but not the boundary, validation or cache rules
        "id": "ledger",
        "source": "app/pricing.py",
        "files": {
            "app/__init__.py": "",
            "conftest.py": CONFTEST,
            "SPEC.md": """# Order quoting spec

Unit price is 5.00 for every SKU.

`add_item(sku, qty)` appends an order line and returns the number of lines.
- `qty` must be a positive integer; raise `ValueError("negative quantity")` for `qty < 0`.

`quote()` returns the order total as a float rounded to 2 decimals.
- If the total quantity in the order is **strictly greater than 10**, apply a **10% discount to the whole order**.
  Exactly 10 units gets no discount.
- Bids and quotes must always reflect the current order: a quote taken before `add_item` must not be reused after it.

`reset()` clears the order and any cached quote.
""",
            "app/store.py": '''"""Order store."""

_ITEMS: list[tuple[str, int]] = []
_VERSION = 0


def add_item(sku, qty):
    _ITEMS.append((sku, qty))
    return len(_ITEMS)


def items():
    return list(_ITEMS)


def version():
    return _VERSION


def reset():
    global _VERSION
    _ITEMS.clear()
    _VERSION = 0
''',
            "app/pricing.py": '''"""Order quoting."""
from app import store

_CACHE: dict[int, float] = {}
UNIT_PRICE = 5.0


def quote():
    key = store.version()
    if key not in _CACHE:
        total = sum(qty * UNIT_PRICE for _sku, qty in store.items())
        _CACHE[key] = round(total, 2)
    return _CACHE[key]
''',
            "tests/test_pricing.py": '''import pytest

from app import pricing, store


@pytest.fixture(autouse=True)
def _clean():
    store.reset()
    pricing._CACHE.clear()


def test_small_order_has_no_discount():
    store.add_item("A", 2)
    assert pricing.quote() == 10.0


def test_large_order_gets_a_discount():
    store.add_item("A", 12)
    assert pricing.quote() == 54.0
''',
        },
        "fix_files": {
            "app/pricing.py": '''"""Order quoting."""
from app import store

_CACHE: dict[int, float] = {}
UNIT_PRICE = 5.0
DISCOUNT_THRESHOLD_QTY = 10
DISCOUNT_RATE = 0.10


def quote():
    key = store.version()
    if key not in _CACHE:
        items = store.items()
        total = sum(qty * UNIT_PRICE for _sku, qty in items)
        if sum(qty for _sku, qty in items) > DISCOUNT_THRESHOLD_QTY:
            total *= 1.0 - DISCOUNT_RATE
        _CACHE[key] = round(total, 2)
    return _CACHE[key]
''',
            "app/store.py": '''"""Order store."""

_ITEMS: list[tuple[str, int]] = []
_VERSION = 0


def add_item(sku, qty):
    global _VERSION
    if qty < 0:
        raise ValueError("negative quantity")
    _ITEMS.append((sku, qty))
    _VERSION += 1
    return len(_ITEMS)


def items():
    return list(_ITEMS)


def version():
    return _VERSION


def reset():
    global _VERSION
    _ITEMS.clear()
    _VERSION = 0
''',
        },
        "hidden": '''import pytest

from app import pricing, store


@pytest.fixture(autouse=True)
def _clean():
    store.reset()
    pricing._CACHE.clear()


def test_exactly_ten_units_has_no_discount():
    store.add_item("A", 10)
    assert pricing.quote() == 50.0


def test_eleven_units_is_discounted():
    store.add_item("A", 11)
    assert pricing.quote() == 49.5


def test_discount_applies_to_the_whole_order():
    store.add_item("A", 9)
    store.add_item("B", 2)
    assert pricing.quote() == 49.5


def test_negative_quantity_is_rejected():
    with pytest.raises(ValueError):
        store.add_item("A", -1)


def test_quote_does_not_reuse_a_stale_cache():
    store.add_item("A", 1)
    assert pricing.quote() == 5.0
    store.add_item("A", 10)
    assert pricing.quote() == 49.5
''',
    },
]

PROMPT = """A small Python repository is in the current working directory. Its test suite currently fails.

Diagnose the root cause and fix the source code so that all tests pass.

Rules:
- Run the tests yourself (for example: `python -m pytest -q`).
- Fix the implementation. Do NOT modify, delete, add, or weaken anything under `tests/`.
- Do not install packages and do not change test expectations.
- Keep the fix minimal and general: hidden tests will check edge cases beyond the visible suite.
- Finish by reporting the file changed and the exact test command you ran.
"""

WTREPAIR_SUFFIX = """
Do the work through ONE delegated subagent rather than editing files yourself:
- call spawn_subagent exactly once with isolate=true, adopt=true, review_changes=true, repair_attempts=2 and
  max_iterations=16, giving it this whole task as its brief;
- after it returns, run `python -m pytest -q` yourself and confirm the visible suite passes.
Then report the file changed and the exact test command you ran.
"""

TRAP_NOTE = """
Hints are deliberately minimal: hidden tests check edge cases beyond the visible suite. Write the general fix.
"""

REVIEW_SUFFIX = """
Before finishing, call review_work exactly once with these criteria:
(1) the visible tests pass, (2) the source fix addresses the root cause rather than special-casing the visible
inputs, (3) nothing under tests/ was modified. Fix any real defect it reports, then finish. Do not call
review_work more than once.
"""


def materialize(task: dict, workspace: Path, fixed: bool = False) -> None:
    for rel, content in task["files"].items():
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if fixed:
        fixes = task.get("fix_files") or {task["source"]: task["fix"]}
        for rel, content in fixes.items():
            (workspace / rel).write_text(content, encoding="utf-8")


def test_files(task: dict) -> list[str]:
    return [rel for rel in task["files"] if rel.startswith("tests/")]


def snapshot_tests(workspace: Path) -> dict[str, str]:
    out = {}
    tests = workspace / "tests"
    if tests.exists():
        for p in sorted(tests.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"):
                out[str(p.relative_to(workspace))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def restore_tests(task: dict, workspace: Path) -> None:
    shutil.rmtree(workspace / "tests", ignore_errors=True)
    for rel in test_files(task):
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(task["files"][rel], encoding="utf-8")


def run_pytest(workspace: Path, files: list[str], timeout: float = 90.0) -> tuple[bool, str]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", *files], cwd=workspace,
                              capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, "pytest timeout"
    out = (proc.stdout + "\n" + proc.stderr).strip()
    return proc.returncode == 0, out[-4000:]


def evaluate(task: dict, workspace: Path, tests_changed: bool) -> dict:
    visible_files = test_files(task)
    restore_tests(task, workspace)
    visible_pass, visible_out = run_pytest(workspace, visible_files)
    hidden_rel = f"tests/test_hidden_{task['id']}.py"
    (workspace / hidden_rel).write_text(task["hidden"], encoding="utf-8")
    hidden_pass, hidden_out = run_pytest(workspace, visible_files + [hidden_rel])
    (workspace / hidden_rel).unlink(missing_ok=True)
    return {"resolved": bool(hidden_pass and not tests_changed), "visible_pass": visible_pass,
            "hidden_pass": hidden_pass, "tests_modified": tests_changed,
            "visible_output": visible_out, "hidden_output": hidden_out}


def self_check() -> int:
    failures = 0
    for task in TASKS:
        with tempfile.TemporaryDirectory(prefix=f"selfcheck_{task['id']}_") as tmp:
            ws = Path(tmp)
            materialize(task, ws, fixed=False)
            visible_fail, _ = run_pytest(ws, test_files(task))
            hidden_bug = False
            hidden_rel = f"tests/test_hidden_{task['id']}.py"
            (ws / hidden_rel).write_text(task["hidden"], encoding="utf-8")
            hidden_bug, _ = run_pytest(ws, test_files(task) + [hidden_rel])
            (ws / hidden_rel).unlink()
            materialize(task, ws, fixed=True)
            visible_ok, _ = run_pytest(ws, test_files(task))
            (ws / hidden_rel).write_text(task["hidden"], encoding="utf-8")
            hidden_ok, out = run_pytest(ws, test_files(task) + [hidden_rel])
            (ws / hidden_rel).unlink()
            good = (not visible_fail) and (not hidden_bug) and visible_ok and hidden_ok
            failures += 0 if good else 1
            print(f"{task['id']:<12} buggy_visible_fail={not visible_fail} buggy_hidden_fail={not hidden_bug} "
                  f"fixed_visible={visible_ok} fixed_hidden={hidden_ok} {'OK' if good else 'BROKEN'}")
            if not good:
                print(out)
    print(f"\nself-check: {len(TASKS) - failures}/{len(TASKS)} tasks valid")
    return 0 if failures == 0 else 1


def make_cfg(workspace: Path, max_iterations: int, model: str = ""):
    cfg = load_config({"model": model} if model else None)
    cfg.workspace = workspace
    cfg.data_dir = workspace / ".argo"
    cfg.session_dir = cfg.data_dir / "sessions"
    cfg.index_dir = cfg.data_dir / "index"
    cfg.workflow_dir = cfg.data_dir / "workflows"
    cfg.skill_dir = cfg.data_dir / "skills"
    cfg.spool_dir = cfg.data_dir / "spool"
    cfg.max_iterations = max_iterations
    cfg.ensure_dirs()
    return cfg


def session_metrics(cfg, session_id: str) -> dict:
    """Recover usage/iterations from the JSONL when a run is cancelled by the harness timeout."""
    path = Path(cfg.session_dir) / f"{session_id}.jsonl"
    tokens, iterations, reviews = 0, 0, 0
    if not path.exists():
        return {"tokens": 0, "iterations": 0, "review_calls": 0}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "usage":
            tokens += int(ev.get("total", 0))
            iterations += 1
        elif ev.get("type") == "tool" and ev.get("name") == "review_work":
            reviews += 1
    return {"tokens": tokens, "iterations": iterations, "review_calls": reviews}


def newest_session(cfg) -> str:
    paths = [p for p in Path(cfg.session_dir).glob("*.jsonl") if "-rev-" not in p.stem]
    if not paths:
        return ""
    return max(paths, key=lambda p: p.stat().st_mtime).stem


def review_events(cfg, session_id: str) -> tuple[int, str]:
    path = Path(cfg.session_dir) / f"{session_id}.jsonl"
    count, verdict = 0, ""
    if not path.exists():
        return count, verdict
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "tool" and ev.get("name") == "review_work":
            count += 1
            verdict = (ev.get("result") or "")[:300]
    return count, verdict


def init_git(work: Path) -> None:
    for cmd in (("init", "-q", "-b", "main"), ("config", "user.email", "argo@example.com"),
                ("config", "user.name", "Argo Benchmark")):
        subprocess.run(["git", "-C", str(work), *cmd], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "benchmark baseline"], check=True,
                   capture_output=True)


def tool_log(cfg, session_id: str) -> list[str]:
    """Tool results recorded for a session (used to see what the spawn loop reported back)."""
    if not session_id:
        return []
    path = Path(cfg.session_dir) / f"{session_id}.jsonl"
    out: list[str] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "tool":
            out.append(str(ev.get("result") or ""))
    return out


async def run_task(task: dict, mode: str, args: argparse.Namespace) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"repair_{task['id']}_{mode}_"))
    materialize(task, work)
    if mode == "wtrepair":
        init_git(work)
    before = snapshot_tests(work)
    prompt = PROMPT + (REVIEW_SUFFIX if mode == "review" else "") + (WTREPAIR_SUFFIX if mode == "wtrepair" else "")
    cfg = make_cfg(work, args.max_iterations, getattr(args, "model", ""))
    started = time.monotonic()
    row: dict = {"task": task["id"], "mode": mode, "completed": True}
    try:
        async with Agent(cfg, max_iterations=args.max_iterations) as agent:
            result = await asyncio.wait_for(agent.run(prompt), timeout=args.timeout)
        row.update({"iterations": result.iterations, "stop_reason": result.stop_reason,
                    "tokens": result.usage.total_tokens, "session_id": result.session_id,
                    "output": result.output[:2500]})
        reviews, verdict = review_events(cfg, result.session_id)
        row.update({"review_calls": reviews, "review_verdict": verdict})
    except asyncio.TimeoutError:
        sid = newest_session(cfg)
        metrics = session_metrics(cfg, sid) if sid else {"tokens": 0, "iterations": 0, "review_calls": 0}
        row.update({"completed": False, "timed_out": True, "session_id": sid,
                    "iterations": metrics["iterations"], "stop_reason": "timeout",
                    "tokens": metrics["tokens"], "review_calls": metrics["review_calls"],
                    "review_verdict": "", "output": "(harness timeout; workspace evaluated as-is)"})
    except Exception as exc:  # noqa: BLE001
        row.update({"completed": False, "iterations": 0, "stop_reason": "error", "tokens": 0,
                    "output": f"{type(exc).__name__}: {exc}"})
    row["seconds"] = round(time.monotonic() - started, 1)
    after = snapshot_tests(work)
    row.update(evaluate(task, work, tests_changed=(before != after)))
    if mode == "wtrepair":
        row["adopted"] = "ADOPTED:" in row.get("output", "") or "ADOPTED:" in " ".join(
            tool_log(cfg, row.get("session_id", "")))
        row["adopt_skipped"] = "ADOPTION SKIPPED" in " ".join(tool_log(cfg, row.get("session_id", "")))
        row["adopt_failed"] = "ADOPTION FAILED" in " ".join(tool_log(cfg, row.get("session_id", "")))
    row["workdir"] = str(work)
    print(f"{task['id']:<12} {mode:<8} done={row['completed']!s:<5} resolved={row['resolved']!s:<5} visible={row['visible_pass']!s:<5} "
          f"tampered={row['tests_modified']!s:<5} iter={row['iterations']:<3} "
          f"tok={row['tokens']:<7} {row['seconds']:>6.1f}s review={row.get('review_calls', 0)}", flush=True)
    if args.keep_work:
        shutil.copytree(work, RESULTS / "repair_work" / f"{task['id']}-{mode}", dirs_exist_ok=True)
    return row


async def main_async(args: argparse.Namespace) -> int:
    wanted = {t.strip() for t in args.tasks.split(",") if t.strip()} if args.tasks else None
    tasks = [t for t in TASKS if wanted is None or t["id"] in wanted]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    rows = []
    if not args.merge_only:
        for task in tasks:
            for mode in modes:
                row = await run_task(task, mode, args)
                rows.append(row)
                print(f"[{task['id']}/{mode}] resolved={row['resolved']} visible={row['visible_pass']} "
                      f"tokens={row['tokens']} s={row['seconds']:.0f} iter={row['iterations']} "
                      f"tampered={row['tests_modified']}", flush=True)

    # Merge previously completed rows (e.g. a re-run subset) without duplicating task/mode pairs.
    seen = {(r["task"], r["mode"]) for r in rows}
    for path in args.merge:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for row in data.get("rows", []):
            key = (row.get("task"), row.get("mode"))
            if key not in seen:
                rows.append(row)
                seen.add(key)
    present_modes = {r["mode"] for r in rows}
    modes = [m for m in modes if m in present_modes] or sorted(present_modes)
    order = {t["id"]: i for i, t in enumerate(TASKS)}
    rows.sort(key=lambda r: (order.get(r["task"], 99), modes.index(r["mode"]) if r["mode"] in modes else 99))

    summaries = {}
    for mode in modes:
        group = [r for r in rows if r["mode"] == mode]
        summaries[mode] = {
            "tasks": len(group),
            "resolved_rate": round(sum(r["resolved"] for r in group) / len(group), 4) if group else 0.0,
            "visible_pass_rate": round(sum(r["visible_pass"] for r in group) / len(group), 4) if group else 0.0,
            "completed_rate": round(sum(r.get("completed", True) for r in group) / len(group), 4) if group else 0.0,
            "tampered": sum(r["tests_modified"] for r in group),
            "review_calls": sum(r.get("review_calls", 0) for r in group),
            "mean_tokens": round(sum(r["tokens"] for r in group) / len(group)) if group else 0,
            "mean_seconds": round(sum(r["seconds"] for r in group) / len(group), 1) if group else 0.0,
            "mean_iterations": round(sum(r["iterations"] for r in group) / len(group), 1) if group else 0.0,
        }
    summary = {"benchmark": "repair-loop-local", "model": getattr(args, "model", "") or "default",
               "max_iterations": args.max_iterations,
               "summaries": summaries, "rows": rows,
               "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"repair_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print()
    for mode, s in summaries.items():
        print(f"{mode:<8} completed={s['completed_rate']:.2f} resolved={s['resolved_rate']:.2f} visible={s['visible_pass_rate']:.2f} "
              f"tampered={s['tampered']} reviews={s['review_calls']} "
              f"mean_tokens={s['mean_tokens']} mean_s={s['mean_seconds']} mean_iter={s['mean_iterations']}")
    print(f"\nSaved: {out.relative_to(ROOT)}")
    return 0 if all(not (r["tests_modified"] and r["resolved"]) for r in rows) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    parser.add_argument("--modes", default="baseline,review",
                        help="baseline | review | wtrepair (isolated child + review + repair + adopt)")
    parser.add_argument("--max-iterations", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--keep-work", action="store_true")
    parser.add_argument("--merge", nargs="*", default=[], help="prior result JSONs to merge by (task, mode)")
    parser.add_argument("--merge-only", action="store_true", help="do not run tasks; only merge --merge files")
    parser.add_argument("--model", default="", help="override the provider model (weak model -> over-fitting signal)")
    args = parser.parse_args()
    if args.self_check:
        return self_check()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
