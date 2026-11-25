# Examples

- `quickstart.py` — programmatic one-shot agent run.
- `deck.py` — render a PPTX directly from a spec (no model call).
- `openai_curl.sh` — call the OpenAI-compatible server.
- `daemon_demo.sh` — register a read-only interval trigger and a webhook trigger.
- `web/form.html` — deterministic page used by the computer-use demo.

```bash
python examples/quickstart.py
python examples/deck.py
argo serve --port 8000 & bash examples/openai_curl.sh
```
