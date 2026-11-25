---
name: deck-design
description: Design and build a persuasive slide deck (PPTX) with a narrative arc and visual hierarchy.
---

# Deck design

## When to use
The user asks for slides, a pitch deck, a readout, or a presentation of findings.

## Procedure
1. Decide the audience, the one-sentence takeaway, and the decision you want. Write the narrative arc:
   situation → complication → insight → recommendation → evidence → ask.
2. One idea per slide. Titles are assertions ("Churn is concentrated in month 2"), not labels ("Churn").
3. Build with `create_document(path="deck.pptx", spec={...})`:
   - `theme`: slate for product/tech, light for corporate, warm for creative.
   - `layout`: bullets, section, two_column, quote, table, cards, image.
   - 3-5 bullets max, each ≤ 12 words; put detail in `notes`.
4. Add a title slide, a section break per act, and a closing slide with the decision/ask.
5. Verify: `read_document` the PPTX; check slide count, title assertions, and that no slide overflows.
6. `review_work` with criteria: narrative coherence, one-idea-per-slide, evidence on claims, readable density.

## Quality bar
- A stranger can read only the titles and understand the argument.
- Every chart/table has a takeaway in its title or caption.
- No lorem ipsum, no generic stock phrases, no walls of text.

## Pitfalls
- Don't use bullets for causal arguments; use two_column or a diagram-like cards layout.
- Don't exceed ~6 lines of body text per slide.
