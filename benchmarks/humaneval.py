"""HumanEval pass@1 benchmark (Chen et al., 2021) for Argo's provider layer.

Runs the canonical HumanEval completion+unit-test protocol:
  program = prompt + completion; test + `check(entry_point)`; exit code 0 == pass.
Each sample executes in an isolated subprocess with a hard timeout.

Usage:
  python benchmarks/humaneval.py --limit 20 --samples 1
  python benchmarks/humaneval.py --ids HumanEval/0,HumanEval/1 --verbose
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from argo.config import load_config  # noqa: E402
from argo.core.types import Message  # noqa: E402
from argo.llm.client import LLMClient  # noqa: E402

DATA = Path(__file__).resolve().parent / "data" / "humaneval.jsonl.gz"
RESULTS = Path(__file__).resolve().parent / "results"

INSTRUCTION = """Complete the following Python function. Return the COMPLETE implementation \
(include the original signature and docstring, and any needed imports). Output only a single \
Python code block with no explanation.

```python
{prompt}
```"""


def load_tasks() -> list[dict]:
    with gzip.open(DATA, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def extract_code(completion: str, prompt: str, entry_point: str) -> str:
    text = completion.strip()
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    imports = []
    for line in prompt.splitlines():
        if line.startswith(("import ", "from ")):
            imports.append(line)
        elif re.match(r"^(def |class |@)", line):
            break
    if re.search(rf"(?m)^def\s+{re.escape(entry_point)}\s*\(", text):
        program = text
    else:
        program = prompt + ("\n" if not prompt.endswith("\n") else "") + text
    if imports and not any(line in program for line in imports):
        program = "\n".join(imports) + "\n\n" + program
    return program


def run_tests(program: str, test: str, entry_point: str, timeout: float = 15.0) -> tuple[bool, str]:
    source = program.rstrip() + "\n\n" + test + f"\n\ncheck({entry_point})\n"
    with tempfile.TemporaryDirectory(prefix="he_") as tmp:
        path = Path(tmp) / "solution.py"
        path.write_text(source, encoding="utf-8")
        try:
            proc = subprocess.run([sys.executable, "-I", str(path)], capture_output=True, text=True,
                                  timeout=timeout, cwd=tmp)
        except subprocess.TimeoutExpired:
            return False, "timeout"
        if proc.returncode == 0:
            return True, ""
        err = (proc.stderr or proc.stdout).strip().splitlines()
        return False, err[-1] if err else f"exit {proc.returncode}"


async def solve(llm: LLMClient, task: dict, sem: asyncio.Semaphore, temperature: float,
                max_tokens: int, verbose: bool) -> dict:
    async with sem:
        started = time.monotonic()
        last_err = ""
        for attempt in range(2):  # one retry on API errors only, never on wrong answers
            try:
                resp = await llm.chat(
                    [Message(role="user", content=INSTRUCTION.format(prompt=task["prompt"]))],
                    tools=None, temperature=temperature, max_tokens=max_tokens)
                completion = resp.message.content or ""
                break
            except Exception as exc:  # noqa: BLE001
                last_err = f"{type(exc).__name__}: {exc}"
                if attempt == 1:
                    return {"task_id": task["task_id"], "passed": False, "error": last_err,
                            "completion": "", "latency_s": round(time.monotonic() - started, 1)}
                await asyncio.sleep(2)
        program = extract_code(completion, task["prompt"], task["entry_point"])
        ok, err = run_tests(program, task["test"], task["entry_point"])
        if verbose:
            status = "PASS" if ok else "FAIL"
            print(f"{task['task_id']:<16} {status}  {err[:110]}", flush=True)
        return {"task_id": task["task_id"], "passed": ok, "error": err,
                "completion": completion, "program": program,
                "usage": resp.usage.as_dict(), "model": resp.model,
                "latency_s": round(time.monotonic() - started, 1)}


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k estimator from Chen et al. (n samples, c correct)."""
    if n - c < k:
        return 1.0
    score = 1.0
    for i in range(n - c + 1, n + 1):
        score *= (1.0 - k / i)
    return 1.0 - score


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_config()
    tasks = load_tasks()
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",")}
        tasks = [t for t in tasks if t["task_id"] in wanted]
    else:
        random.Random(args.seed).shuffle(tasks)
        tasks = tasks[: args.limit]
    tasks.sort(key=lambda t: int(t["task_id"].split("/")[1]))

    async with LLMClient(cfg) as llm:
        sem = asyncio.Semaphore(args.concurrency)
        rows = await asyncio.gather(*(
            solve(llm, task, sem, args.temperature, args.max_tokens, args.verbose)
            for task in tasks for _ in range(args.samples)))
        model = next((r.get("model") for r in rows if r.get("model")), cfg.provider.model)

    by_task: dict[str, list[dict]] = {}
    for row in rows:
        by_task.setdefault(row["task_id"], []).append(row)
    per_task_pass1 = sum(1 for rs in by_task.values() if any(r["passed"] for r in rs)) / len(by_task)
    passk = (sum(pass_at_k(len(rs), sum(r["passed"] for r in rs), args.samples) for rs in by_task.values())
             / len(by_task))
    total_tokens = sum((r.get("usage") or {}).get("total", 0) for r in rows)
    summary = {
        "benchmark": "HumanEval", "model": model, "tasks": len(by_task), "samples": args.samples,
        "temperature": args.temperature, "pass@1": round(per_task_pass1, 4),
        "total_tokens": total_tokens, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if args.samples > 1:
        summary[f"pass@{args.samples}"] = round(passk, 4)
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = RESULTS / f"humaneval_{re.sub(r'[^A-Za-z0-9_.-]+', '_', model)}_{stamp}.json"
    out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    failed = [r for r in rows if not r["passed"]]
    if failed and args.verbose:
        print("\nFailures:")
        for r in failed:
            print(f"- {r['task_id']}: {r['error'][:160]}")
    print(f"\nSaved: {out.relative_to(ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20, help="number of random tasks")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--ids", type=str, default="", help="comma-separated task ids (overrides --limit)")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
