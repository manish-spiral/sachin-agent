# Status (as of Phase 13)

All work so far used a **dummy persona** (`examples/dummy_cv.md`, "Arjun
Mehta") and real job data. **No real application has ever been submitted**:
live form checks were read-only (`applier inspect`), and fill/submit was tested
against an offline fixture that mirrors a real Greenhouse form.

227 tests pass (offline: fixtures, mock HTTP, fake LLM); ruff and mypy are clean.

## Phase by phase

| Phase | What it delivered | Verified live | Not yet / caveats |
|---|---|---|---|
| 0 Repo & boundaries | modules, schemas, CLIs, boundary test | ✓ | |
| 1 Profile | schema + finalize gate, import, history | ✓ dummy CV imported via Groq | |
| 2 Database | Alembic, deterministic ids, transitions, upsert | ✓ migrations round-trip | |
| 3 Scraper v1 | Greenhouse, Lever, Ashby; failure isolation | ✓ 730 Stripe jobs, idempotent | |
| 4 Scraper v2 | +Workable, SmartRecruiters, RemoteOK, Remotive, Arbeitnow; normalization; dedup; health | ✓ 3,958 jobs from 11 boards; 23 duplicates (0.6%), each checked by hand | "nightly cron clean for 3 nights" not observed |
| 5 Matcher | rubric scoring, prefilter, review, calibration; fact citations | ✓ 47 jobs LLM-scored; top of queue sensible | calibration 85% used **proxy labels** (mine, for the dummy persona), not yours |
| 6 Tailor | fact-cited drafts, verifier, fallback, PDF, ATS check | ✓ 10 jobs: all verified, all PDFs pass the ATS check | all 10 were **profile-only fallbacks**: real drafts were caught embellishing or hit the Groq quota. Tailored drafts need more quota or a stronger writer |
| 7 Applier v1 | session, spec adapters, supervised runner | ✓ read-only inspect of a real Vercel form | "5 real applications" not done (dummy data; never submit) |
| 8 Human-in-the-loop | detectors, Telegram, consent memory, teaching, inference gate | fixture | Telegram untested live (no token configured) |
| 9 Orchestration | run-all, caps, kill switch, summaries, systemd timer | ✓ live run-all composed correctly | overnight unattended runs not observed |
| 10 Snapshots | DOM/ARIA/HAR/trace per step, API payloads, signatures, offline replay | ✓ real form captured and replayed offline | |
| 11 Diagnosis | rules + LLM classifier, escalations as failures, stats | ✓ 49 real failures all classified; baseline recorded | |
| 12 Replay sandbox | replay, suites, harness, suite gate | ✓ 5/5 real snapshots in 3.1s; broken spec 0/5 | |
| 13 Selector healing | versioned specs, healer, promotion gate, rollback | ✓ broken spec healed from real snapshots, verified live read-only, rolled back | |
| 4b Browse cluster | LinkedIn read with your own session: opt-in `--ok`, read-only, human-paced, stops at checkpoints | ✓ listing spec + fallback checked on one real public page (60 cards) | logged-in run needs your `browse-login` |
| 14 Skill library | — | | not started |
| 15 Adapter synthesis | — | | not started |
| 16 Teaching UI, meta-reflector, metrics | — | | not started (parts exist: answers bank teaching, consent memory) |

## Where the build deviates from the brief, and why

- **Adapters are declarative specs, not Python.** The brief has the
  synthesizer generate adapter *code*. Here one fixed executor runs JSON specs,
  so healing and synthesis can only change locators, never code. That's safer
  (constraint 15), and makes candidates trivially testable and diffable.
- **The fact database** (`profile_facts`) goes beyond the brief's "profile is
  the source of truth": every claim cites a fact id, and code checks the ids.
- **Cross-family verification.** The resume fact-check runs on a different
  model family from the writer.
- **Groq instead of OpenAI** as the working provider (OpenAI is still
  supported). Free-tier quotas shaped several decisions: per-task models,
  prioritized scoring, quota-aware stopping.
- **Escalating pauses are failure events**, so escalation rate is measurable
  from the same table.
- **An `orchestrator` package and a `sachin-agent-run` CLI** were added for
  `run-all`; `sachin-agent-sandbox` hosts the offline tools. Both are covered
  by boundary tests.
- **Page signatures ignore per-job questions**, so all jobs on one ATS share a
  signature. Similarity uses the full feature vector.

## Known limitations

- **One ATS adapter** (Greenhouse). Other sources' jobs fail as
  `unseen_site` until Phase 15 synthesizes adapters (or a spec is written by hand).
- **Greenhouse custom dropdowns** on real pages are React-select widgets. The
  executor handles ARIA comboboxes (tested on the fixture), but filling has
  only been exercised read-only on real pages, so expect some unknowns to
  route to you at first.
- **Groq free tier**: about 60 scored jobs a day; tailoring mostly falls back.
- **Lever, Ashby, Workable, SmartRecruiters company names** are board slugs
  (only Greenhouse provides display names).
- **Location normalization** is alias-based; a city outside the alias table
  stays as written.
- **Calibration and the review queue** were judged by a proxy, not by you.
- **SQLite only**; enum columns would need widening on Postgres.

## What's next

- **14 Skill library**: turn each promoted fix into a reusable skill keyed by
  page signature and goal; retrieve similar skills (cosine over features)
  before proposing new ones; demote skills that stop working.
- **15 Adapter synthesis**: for unseen ATSs, generate a *spec* from the ARIA
  tree with exemplar specs, validate it in the sandbox (isolated subprocess,
  HAR-only network), and pause the first live run for your approval.
- **16 Teaching UI and metrics**: structured resolutions when paused;
  a nightly meta-reflector (consolidate and prune skills, re-validate stale
  ones); a dashboard for first-attempt success, autonomous recovery,
  **escalation-rate trend**, skill reuse, cost per application, and outcomes
  (responses and interviews you record).

## Before using it for real

1. Import your real CV with `--force` and review every flagged item.
2. Fill `preferences`, `work_authorization` and `answers`, then finalize.
3. Redo calibration with **your** labels (`matcher calibrate label`).
4. Set up Telegram.
5. Keep `AUTO_APPROVE_MIN_SCORE` unset at first, and apply with
   `--headed --interactive` for the first few on each site.
