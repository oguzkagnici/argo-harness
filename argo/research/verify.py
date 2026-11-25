"""R2: claim-level citation verification.

Each claim is checked against the source it cites. The verifier is deliberately conservative: unless the
evidence entails the claim, the verdict is partial/unsupported and the claim must be rewritten or demoted.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from .ledger import Claim, EvidenceLedger

VERIFY_PROMPT = """You verify one claim against one evidence source.

CLAIM:
{claim}

EVIDENCE (from {source}):
{evidence}

Decide:
- "supported": the evidence entails the claim as stated (numbers, names, and scope match).
- "partial": the evidence supports part of it or a weaker version.
- "unsupported": the evidence does not support the claim.

Reply with JSON only: {{"verdict": "supported|partial|unsupported", "reason": "<one sentence>",
"evidence": "<short verbatim span from the evidence, or empty>"}}"""

_JSON_RE = re.compile(r"\{.*\}", re.S)


def _parse(text: str) -> tuple[str, str, str]:
    match = _JSON_RE.search(text or "")
    if not match:
        return "unverified", "verifier returned no JSON", ""
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return "unverified", "verifier returned invalid JSON", ""
    verdict = str(data.get("verdict", "")).lower().strip()
    if verdict not in ("supported", "partial", "unsupported"):
        return "unverified", f"unexpected verdict {verdict!r}", ""
    return verdict, str(data.get("reason", ""))[:400], str(data.get("evidence", ""))[:600]


async def verify_claim(llm: Any, claim: Claim, ledger: EvidenceLedger, max_evidence: int = 6000) -> Claim:
    if not claim.citations:
        claim.checked, claim.verdict, claim.reason = True, "unsupported", "claim has no citation"
        return claim
    verdicts: list[str] = []
    reasons: list[str] = []
    evidence = ""
    for cid in claim.citations:
        src = ledger.sources.get(cid)
        if src is None:
            verdicts.append("unsupported")
            reasons.append(f"citation {cid} does not exist")
            continue
        body = src.text[:max_evidence]
        prompt = VERIFY_PROMPT.format(claim=claim.text, source=f"{src.id} {src.url}", evidence=body)
        raw = ""
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = await llm.chat(_as_messages(prompt), tools=None, temperature=0.0, max_tokens=600)
                raw = (resp.message.content or "").strip()
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if "429" not in str(exc) and "rate" not in str(exc).lower():
                    break
                await asyncio.sleep(2.0 * (attempt + 1))
        if last_exc is not None:
            verdicts.append("unverified")
            reasons.append(f"{type(last_exc).__name__}: {last_exc}")
            continue
        verdict, reason, quote = _parse(raw)
        if verdict == "unverified" and raw:
            try:
                retry = await llm.chat(_as_messages(prompt + "\n\nRespond with the JSON object only, no prose."),
                                       tools=None, temperature=0.0, max_tokens=600)
                verdict, reason, quote = _parse((retry.message.content or "").strip())
            except Exception:  # noqa: BLE001
                pass
        verdicts.append(verdict)
        reasons.append(reason)
        if quote and not evidence:
            evidence = quote
    rank = {"supported": 0, "partial": 1, "unverified": 2, "unsupported": 3}
    best = min(verdicts, key=lambda v: rank.get(v, 3)) if verdicts else "unverified"
    claim.checked = True
    claim.verdict = best
    claim.reason = "; ".join(r for r in reasons if r)[:600]
    claim.evidence = evidence
    return claim


def _as_messages(prompt: str) -> list[Any]:
    from ..core.types import Message
    return [Message(role="user", content=prompt)]


async def verify_ledger(ledger: EvidenceLedger, llm: Any, concurrency: int = 2) -> dict[str, object]:
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(claim: Claim) -> None:
        async with sem:
            await verify_claim(llm, claim, ledger)

    await asyncio.gather(*(one(c) for c in ledger.claims.values()))
    return ledger.stats()
