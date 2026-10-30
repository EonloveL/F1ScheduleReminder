# LLM Integration Guide

> Applies to both the QQ and Discord versions (they share the same LLM pipeline in `common/llm*`).
> Everything can be configured via `.env`; runtime changes are done by admins via DM commands `/llm` and `/model` (see "Runtime management").
> With no keys configured, every non-AI feature (pushes / commands / charts) works normally — only AI Q&A is disabled.

## 1. Feature ↔ model layer mapping

Tasks are routed to "layers"; each layer can be pinned to a provider/model (`/model set <layer> <provider/model>`):

| Layer | Code | What it does | Default model (auto-picked from available providers) | Selection advice |
|---|---|---|---|---|
| Query rewrite | `l0` | Resolves pronouns/relative time into standalone questions ("what about his teammate?" → "Piastri's points?") | First non-web provider in ask order | Fast & cheap |
| Data collection | `l1` | Decides which F1 data tools to call (never answers directly) | Same | Needs reliable JSON output |
| Composition | `l2` | Writes the final answer from tool data (may search the web) | First **web-capable** provider | Needs web search (Kimi) |
| Deep analysis | `deep` | Upgrade-effectiveness analyses (forced web + reasoning model) | kimi-k3 | Reasoning model |
| Vision | `vision` | Image understanding | kimi-k2.6 | Needs vision |
| Memory summarizer | `memory` | `/clear` archival & 1h-timeout session compression into topic summaries | First non-web provider | Cheapest available; local model = zero cost |
| Translation | (internal) | News digest / upgrade notes EN→ZH | DeepSeek first | Fast & cheap |

Other LLM consumers: weekly-preview lap-record verification (Kimi web), Bilibili video summarization, weekly knowledge distillation, news translation.

## 2. Providers

Six providers; **a provider activates automatically once its key is set**. Q&A falls back along `LLM_PROVIDER` preference + built-in order.

| Provider | Key env | Model env | Default model | Web | Notes |
|---|---|---|---|---|---|
| Kimi (Moonshot) | `MOONSHOT_API_KEY` | `LLM_MODEL` | kimi-k2.6 | ✅ | Web + vision workhorse |
| DeepSeek | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL` | deepseek-chat | ❌ | Rewrite/collect/translate workhorse — fast & cheap |
| OpenAI-compatible | `OPENAI_COMPAT_KEY` | `OPENAI_COMPAT_MODEL` | — (required) | ❌ | Any self-hosted/third-party compatible endpoint; also needs `OPENAI_COMPAT_BASE_URL` |
| OpenRouter | `OPENROUTER_API_KEY` | `OPENROUTER_MODEL` | openai/gpt-4o-mini | ❌ | One key, many models |
| Gemini | `GEMINI_API_KEY` | `GEMINI_MODEL` | gemini-2.5-flash | ❌ | Google's OpenAI-compatible endpoint |
| **Local** (Ollama/llama.cpp/vLLM/LM Studio) | `LOCAL_LLM_API_KEY` (optional) | `LOCAL_LLM_MODEL` | — (required to enable) | ❌ | Keyless; `LOCAL_LLM_BASE_URL` defaults to `http://localhost:11434/v1` |

Fallback order (override the first choice with `LLM_PROVIDER`):
`kimi → deepseek → openai_compat → openrouter → gemini → local`
(Translation order is reversed: DeepSeek first — no web needed.)

```env
# Minimal viable combo (either alone enables AI Q&A):
MOONSHOT_API_KEY=sk-...        # web-capable Q&A + vision
DEEPSEEK_API_KEY=sk-...        # rewrite/collect/translate (strongly recommended together, layered cost saving)
```

## 3. Behavior tuning (all have sane defaults — usually untouched)

