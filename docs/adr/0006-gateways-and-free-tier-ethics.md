# ADR-0006: Gateways and free-tier ethics

- **Status:** accepted
- **Date:** 2026-10-07

## Context

Gateways such as OpenRouter (hosted), 9Router and LiteLLM (self-hosted local proxies) put many providers behind one OpenAI-compatible endpoint. Some fall back to another model on their own, which hides which model answered and spoils routing data. Some community gateways also offer multi-account rotation, MITM endpoints, or reuse of subscription credentials. These break provider terms and can get accounts suspended. Free tiers may also train on submitted data, depending on the provider.

## Decision

Gateways and local servers are ordinary `openai_compatible` providers in config, marked with `gateway = true` where that applies.

- Provenance of the actual model is mandatory: every call records the model that answered, and a mismatch with the requested model is flagged and kept out of router learning (RTE-11).
- Arpeggio does not support quota evasion. Config validation rejects two providers with the same kind and endpoint (QTA-04), and the project will not integrate rotation, MITM or credential-reuse features.
- Every provider declares `data_use`. `private` and `client` repos only reach providers marked `no_training`, unless the repo opts in with `allow_training_providers = true` (SAF-07). A gateway counts as its own provider for privacy purposes (SAF-08).

## Alternatives considered

| Option | Pros | Cons |
|---|---|---|
| Build our own multi-provider gateway | Full control over fallback and logging | Duplicates existing tools and adds a server to run |
| Integrate community gateways' rotation features | More free requests per day | Breaks provider terms, and accounts can be suspended |
| **Gateways as plain providers, with provenance and privacy rules (chosen)** | Works with any OpenAI-compatible gateway, learning data stays clean | Users must turn off gateway-side fallback or pin the model |

## Consequences

- Adapters must read the served model from each response, and models may list `response_model_aliases` for names a provider returns for the same model (CFG-10).
- Free-tier providers default to `data_use = "unknown"`, so private repos cannot use them until the user verifies the terms or opts in.

## Revisit when

Providers change their free-tier terms materially, for example on training on submitted data or on rate limits.

## Amendment (2026-10-07)

Every packaged template ships its providers with `data_use = "unknown"`, so a per-repo opt-in alone would make a private repo unusable until each repo is edited. The opt-in now has two levels:

- A global `[privacy] allow_training_providers` (default `false`) in `~/.arpeggio/config.toml`.
- The repo's `[repo] allow_training_providers` wins when it is set, in either direction.
- `client` repos ignore the global value. They opt in only with an explicit repo-level `true`, because client code is governed by agreements the global setting knows nothing about.

`arpeggio init` prints one line when the chosen template has a provider with `data_use = "unknown"`, asking the user to check that provider's data policy. Enforcement in routing still arrives with M1.13.
