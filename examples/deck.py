"""Render a deck directly from a spec (no model call)."""
from argo.config import load_config
from argo.tools.base import ToolContext, ToolRegistry
from argo.tools import docs

spec = {
    "title": "Argo in 5 minutes",
    "subtitle": "A general-purpose agent harness",
    "theme": "slate",
    "slides": [
        {"layout": "section", "title": "The problem"},
        {"layout": "bullets", "title": "Harnesses are the product",
         "bullets": ["Context policy beats prompt wording", "Memory must be curated, not accumulated",
                     "Verification needs a separate evaluator"]},
        {"layout": "two_column", "title": "What Argo does",
         "left": ["Web + documents", "Code execution", "Browser automation"],
         "right": ["Hybrid retrieval", "Dreaming", "Always-on daemon"]},
        {"layout": "quote", "quote": "A small, current memory beats a large stale archive.",
         "attribution": "Argo design principle"},
    ],
}

cfg = load_config()
ctx = ToolContext(cfg=cfg)
reg = ToolRegistry(ctx)
docs.register(reg)
result = __import__("asyncio").run(
    reg.execute("create_document", {"path": "examples/out/argo_deck.pptx", "spec": spec})
)
print(result.content)