| Env | Default | Controls |
|---|---|---|
| `LLM_ASK_ENABLED` | true | Master switch (false = AI fully off) |
| `LLM_MAX_CONCURRENCY` | 4 | Global AI concurrency cap (provider rate-limit protection) |
| `LLM_ASK_NOTICE_AFTER` | 45s | Send "still working" progress notice after this long |
| `LLM_ASK_HARD_TIMEOUT` | 600s | Hard timeout for Q&A (background task cancelled at turn boundary) |
| `LLM_CONNECT_TIMEOUT` | 10s | HTTP connect timeout (fail fast) |
| `LLM_L1_TIMEOUT` | 60s | Collect-layer read timeout |
| `LLM_L2_TIMEOUT` | 150s | Compose-layer read timeout |
| `LLM_DEEP_TIMEOUT` | 180s | Deep-analysis read timeout |
| `LLM_L2_MAX_TOKENS` | 1800 | Compose-layer output budget (prevents read-timeout on long answers) |
| `LLM_L2_REASONING` | none | Kimi compose-layer reasoning effort (none/low/medium/high; data is injected, reasoning is wasted cost) |
| `LLM_DEEP_MODEL` | kimi-k3 | Deep-analysis model (Kimi only) |
| `LLM_DEEP_REASONING` | high | Deep-analysis reasoning effort (Kimi only) |
| `LLM_VISION_MODEL` | kimi-k2.6 | Vision model |
| `LLM_L1_JSON_MODE` | true | Collect-layer structured output (false = native function calling) |
| `TAVILY_API_KEY` / `BOCHA_API_KEY` | — | Client-side web search for non-web providers (e.g. DeepSeek) |

## 4. Conversation memory (three-tier decaying)

| Env | Default | Controls |
|---|---|---|
| `MEMORY_EMBEDDING` | off | Relevance engine: `off` = entity/topic matching (zero cost); `provider` = vector recall (OpenAI-compatible embeddings — remote or local Ollama); `local` = in-process fastembed (needs `pip install fastembed`, ≥2GB free RAM) |
| `EMBEDDING_BASE_URL` / `EMBEDDING_KEY` / `EMBEDDING_MODEL` | — | Endpoint/key/model for provider mode (key may be empty for local Ollama) |
| `LOCAL_EMBED_MODEL` | BAAI/bge-small-zh-v1.5 | fastembed model for local mode |

Memory works with zero configuration: recent turns kept verbatim (1h half-life), `/clear` and 1h timeouts archive into topic summaries (3-day half-life), corrections and explicit "remember this" become permanent anchors, unrelated history is never injected into new topics.

## 5. Runtime management (admin DM, hot-reload)

| Command | Effect |
|---|---|
| `/llm` | Provider status (keys shown masked) |
| `/llm use <provider>` | Switch preferred provider |
| `/llm setkey <provider> <key>` | Set/replace key (can enable providers not in env) |
| `/llm setmodel <provider> <model>` | Change default model |
| `/llm seturl <provider> <url>` | Custom endpoint |
| `/llm delkey <provider>` / `/llm reset` | Remove runtime config / clear all overrides |
| `/models` `/model set <layer> <provider/model>` | Per-layer model management |

Security: `/llm` is refused in group chats (keys must not land in group history); runtime keys live in `data/llm_secrets.json` (0600, excluded from git/images/packages); all outbound messages and logs are auto-redacted for registered secrets.

## 6. Common setups

| Scenario | Config |
|---|---|
| Minimal cost | `DEEPSEEK_API_KEY` only (no web verification) |
| Recommended | `MOONSHOT_API_KEY` + `DEEPSEEK_API_KEY` |
| One key for everything | `OPENROUTER_API_KEY` (mind per-model rate limits) |
| Fully local (beefy server) | Ollama with `qwen2.5:14b` + `LOCAL_LLM_MODEL=qwen2.5:14b`, `/llm use local`, `/model set memory local/qwen2.5:7b` |
| Mainland-China server | Kimi/DeepSeek direct, no proxy; Gemini/OpenRouter need egress |

Docker containers reaching a host Ollama: add `extra_hosts: ["host.docker.internal:host-gateway"]` to the bot service and use `http://host.docker.internal:11434/v1` as base URL.
