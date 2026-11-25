---
name: research-report
description: Produce a cited research report from web/arXiv/Wikipedia sources using search + fetch + document rendering.
---

# Research report

## When to use
The user asks for a briefing, market/technical scan, literature review, or any deliverable that needs
current facts and citations.

## Procedure
1. `update_plan` with: scope → source gathering → synthesis → artifact → verification.
2. Write 5-10 concrete sub-questions. Search each (`web_search`, `wikipedia_search`, `arxiv_search`).
3. Prefer primary sources (papers, official docs, filings). `fetch_url` every source you cite; never rely
   on a search snippet for a number, quote, or API detail.
4. Keep a source ledger in a file: URL, title, date, key claim, quote. If a claim has no source, label it
   "assumption".
5. Synthesize: executive summary → findings (each with citation) → tensions/uncertainty → implications.
6. Render with `create_document` (docx or pdf; md if the user wants plain text).
7. `read_document` the output to verify structure, then `review_work` with explicit criteria.

## Quality bar
- Every non-obvious claim has a citation or is marked as inference.
- Numbers include units, dates, and source.
- Separate what is proven, what is reported, and what is speculation.

## Pitfalls
- Cloud IPs are often rate-limited by search engines; fall back to Wikipedia/arXiv or direct URLs, or ask
  the user for a Tavily/Brave key.
- Do not pad length. A dense 3-page report beats a hollow 12-page one.
