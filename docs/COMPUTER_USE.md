# Computer use and workflow extraction

Argo can drive a real browser and turn a successful interaction into a **parameterized, replayable workflow**.
This is the "extract and automate complex computer use" layer.

## 1. The browser tool

`computer` uses Playwright (optional dependency):

```bash
uv pip install -e ".[computer]"
python -m playwright install chromium
```

Actions: `launch/goto`, `click` (CSS selector, visible text, or x/y), `type`, `press`, `scroll`, `wait`,
`screenshot`, `extract`, `eval`, `back`, `close`.

Every action returns:
- a **screenshot** (attached to the model as an image message, for vision-capable models),
- a **compact DOM view**: title, URL, headings, interactive elements (`input#email[name=email]`), links, text,
- a saved PNG path under `.argo/browser/`.

Browser state (cookies/localStorage) is persisted to `.argo/browser/state.json`, so workflows can resume
authenticated sessions without putting credentials in the transcript. The agent still needs credentials from a
secret source; never store them in memory or task text.

**Safety.** The tool blocks non-HTTP(S) schemes and metadata endpoints, runs headless by default, and is not a
security sandbox. Use a dedicated profile/container for untrusted sites. Read-only daemon triggers cannot use
`computer`.

## 2. Demonstrated end-to-end run

Test page (`examples/web/form.html`):

```html
<form onsubmit="... document.getElementById('out').innerText='Welcome, '+name+'!'">
  <input id="name" name="name">
  <button id="go" type="submit">Sign in</button>
</form>
```

Agent task:

```bash
argo run --no-stream "Open http://127.0.0.1:8899/form.html in the browser, fill the Name field with
'Grace', click 'Sign in', and confirm the page shows Welcome, Grace!. Report the URL and result."
```

Verified result: the DOM text after the click contained `Welcome, Grace!`; screenshots were saved under
`.argo/browser/`. The agent correctly used `launch → type(#name) → click(#go) → extract`.

Extract the trace into a workflow:

```bash
argo workflow extract 20261124-124901-d50dd0 --name demo_login
```

Generated `.argo/workflows/demo_login.json` (abridged):

```json
{
  "name": "demo_login",
  "params": [
    {"name": "url", "required": true, "default": "http://127.0.0.1:8899/form.html"},
    {"name": "name", "required": true, "default": "Grace"}
  ],
  "steps": [
    {"tool": "computer", "args": {"action": "launch", "url": "{{url}}"}, "expect_contains": "Demo Login"},
    {"tool": "computer", "args": {"action": "type", "selector": "#name", "text": "{{name}}"},
     "expect_contains": "type ok"},
    {"tool": "computer", "args": {"action": "click", "selector": "#go"}, "expect_contains": "Welcome,"}
  ]
}
```

Replay with a different value:

```bash
argo workflow run demo_login -p name=Alan
# → step 3 output text contains: "Welcome, Alan!"
```

This proves the full loop: **explore → record → parameterize → assert → replay**.

A matching skill is written to `.argo/skills/demo_login/SKILL.md`, so future sessions discover the procedure
through progressive disclosure and can replay it with `run_workflow`.

## 3. How extraction works

`argo/workflows/synth.py`:

1. Loads the session JSONL and keeps tool calls + outputs (last ~60), plus plan updates.
2. Sends the trace and the **actual tool catalog** to the model with hard constraints:
   - use only real tools/arguments,
   - lift run-specific literals (URLs, names, paths, queries) into parameters,
   - drop dead ends and exploratory reads,
   - add success assertions only when the trace proves them,
   - use `save_as`/`{{var}}` for data flow.
3. Validates the JSON and removes steps whose tool does not exist.
4. Saves the workflow and a `SKILL.md`.

## 4. Why declarative workflows, not replay code

| Raw mouse/keyboard replay | Generated Playwright/Python | Declarative workflow (Argo) |
|---|---|---|
| Brittle coordinates | Expressive | Parameterized + assertions |
| No policy checks | Bypasses tool policy | Runs through the same registry/approvals |
| Hard to review | Needs code review | JSON, diffable, inspectable |
| No semantic success check | Possible | `expect_contains`, `expect_exit_zero`, retries |

The tradeoff is expressiveness: complex branching logic may need a real tool or a small script. For repeatable
UI procedures (fill a form, download a report, check a dashboard), declarative is safer and sufficient.

## 5. Patterns that work

- **Prefer APIs when they exist.** `fetch_url` + JSON parsing is faster and more stable than the GUI.
- **Use semantic selectors** (`#id`, `[name=...]`, visible text) over coordinates.
- **Assert after every state change** (`expect_contains` on a confirmation).
- **Persist auth state, not credentials.** Let the user log in once in the profile; workflows reuse cookies.
- **Keep workflows small.** One workflow = one outcome. Compose with `run_workflow` from a parent task.
- **Re-extract after UI changes** rather than patching selectors blindly.
- **Use screenshots for layout/visual checks**, DOM text for reliable extraction.

## 6. Roadmap

- Selector healing and DOM-diff assertions.
- Deferred approvals that resume the exact browser action.
- Per-workflow dry-run against a recorded DOM snapshot.
- Containerized browser per run for stronger isolation.
