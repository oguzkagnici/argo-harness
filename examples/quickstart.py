"""Minimal programmatic use of the Argo agent."""
import asyncio

from argo.config import load_config
from argo.core.agent import Agent


async def main() -> None:
    cfg = load_config()
    async with Agent(cfg) as agent:
        result = await agent.run(
            "Create analysis.md with a short comparison of BM25 vs dense retrieval, "
            "including when each wins. Then verify the file."
        )
        print(result.output)
        print(result.brief())


if __name__ == "__main__":
    asyncio.run(main())
