# Data model

One SQLite database (`data/sachin-agent.db`). Every table is a SQLModel class in
`shared/schemas/`: the Pydantic model *is* the table, so modules share types,
not just column names.

## Rules

1. **Frozen schemas.** Once a table ships, fields are only ever **added**:
   never renamed, removed or retyped. A rename would need a `_v2` field and a
   deprecation. Every phase so far has been additive (`title_norm`,
   `requirements`, `features`, `diagnosis`, and new enum values).
2. **Deterministic ids** for anything that comes from outside (see
   [architecture.md](architecture.md#identity-and-idempotency)). Never `uuid4`,
   except for events (`failures`, `pipeline_runs`).
3. **Status changes go through `transition()`.**
4. **Migrations, not `create_all`.** `sachin-agent-db init` and `upgrade` run
   Alembic with `render_as_batch=True` (SQLite can't `ALTER` in place). Tests
   use `fast_create()` on a fresh temporary DB.
5. **Enum columns** store member *names* in `VARCHAR(n)` sized at creation.
   SQLite ignores the length, so new enum values are safe. On Postgres those
   columns would need widening.

Migrations, oldest first: `initial_schema` → `job_normalization_and_dedup_fields`
→ `profile_facts` → `tailored_documents_and_job_requirements` →
`consent_choices` → `pipeline_runs` → `snapshot_features` → `failure_diagnosis`.
Each was round-trip tested (upgrade → downgrade → upgrade) on the real DB.

## Tables

### `jobs`: written by the scraper; status and score by the matcher

| Field | Notes |
|---|---|
| `id` | `sha256(source \| external_id)` |
| `source` | `greenhouse:stripe`, `remoteok`, … (family `:` board) |
| `external_id`, `title`, `company`, `url`, `description_raw` (HTML stripped), `posted_at` | from the source |
| `location_raw`, `location_norm` | raw and canonical (`"bengaluru \| remote"`) |
| `remote`, `salary_min`, `salary_max`, `salary_currency`, `skills` | when the source has them |
| `title_norm`, `company_norm` | canonical keys for dedup and prefiltering |
| `duplicate_of` | id of the canonical copy, or null |
| `status` | `new → scored → approved → applied` (plus `rejected`, `failed`) |
| `score`, `score_breakdown` | overall 1–5 and the stored score: method, per-dimension scores with reasons and cited fact ids, weights, prompt, model, profile version, citation issues |
| `requirements` | cached JD analysis: must-haves, nice-to-haves, keywords, plus its prompt version |
| `first_seen_at`, `last_seen_at` | re-scrapes only move `last_seen_at` |

A re-scrape may overwrite only a whitelist (`shared/db/upsert.py`): title,
company, description, URL, dates, salary, remote, skills, location and
normalized keys. Never status, score, `first_seen_at` or `duplicate_of`.

### `profile_records` and `profile_facts`

- `profile_records`: one row per profile save (`version`, full `profile_json`,
  `source` = init / import / manual / restore / teaching, `notes`).
- `profile_facts`: the citable facts (see [anti-hallucination.md](anti-hallucination.md)):
  `id` (stable `F…`), `kind`, `text`, `path`, `parent_id`, `data`, `pii`,
  `evidence` (the CV line), `verified`, `active`, `first_profile_version`,
  `last_profile_version`.

### `tailored_documents`: written by the matcher, read by the applier

`id = sha256(job_id | kind)`; `kind` is resume or cover_letter; `pdf_path`,
`html_path`; `fact_ids` (every fact the document states); `verified`;
`violations` (from rejected drafts); `fallback`; `attempts`;
`prompt_version`, `model`, `profile_version`.

### `applications`: written by the applier

`id = sha256("application" | job_id)`; `job_id`; `status`
(pending / paused / submitted / failed / abandoned); `pause_reason` (captcha,
login, consent, unknown_field, submit_approval, kill_switch, adapter_approval);
`adapter_version_id` (`greenhouse.io@v1`); `resume_path`, `cover_letter_path`;
`browser_state_path` (cookies for resuming); `last_step`; `submitted_at`.
The step-by-step log is `data/applications/<id>/steps.jsonl`.

### `consent_choices`

Your decisions, per `domain`: `kind` (`banner` = cookie wall,
`acknowledgement` = form question), `prompt`, `choice` (the exact button or
option text), `decided_by`, `decided_at`. The agent replays these and never
makes new ones.

### `snapshots`

`id` (`<run_id>-<step>`, or `<run_id>-<source>-<board>` for API snapshots);
`run_id`; `domain`; `url`; `intent` (`open`, `filled`, `paused_captcha`,
`inspect`, `api:fetch`, …); `step_index`; `html_path` (the raw JSON for API
snapshots); `a11y_path`, `har_path`, `screenshot_path`, `trace_path`;
`page_signature`; `features`.

### `failures`

`id`; `source_module`; `phase` (`fetch`, `normalize`, `score`, `tailor_resume`,
`fill`, `submit`, `pause_captcha`, …); `domain`; `job_id`, `application_id`,
`snapshot_id`; `error_type`, `error_message`; `context` (for drift: the intent,
locator and adapter version); `failure_class` (provisional until diagnosed);
`diagnosed_at`; `diagnosis` (method, confidence, evidence, emitter's class,
`healed_by`).

### `adapter_versions`

`id = sha256(domain | v<N>)`; `domain`; `version`; `parent_version_id`;
`adapter_code_path` (the spec **JSON**: adapters are data);
`fix_summary`; `evidence` (failure, snapshots, every trial and its rates);
`snapshot_pass_rate`; `source` (`builtin`, `synthesized`, `human_approved`);
`promoted_at`; `is_active` (one per domain).

### `source_health`

Per board (`greenhouse:stripe`): `success_count` and `failure_count` (*runs*,
not jobs), `last_success`, `last_error`, `last_error_message`.

### `pipeline_runs`

`id`; `trigger` (manual / timer); `status` (running / succeeded / partial /
killed / failed); `stages` (name, argv, exit code, seconds, output tail);
`summary` (job and application changes, failures); `started_at`, `finished_at`.

### `skills`

Defined since Phase 0 for the Phase 14 skill library. Not used yet.

## Files outside the database

| Path | Contents | Committed? |
|---|---|---|
| `profile-data/profile.json`, `profile.md` | your profile | no (PII) |
| `profile-data/resumes/`, `cover_letters/` | tailored PDFs and HTML | no (PII) |
| `data/applications/<id>/` | steps log, storage state, page dumps | no |
| `data/adapters/<domain>/v<N>.json` | promoted adapter specs | no |
| `data/calibration.json`, `scoring_weights.json` | your labels and tuned weights | no |
| `snapshots/<run_id>/` | DOM, ARIA, HAR, trace, screenshots, API payloads | no |
| `browser_profile/` | the persistent Chromium profile (cookies, logins) | no |
| `scraper/companies.json` | your boards | no (`companies.example.json` is) |
| `.env` | keys | no |
