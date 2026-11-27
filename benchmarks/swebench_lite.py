"""Official SWE-bench-Lite subset: generate Argo patches and optionally grade them with the real harness.

Two stages, so patch generation and grading can run separately:

  --generate   clone each instance's repo at its base commit, run the Argo agent on the issue text, and write
               a SWE-bench-compatible predictions JSONL (``git diff``, test-file edits reverted).
  --verify     grade a predictions file locally with the instance's official test command in its pinned venv
  --evaluate   call ``python -m swebench.harness.run_evaluation`` with prebuilt images
               (``--namespace swebench``) for the same instance ids.

This machine has ~9 GB disk and ~4 GB free RAM, so the default subset is two small sympy instances and grading
runs one instance at a time with images removed afterwards. Use ``--instance-ids`` to pick others.

Usage:
  python benchmarks/swebench_lite.py --generate --limit 2
  python benchmarks/swebench_lite.py --verify --predictions benchmarks/results/swebench_lite_preds_X.jsonl
  python benchmarks/swebench_lite.py --evaluate --limit 2
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from argo.config import load_config  # noqa: E402
from argo.core.agent import Agent  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
WORK = Path("/tmp/argo_swebench")
DATASET = "princeton-nlp/SWE-bench_Lite"
DEFAULT_IDS = ["sympy__sympy-21614", "sympy__sympy-16988"]
MAX_PATCH_CHARS = 40_000

PROMPT = """You are fixing a real bug in the repository at {repo}.

ISSUE:
{problem}

Work directly in the repository. Read the relevant source, find the root cause, and edit the code so the issue is
fixed. Do not modify, delete, or weaken any test files. Prefer the minimal correct change; do not reformat unrelated
code. You may use bash to search (grep/ripgrep) and to run `python -c` syntax checks and the specific tests below.

When done, finish with a short summary: files changed and why the fix is correct."""

PROMPT_FEEDBACK = PROMPT + """

The project is installed in editable mode, so you CAN and MUST run the failing tests:

  {test_command}

