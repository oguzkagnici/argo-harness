"""Configuration for Argo: BYOK, OpenAI-compatible endpoints, workspace + data dirs."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _codex_defaults() -> dict[str, Any]:
    """Inherit provider settings from Codex config when Argo has none of its own."""
    cfg = _read_toml(Path.home() / ".codex" / "config.toml")
    if not cfg:
        return {}
    provider_name = cfg.get("model_provider")
    providers = cfg.get("model_providers", {})
    provider = providers.get(provider_name, {}) if provider_name else {}
    out: dict[str, Any] = {}
    if cfg.get("model"):
        out["model"] = cfg["model"]
    if provider.get("base_url"):
        out["base_url"] = provider["base_url"]
    if provider.get("wire_api"):
        out["wire_api"] = provider["wire_api"]
    if provider.get("env_key"):
        out["env_key"] = provider["env_key"]
    return out


def _env(name: str) -> str | None:
    val = os.environ.get(name)
    return val if val else None


@dataclass(slots=True)
class ProviderConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    wire_api: str = "chat"          # "chat" (OpenAI-compatible) or "responses"
    env_key: str = ""

    @property
    def headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h


@dataclass(slots=True)
class ModelConfig:
    context_window: int = 128_000
    max_output_tokens: int = 8_192
    temperature: float = 0.2
    reasoning_effort: str | None = None
    embedding_model: str = ""
    rerank_model: str = ""
    embedding_dim: int = 0


@dataclass(slots=True)
class SearchConfig:
    provider: str = "duckduckgo"      # duckduckgo | tavily | brave | searxng
    tavily_api_key: str = ""
    brave_api_key: str = ""
    searxng_url: str = ""
    max_results: int = 8
    timeout: float = 20.0


@dataclass(slots=True)
class PolicyConfig:
    # ask | auto | deny  — "ask" pauses for approval in interactive mode / daemon queue
    shell: str = "auto"
    write_outside_workspace: str = "ask"
    network: str = "auto"
    max_shell_timeout: int = 300


@dataclass(slots=True)
class MCPServerConfig:
    name: str = "server"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    transport: str = "stdio"     # "stdio" | "http" (streamable HTTP) | "sse"
    url: str = ""
    read_only: bool = False      # unknown side effects -> treat as mutating (approval applies)
    # Per-tool override of ``read_only`` keyed by the server's own tool name
    # (e.g. {"search": true, "delete": false}); unlisted tools use ``read_only``.
    read_only_tools: dict[str, bool] = field(default_factory=dict)
    timeout: float = 120.0


@dataclass(slots=True)
class SecurityConfig:
    wrap_untrusted: bool = True
    prompt_injection_guard: bool = True
    block_secret_egress: bool = True


@dataclass(slots=True)
class ArgoConfig:
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    workspace: Path = field(default_factory=Path.cwd)
    data_dir: Path = field(default_factory=lambda: Path.home() / ".argo")
    session_dir: Path = field(default_factory=lambda: Path.cwd() / ".argo" / "sessions")
    index_dir: Path = field(default_factory=lambda: Path.cwd() / ".argo" / "index")
    workflow_dir: Path = field(default_factory=lambda: Path.cwd() / ".argo" / "workflows")
    skill_dir: Path = field(default_factory=lambda: Path.cwd() / ".argo" / "skills")
    spool_dir: Path = field(default_factory=lambda: Path.cwd() / ".argo" / "spool")
    max_iterations: int = 60
    compaction_enabled: bool = True        # False = never summarize (use when the provider window is large)
    compaction_threshold: float = 0.75     # fraction of context window that triggers work
    reset_threshold: float = 0.92
    keep_recent_tokens: int = 12_000
    compact_reserve_tokens: int = 13_000   # headroom kept below the window (Claude Code AUTOCOMPACT_BUFFER_TOKENS)
    compact_max_failures: int = 3          # circuit breaker for failing compaction calls
    compact_min_head_messages: int = 3     # do not summarize a 1-2 message head (avoid churn)
    compact_min_growth_tokens: int = 1200  # hysteresis: re-compact only after this much new context
    compact_fact_ledger: bool = True       # keep verbatim key/value facts across compaction
    time_based_microcompact: bool = True   # evict stale tool output once the server prompt cache has expired
    microcompact_gap_minutes: float = 60.0 # 1h server cache TTL, so a longer gap means the prefix is rewritten anyway
    microcompact_keep_recent: int = 5      # tool results preserved by cache-expiry eviction
    daily_token_budget: int = 0            # 0 = unlimited
    max_run_tokens: int = 0                # per-run token cap, 0 = unlimited
    max_run_wall_seconds: float = 0.0      # per-run wall-clock cap, 0 = unlimited
    max_run_tool_calls: int = 0            # per-run tool-call cap, 0 = unlimited
    model_prices: dict = field(default_factory=dict)  # {model_substr: {"input": $/Mtok, "output": $/Mtok}}
    cheap_model: str = ""                  # optional cheaper model for compaction/handoff summaries
    router_policy: str = "off"             # off | explore (per-turn cheap/strong routing)
    summary_prompt_variant: str = "default"  # default | identifiers | twopass (A/B for compaction quality)
    low_priority_pacing: float = 0.0       # seconds to sleep before each provider call (shared-quota politeness)
    memory_inject_chars: int = 6000        # always-loaded memory budget
    no_progress_limit: int = 10            # stop a run stuck in verify-only loops
    plan_mode: bool = False                # read-only exploration until a plan is approved
    review_policy: str = "manual"          # manual | adaptive | always | off
    review_risk_threshold: int = 4         # adaptive: minimum risk score to auto-review
    rrf_k: int = 60                        # RRF rank discount; SciFact ablation favoured 10
    rrf_lexical_weight: float = 0.8        # BM25 weight in fusion
    rrf_dense_weight: float = 1.2          # dense weight in fusion

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.session_dir, self.index_dir,
                  self.workflow_dir, self.skill_dir, self.spool_dir):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def memory_db(self) -> Path:
        return self.data_dir / "memory.sqlite"

    @property
    def sessions_db(self) -> Path:
        return self.data_dir / "sessions.sqlite"

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["provider"]["api_key"] = "***" if self.provider.api_key else ""
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in d.items()}


MODEL_CONTEXT_HINTS = {
    "deepseek-v4.1-flash": 1_048_576,
    "glm-5.3": 524_288,
    "qwen3.8-flash-next": 262_144,
    "gemma-4-31b": 262_144,
}


def _mcp_servers(raw_servers: list[Any]) -> list[MCPServerConfig]:
    return [
        MCPServerConfig(
            name=str(raw.get("name") or f"server{idx}"),
            command=str(raw.get("command") or ""),
            args=[str(a) for a in raw.get("args", [])],
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
            cwd=raw.get("cwd"),
            transport=str(raw.get("transport") or "stdio"),
            url=str(raw.get("url") or ""),
            read_only=bool(raw.get("read_only", False)),
            read_only_tools={str(k): bool(v) for k, v in (raw.get("read_only_tools") or {}).items()},
            timeout=float(raw.get("timeout", 120.0)),
        )
        for idx, raw in enumerate(raw_servers) if raw.get("command") or raw.get("url")
    ]


def load_config(overrides: dict[str, Any] | None = None) -> ArgoConfig:
    overrides = overrides or {}
    user = _read_toml(Path.home() / ".argo" / "config.toml")
    inferred = _codex_defaults()

    provider_raw = user.get("provider", {})
    base_url = (
        overrides.get("base_url")
        or _env("ARGO_BASE_URL")
        or provider_raw.get("base_url")
        or inferred.get("base_url")
        or DEFAULT_BASE_URL
    )
    model = (
        overrides.get("model")
        or _env("ARGO_MODEL")
        or provider_raw.get("model")
        or inferred.get("model")
        or DEFAULT_MODEL
    )
    env_key = provider_raw.get("env_key") or inferred.get("env_key") or ""
    api_key = (
        overrides.get("api_key")
        or _env("ARGO_API_KEY")
        or (_env(env_key) if env_key else None)
        or _env("OPENAI_API_KEY")
        or _env("LLM_API_KEY")
        or provider_raw.get("api_key", "")
    )

    models_raw = user.get("models", {})
    ctx = (
        models_raw.get("context_window")
        or MODEL_CONTEXT_HINTS.get(model)
        or 128_000
    )
    models = ModelConfig(
        context_window=int(ctx),
        max_output_tokens=int(models_raw.get("max_output_tokens", 8192)),
        temperature=float(models_raw.get("temperature", 0.2)),
        reasoning_effort=models_raw.get("reasoning_effort"),
        embedding_model=models_raw.get("embedding_model", "qwen3-embedding-8b"),
        rerank_model=models_raw.get("rerank_model", "qwen3-reranker-8b"),
    )

    search_raw = user.get("search", {})
    search = SearchConfig(
        provider=_env("ARGO_SEARCH_PROVIDER") or search_raw.get("provider", "duckduckgo"),
        tavily_api_key=_env("TAVILY_API_KEY") or search_raw.get("tavily_api_key", ""),
        brave_api_key=_env("BRAVE_API_KEY") or search_raw.get("brave_api_key", ""),
        searxng_url=_env("SEARXNG_URL") or search_raw.get("searxng_url", ""),
        max_results=int(search_raw.get("max_results", 8)),
    )

    policy_raw = user.get("policy", {})
    policy = PolicyConfig(**{k: v for k, v in policy_raw.items() if k in PolicyConfig.__dataclass_fields__})
    security_raw = user.get("security", {})
    security = SecurityConfig(**{k: v for k, v in security_raw.items() if k in SecurityConfig.__dataclass_fields__})

    mcp_servers = _mcp_servers(user.get("mcp") or [])

    workspace = Path(overrides.get("workspace") or user.get("workspace") or Path.cwd()).resolve()
    data_dir = Path(overrides.get("data_dir") or _env("ARGO_HOME") or user.get("data_dir")
                    or (Path.home() / ".argo")).expanduser()
    project = workspace / ".argo"

    cfg = ArgoConfig(
        provider=ProviderConfig(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            model=model,
            wire_api=(overrides.get("wire_api") or provider_raw.get("wire_api") or inferred.get("wire_api") or "chat"),
            env_key=env_key,
        ),
        models=models,
        search=search,
        policy=policy,
        security=security,
        mcp_servers=mcp_servers,
        workspace=workspace,
        data_dir=data_dir,
        session_dir=project / "sessions",
        index_dir=project / "index",
        workflow_dir=project / "workflows",
        skill_dir=project / "skills",
        spool_dir=project / "spool",
        max_iterations=int(overrides.get("max_iterations") or user.get("max_iterations", 60)),
        compaction_enabled=bool(user.get("compaction_enabled", True)),
        compaction_threshold=float(user.get("compaction_threshold", 0.75)),
        reset_threshold=float(user.get("reset_threshold", 0.92)),
        keep_recent_tokens=int(user.get("keep_recent_tokens", 12_000)),
        daily_token_budget=int(user.get("daily_token_budget", 0)),
        max_run_tokens=int(overrides.get("max_run_tokens") or user.get("max_run_tokens", 0)),
        max_run_wall_seconds=float(overrides.get("max_run_wall_seconds")
                                   or user.get("max_run_wall_seconds", 0.0)),
        max_run_tool_calls=int(overrides.get("max_run_tool_calls") or user.get("max_run_tool_calls", 0)),
        model_prices=dict(user.get("pricing", {}) or {}),
        cheap_model=str(overrides.get("cheap_model") or models_raw.get("cheap") or ""),
        router_policy=str(overrides.get("router_policy") or models_raw.get("router") or "off"),
        summary_prompt_variant=str(overrides.get("summary_prompt_variant")
                                   or user.get("summary_prompt_variant", "default")),
        low_priority_pacing=float(overrides.get("low_priority_pacing")
                                  or user.get("low_priority_pacing", 0.0) or 0.0),
        plan_mode=bool(overrides.get("plan_mode", user.get("plan_mode", False))),
        review_policy=str(overrides.get("review_policy", user.get("review_policy", "manual"))),
        review_risk_threshold=int(overrides.get("review_risk_threshold", user.get("review_risk_threshold", 4))),
        rrf_k=int(overrides.get("rrf_k", user.get("rrf_k", 60))),
        rrf_lexical_weight=float(overrides.get("rrf_lexical_weight", user.get("rrf_lexical_weight", 0.8))),
        rrf_dense_weight=float(overrides.get("rrf_dense_weight", user.get("rrf_dense_weight", 1.2))),
    )
    cfg.ensure_dirs()
    return cfg
