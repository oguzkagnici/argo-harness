import pytest

from argo.core.types import ToolResult
from argo.workflows.core import Workflow, WorkflowStep, _substitute, run_workflow


class FakeRegistry:
    def __init__(self):
        self.calls = []

    async def execute(self, name, args):
        self.calls.append((name, args))
        if args.get("fail"):
            return ToolResult("boom", is_error=True)
        if name == "echo":
            return ToolResult(f"echo: {args.get('text')}")
        return ToolResult("ok")


def test_substitution_recurses():
    out = _substitute({"a": "{{x}}", "b": ["{{y}}", "lit"], "c": "pre {{x}} post"},
                      {"x": "X", "y": 3})
    assert out == {"a": "X", "b": [3, "lit"], "c": "pre X post"}


@pytest.mark.asyncio
async def test_workflow_runs_saves_vars_and_asserts():
    wf = Workflow(name="t", params=[{"name": "who", "required": True}],
                  steps=[WorkflowStep(tool="echo", args={"text": "hi {{who}}"}, save_as="first"),
                         WorkflowStep(tool="echo", args={"text": "{{first}}"}, expect_contains="hi bob")])
    reg = FakeRegistry()
    run = await run_workflow(wf, {"who": "bob"}, reg)
    assert run.ok
    assert run.variables["first"] == "echo: hi bob"


@pytest.mark.asyncio
async def test_workflow_missing_param_and_retry():
    wf = Workflow(name="t", params=[{"name": "p", "required": True}],
                  steps=[WorkflowStep(tool="echo", args={"text": "{{p}}"})])
    run = await run_workflow(wf, {}, FakeRegistry())
    assert not run.ok and "missing required" in run.error

    wf2 = Workflow(name="t2", steps=[WorkflowStep(tool="echo", args={"text": "a", "fail": True}, retries=1)])
    reg = FakeRegistry()
    run2 = await run_workflow(wf2, {}, reg)
    assert not run2.ok
    assert len(reg.calls) == 2
