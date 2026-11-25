#!/usr/bin/env bash
# Requires: argo serve --port 8000
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "argo",
    "messages": [
      {"role": "user", "content": "Write report.md summarizing the three main risks of agent memory systems, with mitigations."}
    ],
    "argo": {"session_id": "example-memory-risks"}
  }' | python -m json.tool
