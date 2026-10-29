# Safety: the non-negotiables and where they live

The project brief lists constraints that win over any feature. This maps each
one to the code that enforces it and the test that proves it.

| # | Constraint | Enforced in | Proven by |
|---|---|---|---|
| 1 | Three modules never import each other | `tests/test_boundaries.py` (AST scan), extended to `orchestrator`, `shared`, `sandbox` | the test itself |
| 3 | All communication through the DB | modules share only `shared/schemas`; the orchestrator runs CLIs as subprocesses | `test_orchestrator_only_talks_through_subprocesses` |
| 4 | Frozen schemas: add, never rename | every migration since Phase 2 is additive | migration history |
| 5 | Profile is the only source of applicant facts | `profile_facts`, citations, verifier, fact-only form answers ([anti-hallucination.md](anti-hallucination.md)) | `test_tailor.py`, `test_facts.py`, `test_hitl.py` |
| 5 | No fabrication on finalize | `Profile` has `extra="forbid"` and a `finalized` completeness gate; scoring/tailoring/applying refuse drafts | `test_profile.py`, `test_requires_finalized_profile` |
| 6 | Deterministic job ids | `make_job_id` = `sha256(source\|external_id)` | `test_upsert.py` |
| 7 | Idempotency | upserts, deterministic application/document ids, full-recompute dedup | `test_run_inserts_and_is_idempotent`, `test_dedup_is_idempotent…`, `already submitted` test |
| 8 | CAPTCHA, consent, login are hard stops | `applier/detectors.py` only detects; `Human` decides; consent memory replays only your own choice per domain | `test_hitl.py` |
| 9 | First run of a synthesized adapter is supervised | applications always pause before submit (below); Phase 15 adds an explicit approval gate | — |
| 10 | Pause before submit by default | `Human.approve` is required for every submit; `Absent` (unattended) never approves | `test_fills_from_facts…`, `test_human_resolves…` |
| 11 | ToS and rate limits | `Retry-After` honoured (capped), backoff on 429/5xx, per-source minimum intervals (Remotive ≤4/day), daily application cap | `test_scraper.py`, `test_runner_normalizes_and_respects_min_interval`, `test_daily_cap` |
| 12 | Capture before repair | `FailureEvent` from every module since Phase 3, with snapshot links | `test_diagnose.py`, `test_snapshots.py` |
| 13 | Never self-modify against the live site | healing reads snapshots; replay aborts all network; `ReplaySession` refuses navigation | `test_replay_is_offline_and_faithful`, `test_replay_session_cannot_reach_the_live_site` |
| 14 | Stage → validate → promote, reversible | adapters are versioned specs; promotion requires ≥90% on snapshots and an improvement; rollback is one command | `test_healer.py` |
| 15 | The safety core can't be self-modified | healing and synthesis only produce **specs** (locators); detectors, consent policy, approval gates, rate limits and PII handling are code the loop never writes | design: `applier/adapters/spec.py` |
| 16 | Optimize outcomes, not volume | daily cap; auto-approval off by default; scoring caps impossible jobs | settings defaults |
| 18 | Lazy imports of heavy deps | `openai` and `playwright` imported inside the functions that use them | — |

## Privacy

- **Contact details (PII) never reach an LLM.** Identity facts are flagged
  `pii`, prompts are built from non-PII facts only, and
  `format_facts_for_prompt` raises if one slips through. Citing a PII fact is a
  verifier violation. Resume headers are filled from facts *after* generation.
- **Demographic questions** (gender, race, disability, veteran status…) are
  never answered for you: the agent picks "decline" if offered, otherwise
  leaves them blank.
- **Keys**: `.env` only (gitignored); `LLMConfig` keeps the key out of its
  printed form; the Telegram token is never logged (it's in the URL, so only
  the error type is logged); tests blank all keys and Telegram settings.
- **Personal files** (`profile-data/`, `data/`, `snapshots/`,
  `browser_profile/`) are gitignored. Every commit was checked for the API key
  and for these paths before pushing.

## What the agent will never do

- solve a CAPTCHA, or accept a cookie wall or legal acknowledgement on its own;
- submit an application without your explicit approval of that filled form;
- state a skill, number, employer or result that isn't a profile fact;
- try a repair on a live site;
- run code it generated: repairs change data (locators), never code;
- keep going when the kill switch is on.
