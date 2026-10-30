# Admin Guide

> Applies to: QQ version + Discord version.
> Admin identity: QQ = `ADMIN_OPENIDS` env (member_openids, comma-separated); Discord = `ADMIN_USER_IDS` (Discord user IDs, comma-separated).
> **All key-configuration commands are DM-only** — `/llm` in a QQ group is refused, so keys never land in group history.

## 1. LLM providers & keys (/llm family, DM only)

| Command | Effect |
|---|---|
| `/llm` | Provider status: model, masked key with source (env/runtime), position in ask order |
| `/llm use <provider>` | Switch preferred Q&A provider — hot, no restart |
| `/llm setkey <provider> <key>` | Set/replace API key (runtime store takes precedence over env; can enable providers absent from env) |
| `/llm setmodel <provider> <model>` | Change provider default model |
| `/llm seturl <provider> <url>` | Custom base_url (self-hosted endpoints like openai_compat) |
| `/llm delkey <provider>` | Remove runtime config, fall back to env (provider goes offline if no env key either) |
| `/llm reset` | Clear all runtime overrides |
| `/llm help` | Quick reference card |

Providers: `kimi` (web search, recommended for L2) / `deepseek` (recommended for L0/L1) / `openai_compat` / `openrouter` / `gemini` / `local` (self-hosted OpenAI-compatible endpoints)

Typical scenarios:
- Switch provider: `/llm use gemini`
- Add a provider: `/llm setkey openrouter sk-or-xxx` → `/llm use openrouter`
- Rotate a key: `/llm setkey deepseek sk-new-key` (overwrites in place)

⚠️ **Delete the message containing the key right after sending it** (QQ: long-press → recall; Discord: right-click → delete).
Keys land in `data/llm_secrets.json` (0600, excluded from git/Docker images/deploy packages/OSS).

## 2. Layered models (/models /model, available in QQ groups)

Six layers: l0=query rewrite · l1=data collection · l2=composition · deep=deep analysis · vision=vision · **memory=memory summarizer**

| Command | Effect |
|---|---|
| `/models` | Current models per layer + each provider's available model list |
| `/models refresh` | Force-refresh model lists |
| `/model set <layer> <provider/model>` | Pin a layer, e.g. `/model set l2 kimi/kimi-k3` |
| `/model reset [layer]` | Back to default (all layers if omitted) |

Note: `/llm use` changes the **default order**; layers pinned via `/model set` are unaffected.

💡 For the memory layer pick a cheap model (`/models` lists what each provider offers), e.g. `/model set memory gemini/gemini-2.5-flash`; with a local LLM, `/model set memory local/qwen2.5:7b` makes archival free.

## 2.5 Conversation memory (three-tier decaying)

User-facing commands (both platforms, not admin-only):

| Command | Effect |
|---|---|
| `/memory` | Inspect your memory (working / topic summaries / anchors) |
| `/memory forget <id>` | Delete a specific memory item |
| `/clear` | **Archive-style clear**: the session is LLM-summarized into long-term memory instead of being deleted |

Mechanism: recall score = time decay × topic relevance × salience — unrelated history never pollutes new questions; sessions idle for 1h auto-archive; corrections and explicit "remember this" become non-decaying anchors.

Optional vector recall: `MEMORY_EMBEDDING=provider` + `EMBEDDING_BASE_URL/KEY/MODEL` (remote or local Ollama); beefy servers can use `MEMORY_EMBEDDING=local` + `pip install -r discord_version/requirements-local.txt` (in-process fastembed, needs ≥2GB free RAM).

## 3. Local LLMs (the `local` provider, for capable servers)

Any OpenAI-compatible local endpoint (Ollama / llama.cpp / vLLM / LM Studio):

```bash
# env style
LOCAL_LLM_BASE_URL=http://localhost:11434/v1   # this is the default
LOCAL_LLM_MODEL=qwen2.5:7b                      # model name required
# or at runtime (admin DM):
/llm setmodel local qwen2.5:7b
/llm use local                                  # make it preferred
```

Docker container reaching host Ollama: add `extra_hosts: ["host.docker.internal:host-gateway"]` to the bot service and use `http://host.docker.internal:11434/v1` as base URL.

Resource guide: 7B model (Q4) ≈ 6GB RAM; 14B ≈ 12GB. Not recommended on lightweight servers (≤2GB free).

## 4. Other admin commands (QQ version only)

| Command | Effect |
|---|---|
| `/kb` | Knowledge-base candidate review (`/kb ok <id>` approve / `/kb no <id>` reject) |
| `/bili_login` | Bilibili QR login (unlocks video subtitle APIs) |

## 5. Egress guard (both versions, no action needed)

- All outbound messages pass secret_guard redaction: registered secrets are replaced with `***REDACTED***`
- Logs are filtered the same way — secrets never reach log files
- Runtime key file never enters git / Docker images / deploy packages / the OSS repo

## 6. Web-dependent features (optional, Discord version)

Hidden by default; appear after deploying the web service and setting env:

| env | Enables |
|---|---|
| `RATING_BASE_URL=https://your-domain` | Rating lifecycle pushes + `/rate` `/ratings` `/dotd` `/telemetry` |
| `LIVE_BASE_URL=https://...` (defaults to the above) | `/live` live timing panel |
| `WATCH_LINKS=Name\|URL;Name\|URL` | `/watch` viewing links |

Web service (rating_server: rating form / boards / telemetry pages / ICS calendar):

```bash
cd discord_version
docker compose --profile web up -d --build   # starts f1-discord-web on :8080
```

Expose :8080 as an https URL via any reverse proxy (nginx/Caddy) and put it into `RATING_BASE_URL`.
