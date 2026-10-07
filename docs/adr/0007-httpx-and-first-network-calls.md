# ADR-0007: httpx and the first network calls

- **Status:** accepted
- **Date:** 2026-10-07

## Context

Milestone M0.3 adds the first calls to model providers: an `api` adapter for OpenAI-compatible endpoints. Until now Arpeggio made no network calls at all. These calls cost money and need API keys, and the [AGENTS.md](../../AGENTS.md) hard rules require an ADR for any network call or new dependency. A bug here can spend real money, leak a key into a log, or hit the network from a test.

## Decision

Use `httpx` as the only HTTP client. Every request goes through a spend guard first, and tests never reach the network.

- **Client.** `httpx` is the one new runtime dependency (`hypothesis` is the one new dev dependency, for property tests). It supports async, takes separate connect and read timeouts, and ships `httpx.MockTransport`. Only `adapters/api.py` imports it at runtime. The adapter registry loads that module lazily, so CLI commands that make no model call never import httpx.
- **Where calls go.** Requests go only to the `base_url` of a provider in the user's config, at `POST {base_url}/chat/completions`. There is no telemetry and no other host.
- **Tests.** Every adapter test passes an `httpx.MockTransport` into the adapter. The only test that reaches a provider is `tests/live/test_live_smoke.py`. It carries the `live` marker, the default `pytest` run deselects it, and it skips itself unless `ARPEGGIO_LIVE=1` and the provider's key variable are both set.
- **Keys.** Config holds a reference (`env:NAME`), never a key. `core/secrets.py` resolves the reference when a request is about to be sent and returns a `pydantic.SecretStr`. `keychain:` references fail with "not supported yet". The key goes into the `Authorization` header and nowhere else: not logs, exceptions, artifacts, database rows or a `repr`. Error messages name the variable, never its value. An integration test plants a fake key and searches every artifact, log file and database text column for it.
- **Spend guard.** Before each request, `cost/guard.py` refuses a model whose provider id is still a template placeholder such as `<free-model-id>`, any model that is not free under the `free` profile (local providers count as free), and any call whose worst case (prompt tokens estimated conservatively as `ceil(characters / 2)`, plus `max_tokens`, at the current price window) is more than the attempt has left of `per_task_usd`. A refused call sends nothing. The refusal is recorded as a step and the attempt pauses.
- **Retries.** HTTP 429, 500, 502, 503, 504 and connection or read timeouts are retried at most 3 times with full-jitter exponential backoff (base 1 s, cap 30 s). A `Retry-After` header is honored up to `budget.max_quota_wait_s`. Above that the adapter stops retrying. Every response that reports usage is billed and recorded as its own step, retries included.

## Alternatives considered

| Option | Pros | Cons |
|---|---|---|
| `urllib.request` from the standard library | No dependency | Blocking only, which breaks the async adapter rule, and no built-in way to fake a transport |
| Official `openai` SDK | Typed responses | Large dependency, its own retry logic hides cost per retry, and it is tied to one vendor's schema |
| `aiohttp` | Mature async client | No mock transport built in, so tests would need another library such as `aioresponses` |
| **`httpx` (chosen)** | Async, explicit timeouts, `MockTransport`, already named in docs/03 | One more dependency tree in the lock file |

## Consequences

- Arpeggio can now spend money. The guard bounds a single call, and budgets across tasks, days and months arrive with the cost governor (M1.4) and prepaid accounting (M1.11).
- httpx brings httpcore, h11, anyio, certifi, idna and sniffio into the lock file.
- Recorded costs are only as accurate as the prices in config. Prices change, and `last_verified` says when they were last checked.
- Logs and error messages never contain response bodies, headers or prompts. Request and response bodies are kept only as artifacts under `~/.arpeggio/artifacts/`.

## Revisit when

Streaming responses, native Anthropic-format providers or keychain references are added, or a provider needs a transport feature httpx lacks.
