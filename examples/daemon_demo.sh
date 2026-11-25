#!/usr/bin/env bash
set -euo pipefail
# Read-only proactive research every 6 hours + a webhook-triggered task.
argo trigger add market_watch \
  "Review the top 3 headlines about AI agent memory and write a 5-bullet brief to briefs/\$(date +%F).md" \
  --kind interval --seconds 21600 --read-only

argo trigger add inbox_triage \
  "Triage the webhook payload into actions and reply with a short plan." \
  --kind webhook

argo daemon --interval 30
# Then: curl -X POST localhost:8000/hooks/inbox_triage -d '{"subject":"..."}'   (when serving)