Iterate: run it, read the failure, fix the source, and re-run until it passes. Do not edit the test files."""


def load_instances(ids: list[str], limit: int, offset: int = 0) -> list[dict]:
    from datasets import load_dataset
    ds = load_dataset(DATASET, split="test")
    wanted = set(ids)
    rows = [r for r in ds if (not ids or r["instance_id"] in wanted)]
    if offset:
        rows = rows[offset:]
    return rows[:limit] if limit else rows


def sh(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def prepare_repo(inst: dict, root: Path) -> Path:
    ws = root / "repos" / inst["instance_id"]
    if (ws / ".git").exists():
        sh("git", "-C", str(ws), "checkout", "-q", "--", ".", check=False)
        sh("git", "-C", str(ws), "clean", "-qfd", check=False)
        return ws
    ws.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{inst['repo']}.git"
    sh("git", "clone", "-q", "--filter=blob:none", "--no-checkout", url, str(ws))
    sh("git", "-C", str(ws), "checkout", "-q", inst["base_commit"])
    (ws / ".git" / "info").mkdir(exist_ok=True)
    exclude = ws / ".git" / "info" / "exclude"
    exclude.write_text((exclude.read_text() if exclude.exists() else "") + "\n.argo/\n__pycache__/\n*.pyc\n")
    return ws


def test_files(inst: dict) -> set[str]:
    """Files touched by the official test patch: the agent must not edit them."""
    patch = inst.get("test_patch") or ""
    files = set()
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            files.add(line[6:].strip())
        elif line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                files.add(parts[3].removeprefix("b/"))
    return {f for f in files if f}


def make_patch(ws: Path, inst: dict) -> tuple[str, bool]:
    protected = test_files(inst)
    tampered = False
    for rel in protected:
        target = ws / rel
        if target.exists():
            before = sh("git", "-C", str(ws), "diff", "--", rel)
            if before:
                tampered = True
    if tampered:  # restore official tests; eval applies test_patch itself
        sh("git", "-C", str(ws), "checkout", "--", *sorted(protected), check=False)
    sh("git", "-C", str(ws), "add", "-N", ".", check=False)
    diff = sh("git", "-C", str(ws), "diff", "--", ".", ":(exclude).argo", check=False)
    return diff[:MAX_PATCH_CHARS], tampered


def fail_to_pass(inst: dict) -> list[str]:
    raw = inst.get("FAIL_TO_PASS", inst.get("fail_to_pass", []))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [raw]
    return [str(x) for x in (raw or [])]


def test_command(inst: dict) -> str:
    """The instance's FAIL_TO_PASS tests, run with the official repo test command.

    Django uses ``tests/runtests.py`` labels, astropy/sympy use pytest node ids. When the
    official spec has a ``test_cmd`` we append the F2P labels to it (the same shape the
    harness uses); otherwise we fall back to a pytest invocation with ``-k`` selection.
    """
    nodes = fail_to_pass(inst)
    if not nodes:
        return ""
    spec_cmd = (official_spec(inst) or {}).get("test_cmd") or ""
    if spec_cmd:
        import shlex
        quoted = " ".join(shlex.quote(n) for n in nodes[:40])
        return f"{spec_cmd.strip()} {quoted}"
    if not nodes:
        return ""
    if all("::" in n for n in nodes):
        quoted = " ".join(f'"{n}"' for n in nodes[:25])
        return f"python -m pytest -q {quoted}"
    files = sorted(test_files(inst))
    names = [n.split("::")[-1] for n in nodes[:25]]
    selector = " or ".join(names)
    if files:
        return f'python -m pytest -q {" ".join(chr(34) + f + chr(34) for f in files)} -k "{selector}"'
    quoted = " ".join(f'"{n}"' for n in nodes[:25])
    return f"python -m pytest -q {quoted}"


def apply_test_patch(ws: Path, inst: dict) -> bool:
    """Apply the official test patch and commit it as the baseline.

    SWE-bench applies the test patch before running FAIL_TO_PASS, so the agent must see
    those tests too. Committing it keeps the later ``git diff`` limited to source edits
    (and still detects *additional* agent edits to test files as tampering).
    """
    patch = inst.get("test_patch") or ""
    if not patch.strip():
        return False
    marker = ws / ".git" / "argo_test_patch_applied"
    if marker.exists():
        return True
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as fh:
        fh.write(patch)
        tmp = fh.name
    try:
        proc = subprocess.run(["git", "-C", str(ws), "apply", "--whitespace=nowarn", tmp],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            return False
        sh("git", "-C", str(ws), "add", "-A")
        subprocess.run(["git", "-C", str(ws), "-c", "user.email=argo@local", "-c", "user.name=argo",
                        "commit", "-qm", "official test patch"], capture_output=True, text=True)
        marker.write_text("1")
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def official_spec(inst: dict) -> dict:
    """The official SWE-bench install recipe for this repo/version (pinned packages)."""
    try:
        from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
        from swebench.harness.test_spec.test_spec import make_test_spec
        ts = make_test_spec(inst)
        conf = dict(MAP_REPO_VERSION_TO_SPECS[inst["repo"]][ts.version])
        conf["_scripts"] = list(ts.repo_script_list or []) + list(ts.env_script_list or [])
        return conf
    except Exception:
        return {}


def spec_python(spec: dict) -> str:
    """Interpreter version the official harness uses (3.9 / 3.11 / ...)."""
    import re
    for chunk in spec.get("_scripts") or []:
        match = re.search(r"python[= ]?(3\.(?:9|10|11|12))", str(chunk))
        if match:
            return match.group(1)
    return "3.11"


def pytest_pin(spec: dict) -> str:
    """Pull the pytest version out of the official requirements snapshot, else unpinned."""
    import re
    for chunk in spec.get("_scripts") or []:
        match = re.search(r"\bpytest==([0-9][^\s\"'\\]*)", str(chunk))
        if match:
            return f"pytest=={match.group(1)}"
    return "pytest"


def install_env(ws: Path, inst: dict | None = None, timeout: float = 1200.0) -> tuple[bool, str, str]:
    """Build an isolated venv from the official spec so test versions match the harness.

    A blind ``pip install -e .`` resolves *current* deps and breaks old checkouts (e.g. Flask 2.x
    needs Werkzeug 2.3.7, not 3.x). We therefore create a venv outside the repo, install the
    spec's pinned ``pip_packages``, then ``pip install -e . --no-deps``. Returns (ok, log, python).
    """
    venv = (ws.parent / f".venv_{inst['instance_id']}") if inst else (ws.parent / ".venv_generic")
    python = str(venv / "bin" / "python")
    log: list[str] = []
    spec = official_spec(inst) if inst else {}
    want = spec_python(spec)
    try:
        if (venv / "bin" / "python").exists() and not (venv / ".argo_python").exists():
            shutil.rmtree(venv, ignore_errors=True)  # stale venv built with the wrong interpreter
        if not (venv / "bin" / "python").exists():
            uv = shutil.which("uv")
            if uv:
                proc = subprocess.run([uv, "venv", "--python", want, "--seed", str(venv)],
                                      capture_output=True, text=True, timeout=600)
            else:
                proc = subprocess.run([sys.executable, "-m", "venv", str(venv)],
                                      capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                return False, (proc.stdout + proc.stderr)[-2000:], python
            (venv / ".argo_python").write_text(want, encoding="utf-8")
        pins = spec.get("pip_packages") or []
        if isinstance(pins, str):
            pins = pins.split()
        base = [python, "-m", "pip", "install", "-q", "--upgrade", "pip"]
        for cmd in (base,
                    [python, "-m", "pip", "install", "-q", "setuptools", "wheel", pytest_pin(spec)],
                    [python, "-m", "pip", "install", "-q", *pins] if pins else None,
                    # With pinned packages, resolve deps ourselves (old checkouts pull broken latest deps).
                    # Without pins (e.g. old Django), let the package metadata resolve its own deps.
                    [python, "-m", "pip", "install", "-q", "-e", "."] + (["--no-deps"] if pins else [])):
            if not cmd:
                continue
            proc = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=timeout)
            log.append((proc.stdout + proc.stderr)[-1500:])
            if proc.returncode != 0 and "-e" not in cmd:
                return False, "\n".join(log)[-4000:], python
            if proc.returncode != 0:
                return False, "\n".join(log)[-4000:], python
        return True, "\n".join(log)[-1500:], python
    except subprocess.TimeoutExpired:
        return False, "install timed out", python


def build_cfg(ws: Path, state: Path):
    cfg = load_config({"workspace": str(ws), "data_dir": str(state)})
    cfg.session_dir = state / "sessions"
    cfg.index_dir = state / "index"
    cfg.workflow_dir = state / "workflows"
    cfg.skill_dir = state / "skills"
    cfg.spool_dir = state / "spool"
    cfg.ensure_dirs()
    return cfg


async def generate_one(inst: dict, args) -> dict:
    ws = prepare_repo(inst, WORK)
    state = WORK / "state" / inst["instance_id"]
    cfg = build_cfg(ws, state)
    started = time.monotonic()
    row = {"instance_id": inst["instance_id"], "repo": inst["repo"],
           "base_commit": inst["base_commit"], "tests_protected": sorted(test_files(inst))[:10]}
    prompt = PROMPT.format(repo=inst["repo"], problem=inst["problem_statement"])
    cmd = test_command(inst)
    if getattr(args, "install_deps", False):
        row["test_patch_applied"] = apply_test_patch(ws, inst)
        ok, log, venv_python = install_env(ws, inst)
        row["deps_installed"] = ok
        if not ok:
            row["install_log"] = log[-1200:]
        elif cmd:
            # A wrapper outside the repo pins the venv interpreter (the editable install target)
            # without adding an untracked file to the prediction diff.
            runner = WORK / f"run_tests_{inst['instance_id']}.sh"
            runner.parent.mkdir(parents=True, exist_ok=True)
            bindir = Path(venv_python).parent
            # Django's runtests.py has an `#!/usr/bin/env python` shebang, so prepend the venv
            # to PATH; pytest-style commands are made explicit instead.
            real_cmd = cmd
            if real_cmd.startswith("python "):
                real_cmd = f"{venv_python} {real_cmd[len('python '):]}"
            runner.write_text(f'#!/bin/bash\nexport PATH="{bindir}:$PATH"\ncd "{ws}"\n{real_cmd}\n',
                              encoding="utf-8")
            prompt = PROMPT_FEEDBACK.format(repo=inst["repo"], problem=inst["problem_statement"],
                                            test_command=f"bash {runner}")
        row["test_command"] = cmd
    try:
        async with Agent(cfg, max_iterations=args.max_iterations) as agent:
            result = await asyncio.wait_for(
                agent.run(prompt),
                timeout=args.timeout)
        row.update({"stop_reason": result.stop_reason, "iterations": result.iterations,
                    "tokens": result.usage.total_tokens, "session_id": result.session_id,
                    "output": result.output[:800]})
    except asyncio.TimeoutError:
        row.update({"stop_reason": "timeout", "iterations": 0, "tokens": 0, "output": ""})
    patch, tampered = make_patch(ws, inst)
    row.update({"patch_chars": len(patch), "patch_files": patch.count("diff --git"),
                "tampered_tests": tampered, "patch": patch,
                "seconds": round(time.monotonic() - started, 1)})
    print(f"{inst['instance_id']:<24} stop={row['stop_reason']:<12} iter={row['iterations']:<3} "
          f"tok={row['tokens']:<7} files={row['patch_files']:<2} patch={len(patch):>6}c "
          f"tampered={tampered} {row['seconds']:>6.0f}s", flush=True)
    return row


async def generate(args) -> int:
    ids = [s.strip() for s in args.instance_ids.split(",") if s.strip()] or DEFAULT_IDS
    insts = load_instances(ids, args.limit, args.offset)
    print(f"instances: {', '.join(i['instance_id'] for i in insts)}", flush=True)
    rows = []
    for inst in insts:
        rows.append(await generate_one(inst, args))
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    preds = RESULTS / f"swebench_lite_preds_{stamp}.jsonl"
    with preds.open("w", encoding="utf-8") as fh:
        for r in rows:
            if r["patch"]:
                fh.write(json.dumps({"instance_id": r["instance_id"],
                                     "model_name_or_path": f"argo-{os.environ.get('ARGO_MODEL', 'deepseek')}",
                                     "model_patch": r["patch"]}) + "\n")
    record = RESULTS / f"swebench_lite_{stamp}.json"
    record.write_text(json.dumps({"benchmark": "swebench-lite-local", "dataset": DATASET,
                                  "predictions": str(preds.relative_to(ROOT)), "rows": rows,
                                  "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=2))
    print(f"\npredictions: {preds.relative_to(ROOT)}\nrecord: {record.relative_to(ROOT)}")
    return 0


def verify(args) -> int:
    """Grade a predictions file locally with the *official* repo test command (no Docker).

    Resolve = the model patch applies cleanly and every FAIL_TO_PASS test reports success under the
    instance's pinned venv. This is not the official container harness (PASS_TO_PASS regression is
    only covered by the F2P run), but it uses the official test runner and test patch.
    """
    preds = args.predictions
    if not preds:
        found = sorted(RESULTS.glob("swebench_lite_preds_*.jsonl"))
        if not found:
            print("no predictions file", file=sys.stderr)
            return 2
        preds = str(found[-1])
    wanted = [s.strip() for s in args.instance_ids.split(",") if s.strip()]
    pred_rows = [json.loads(line) for line in Path(preds).read_text(encoding="utf-8").splitlines() if line.strip()]
    insts = {r["instance_id"]: r for r in load_instances(wanted, 0)}
    rows = []
    for row in pred_rows:
        inst = insts.get(row["instance_id"])
        if inst is None:
            continue
        ws = prepare_repo(inst, WORK)
        row_out = {"instance_id": inst["instance_id"], "patch_chars": len(row.get("model_patch") or "")}
        row_out["test_patch_applied"] = apply_test_patch(ws, inst)
        ok, log, venv_python = install_env(ws, inst)
        row_out["deps_installed"] = ok
        if not ok:
            row_out.update({"resolved": False, "reason": "install failed", "log": log[-800:]})
            rows.append(row_out)
            continue
        patch_file = WORK / f"patch_{inst['instance_id']}.diff"
        patch_file.write_text(row.get("model_patch") or "", encoding="utf-8")
        apply = subprocess.run(["git", "-C", str(ws), "apply", "--whitespace=nowarn", str(patch_file)],
                               capture_output=True, text=True)
        row_out["patch_applied"] = apply.returncode == 0
        if apply.returncode != 0:
            row_out.update({"resolved": False, "reason": "patch did not apply",
                            "log": apply.stderr[-600:]})
            rows.append(row_out)
            continue
        cmd = test_command(inst)
        bindir = Path(venv_python).parent
        env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}
        started = time.monotonic()
        proc = subprocess.run(["bash", "-lc", cmd], cwd=str(ws), capture_output=True, text=True,
                              env=env, timeout=args.test_timeout)
        out = (proc.stdout + proc.stderr).strip()
        row_out.update({"resolved": proc.returncode == 0, "returncode": proc.returncode,
                        "seconds": round(time.monotonic() - started, 1), "output": out[-1200:]})
        rows.append(row_out)
        print(f"{inst['instance_id']:<24} resolved={row_out['resolved']} rc={proc.returncode} "
              f"{row_out['seconds']:>6.0f}s", flush=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    record = RESULTS / f"swebench_lite_verify_{stamp}.json"
    record.write_text(json.dumps({"benchmark": "swebench-lite-local-verify",
                                  "predictions": str(Path(preds).resolve().relative_to(ROOT)),
                                  "resolved_rate": round(sum(r["resolved"] for r in rows) / len(rows), 4) if rows else 0.0,
                                  "rows": rows,
                                  "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=2))
    print(f"\nverified {sum(r['resolved'] for r in rows)}/{len(rows)}; record: {record.relative_to(ROOT)}")
    return 0


def evaluate(args) -> int:
    ids = [s.strip() for s in args.instance_ids.split(",") if s.strip()] or DEFAULT_IDS
    preds = args.predictions
    if not preds:
        found = sorted(RESULTS.glob("swebench_lite_preds_*.jsonl"))
        if not found:
            print("no predictions file; run --generate first", file=sys.stderr)
            return 2
        preds = str(found[-1])
    run_id = f"argo_{time.strftime('%Y%m%d%H%M%S')}"
    cmd = [sys.executable, "-m", "swebench.harness.run_evaluation",
           "--dataset_name", "SWE-bench/SWE-bench_Lite", "--split", "test",
           "--predictions_path", preds, "--instance_ids", *ids,
           "--max_workers", "1", "--namespace", "swebench", "--cache_level", "none",
           "--clean", "True", "--timeout", str(args.test_timeout), "--run_id", run_id]
    print(" ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT))
    print(f"\nharness exit={proc.returncode}; report dir: {ROOT / 'logs' / 'run_evaluation' / run_id}")
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="Grade a predictions file locally with the official test command (no Docker).")
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--instance-ids", default="")
    ap.add_argument("--limit", type=int, default=2)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--max-iterations", type=int, default=40)
    ap.add_argument("--timeout", type=float, default=1500.0)
    ap.add_argument("--test-timeout", type=int, default=1800)
    ap.add_argument("--predictions", default="")
    ap.add_argument("--install-deps", action="store_true",
                    help="pip install -e . in the checkout and give the agent the FAIL_TO_PASS command")
    args = ap.parse_args()
    if args.generate:
        return asyncio.run(generate(args))
    if args.verify:
        return verify(args)
    if args.evaluate:
        return evaluate(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
