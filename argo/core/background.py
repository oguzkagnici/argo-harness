"""In-session background jobs.

Long research or a slow build should not block the main loop. ``start_background`` launches a subagent as an
asyncio task and returns immediately; the loop can keep working and poll ``check_background``. Jobs are
in-memory and die with the run (durable work belongs in the daemon goal queue instead).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class BackgroundJob:
    id: str
    task: str
    status: str = "running"          # running | done | error | cancelled
    output: str = ""
    error: str = ""
    iterations: int = 0
    tokens: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    _task: asyncio.Task | None = None

    def summary(self, limit: int = 4000) -> str:
        elapsed = (self.finished_at or time.time()) - self.started_at
        head = f"[{self.id}] {self.status} · {elapsed:.1f}s · {self.tokens} tok"
        if self.status == "running":
            return head + "\nStill running. Do useful work and check again later."
        if self.error:
            return head + f"\nERROR: {self.error[:1000]}"
        return head + "\nRESULT:\n" + (self.output or "(empty)")[:limit]


class BackgroundManager:
    def __init__(self) -> None:
        self.jobs: dict[str, BackgroundJob] = {}

    def start(self, task: str, runner: Callable[[], Awaitable[Any]]) -> BackgroundJob:
        job = BackgroundJob(id=uuid.uuid4().hex[:8], task=task)
        self.jobs[job.id] = job

        async def drive() -> None:
            try:
                result = await runner()
                job.output = result.output
                job.iterations = result.iterations
                job.tokens = result.usage.total_tokens
                job.status = "done"
            except asyncio.CancelledError:
                job.status = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001
                job.error = f"{type(exc).__name__}: {exc}"
                job.status = "error"
            finally:
                job.finished_at = time.time()

        job._task = asyncio.create_task(drive())
        return job

    def get(self, job_id: str) -> BackgroundJob | None:
        return self.jobs.get(job_id)

    def listing(self) -> str:
        if not self.jobs:
            return "No background jobs."
        return "\n".join(f"[{j.id}] {j.status} · {j.task[:80]}" for j in self.jobs.values())

    async def cancel_all(self) -> None:
        running = [j for j in self.jobs.values() if j.status == "running" and j._task]
        for job in running:
            job._task.cancel()
        if running:
            await asyncio.gather(*(j._task for j in running), return_exceptions=True)
