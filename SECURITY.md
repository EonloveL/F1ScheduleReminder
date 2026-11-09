# Security Policy

## Supported Versions

This project follows a rolling-release model on the `main` branch. Security updates are applied to the latest commit on `main`. Tagged releases are not currently used.

| Branch / Tag | Supported |
|--------------|-----------|
| `main`       | ✅ Yes    |
| older commits| ⚠️ Best-effort; please update to latest `main` |

## What We Consider Sensitive

The F1ScheduleReminder bot handles data that should be treated as sensitive:

- **API keys and tokens**: QQ AppSecret, Discord Bot Token, Moonshot/DeepSeek/OpenAI API keys, PushPlus/WeCom webhook keys, Bilibili SESSDATA, OpenF1 credentials.
- **User identifiers**: QQ `openid`, Discord user IDs, group/channel IDs.
- **Runtime data**: user preferences, voting records, LLM conversation memory, knowledge-base candidates.

The codebase uses `common/secret_guard.py` to scrub registered secrets from outbound messages and logs. Runtime secrets are stored in `data/llm_secrets.json` with `0o600` permissions and are excluded from git/docker images.

## Reporting a Vulnerability

If you discover a security issue, please **do not** open a public GitHub issue. Instead:

1. Email: `security@your-domain.example.com` (replace with your actual contact before publishing)
2. Or open a **GitHub Private Security Advisory** at:
   https://github.com/EonloveL/F1ScheduleReminder/security/advisories/new

Please include:

- A clear description of the vulnerability.
- Steps to reproduce (minimal proof-of-concept if possible).
- The version/commit hash you tested against.
- Your preferred disclosure timeline.

We aim to respond within **48 hours** and to release a fix within **7 days** for critical issues.

## Disclosure Policy

- We follow coordinated disclosure.
- We will credit reporters in the CHANGELOG unless they prefer to remain anonymous.
- We do not offer a bug bounty program at this time.

## Security Hardening Recommendations for Self-Hosters

- Run the container as a non-root user (the official Dockerfiles now create a `bot` user).
- Keep `.env` files and `data/` directory outside the container image; mount them as volumes.
- Rotate API keys regularly and avoid committing them to version control.
- Place the bot behind a reverse proxy (Caddy/Nginx) with TLS termination.
- Review `docs/SECURITY_AND_RISKS.md` for operational risks and mitigations.
