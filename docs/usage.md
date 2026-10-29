# Using sachin-agent

## Setup

```bash
uv venv && source .venv/bin/activate
make init                        # uv pip install -e ".[dev]"
playwright install chromium      # used for PDFs, applying and replay
cp .env.example .env
sachin-agent-db init             # applies all migrations to data/sachin-agent.db
cp scraper/companies.example.json scraper/companies.json   # then edit boards
```

Requirements: Python 3.11+, `uv`, network access to the job boards, and an LLM
key (Groq or OpenAI).

### Telegram (optional, recommended)

Pauses and nightly summaries are sent to Telegram; without it they're only logged.

1. Message **@BotFather** → `/newbot` → copy the token.
2. Send your new bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
3. Put both in `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

## Configuration (`.env`)

Every setting is an environment variable (case-insensitive) read by
`shared/config.py`.

| Setting | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///data/sachin-agent.db` | the one database |
| `LOG_LEVEL` | `INFO` | logs go to stderr and `logs/<module>.log` |
| `DATA_DIR`, `SNAPSHOT_DIR`, `LOGS_DIR`, `PROFILE_DATA_DIR`, `BROWSER_PROFILE_DIR` | `data`, `snapshots`, `logs`, `profile-data`, `browser_profile` | where things live |
| `GROQ_API_KEY` / `OPENAI_API_KEY` | – | LLM key; whichever is set is used (OpenAI first) |
| `LLM_PROVIDER` | auto | force `groq` or `openai` |
| `EXTRACTION_MODEL`, `SCORING_MODEL` | provider default | override one task's model |
| `LLM_MODELS` | `{}` | JSON per-task overrides, e.g. `{"resume": "openai/gpt-oss-20b"}`; tasks: extraction, scoring, keywords, resume, cover_letter, verify, form_answer, diagnose, heal |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | – | notifications |
| `MAX_APPLICATIONS_PER_DAY` | `5` | applications *started* per UTC day by `run-approved` / `run-all` |
| `AUTO_APPROVE_MIN_SCORE` | unset | unset: only jobs you approve are applied to. Set (e.g. `4.0`): `run-all` approves scored jobs at or above it. Submission still waits for you |
| `EXCLUDE_COMPANIES`, `EXCLUDE_TITLE_KEYWORDS` | `[]` | JSON lists honoured by auto-approval, e.g. `["Globex"]`, `["manager"]` |
| `SCORE_LLM_CALLS_PER_RUN` | `60` | scoring budget per `run-all` |
| `TAILOR_PER_RUN` | `10` | approved jobs tailored per `run-all` |
| `CAPTURE_SNAPSHOTS` | `true` | record DOM/ARIA/HAR/trace per step and API payloads |
| `SNAPSHOT_RETENTION_DAYS` | `30` | used by `sandbox prune` |
| `KILL_SWITCH` | unset | any value stops everything (as does the file `data/KILL`) |

**Groq free tier.** About 200k tokens/day and 8k tokens/minute *per model*.
That's roughly 60 scored jobs a day on `gpt-oss-120b`. The client waits out
per-minute limits; when the daily quota runs out, a scoring run stops cleanly
and leaves the rest queued for the next run. Tailoring falls back to the
profile-only resume.

## Commands

Every CLI has `--help`. Ids can be given as unique prefixes.

### `sachin-agent-profile`

| Command | Does |
|---|---|
| `init` | empty draft profile |
| `import CV [--force]` | LLM extraction + faithfulness check; refuses to overwrite without `--force` |
| `validate` | what's missing before you can finalize |
| `facts [--kind K] [--evidence]` | the citable facts (✓ verified, 🔒 PII) and their CV lines |
| `render` | `profile-data/profile.md` |
| `history`, `restore N` | versions; restoring adds a new version |

### `sachin-agent-scraper`

| Command | Does |
|---|---|
| `run [-s SOURCE] [-c a,b] [-l N] [--force]` | fetch, normalize, upsert, dedup; `--force` ignores ToS intervals |
| `sources` | sources, default boards, intervals |
| `health [--by-source]` | per-board run success rate, last success, last error |
| `stats [--top N]` | totals, duplicate rate, by source and location |
| `normalize` | recompute normalized keys for all jobs, then dedup |

### `sachin-agent-matcher`

| Command | Does |
|---|---|
| `run [--limit N] [--rescore]` | score new jobs (N = max LLM calls) |
| `review [-n N] [--min-score S] [--list]` | top jobs with reasons; approve / reject / skip |
| `approve ID`, `reject ID` | one job |
| `auto-approve --min-score S [--limit N] [--exclude-company C] [--exclude-title K]` | policy approval |
| `stats` | status counts, score histogram, mean per dimension |
| `tailor ID` / `--top N` / `--approved N` `[--no-cover-letter]` | verified PDFs |
| `calibrate label [--n 20]`, `calibrate report [--apply]` | compare with your judgment, tune weights |

### `sachin-agent-applier`

| Command | Does |
|---|---|
| `inspect JOB` | **read-only** look at the live form: what would be filled, what needs you; captures a snapshot |
| `run JOB [--headed] [--interactive]` | fill; with both flags you answer questions and approve in the terminal while watching the browser; otherwise it pauses |
| `run-approved [--limit N]` | unattended: every ready approved job, within the daily cap |
| `list [--status S]` | applications and why they're paused |
| `answer APP` | answer the questions a paused application waits on (saved to your answers bank) |
| `resume APP [--headed]` | continue a paused application with its saved session |
| `consent list [DOMAIN]`, `consent set DOMAIN PROMPT CHOICE [--kind banner\|acknowledgement]`, `consent forget DOMAIN` | your per-domain decisions |
| `adapters DOMAIN`, `rollback DOMAIN VERSION` | adapter version history and rollback |

### `sachin-agent-run`

| Command | Does |
|---|---|
| `run-all [--no-scrape] [--no-notify] [--trigger timer]` | the whole pipeline + summary |
| `kill`, `resume` | kill switch on / off |
| `history` | past runs and per-stage exit codes |

### `sachin-agent-sandbox`

| Command | Does |
|---|---|
| `list [--domain D] [--run R]` | snapshots |
| `replay SNAP [--js] [--screenshot out.png]` | open a snapshot offline |
| `suite DOMAIN [--spec FILE] [--min-pass 1.0]` | replay an adapter over all stored forms; exit 1 below the bar |
| `diagnose [--rules-only] [--redo]` | classify failures |
| `heal FAILURE / --pending [--llm]` | heal selector drift offline; promote if it passes |
| `prune [--days N]` | delete old snapshot runs |

### `sachin-agent-db`

`init`, `upgrade [REV]`, `downgrade [REV]`, `current`, `migrate -m MSG`,
`stamp`, `stats`, `drop`, `failures stats [--days N]`, `failures list`.

## Workflows

### First time

```bash
sachin-agent-profile import examples/dummy_cv.md   # or your CV
$EDITOR profile-data/profile.json   # preferences, work_authorization, answers, "finalized": true
sachin-agent-profile validate && sachin-agent-profile facts
sachin-agent-scraper run
sachin-agent-matcher run --limit 30
sachin-agent-matcher review
sachin-agent-matcher calibrate label && sachin-agent-matcher calibrate report --apply
```

Things worth putting in `profile.json` because the CV can't contain them:
`preferences.target_titles` (drives the prefilter), `preferred_locations`,
`excluded_companies`, `work_authorization` (`requires_sponsorship` and
`authorized_countries` drive visa scoring and form answers), and `answers` for
questions you get on every form.

### Applying to one job, watching

```bash
sachin-agent-matcher approve <job>
sachin-agent-matcher tailor <job>
open profile-data/resumes/<job-id>.pdf             # check it
sachin-agent-applier inspect <job>                 # read-only: what will it need?
sachin-agent-applier run <job> --headed --interactive
```

### Unattended

```bash
sachin-agent-run run-all          # or: make install-timer  (nightly 02:00)
```

In the morning, Telegram has the summary, plus one message per pause.

```bash
sachin-agent-applier list --status paused
sachin-agent-applier answer <app>                 # for unknown_field
sachin-agent-applier resume <app> --headed        # review the filled form, approve, submit
```

### When something breaks

```bash
sachin-agent-db failures stats                    # what's failing, where
sachin-agent-sandbox diagnose                     # classify new failures
sachin-agent-sandbox heal --pending               # repair selector drift offline
sachin-agent-sandbox suite greenhouse.io          # confirm the active adapter on all snapshots
sachin-agent-applier adapters greenhouse.io       # what changed; rollback if needed
```

### Stopping everything

`sachin-agent-run kill`, or `touch data/KILL`, or `KILL_SWITCH=1`. Applications
in flight pause with their state saved. Undo with `sachin-agent-run resume`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Profile is not finalized` | `sachin-agent-profile validate`, fill what's missing, set `"finalized": true` |
| `No API key for LLM provider` | set `GROQ_API_KEY` or `OPENAI_API_KEY` in `.env` |
| scoring stops early with `rate limit` | Groq daily quota; it resumes next run. Or set a paid key, or move tasks with `LLM_MODELS` |
| every resume is a "profile-only fallback" | the LLM was out of quota, or every draft embellished; `tailored_documents.violations` says which |
| `Executable doesn't exist … chromium` | `playwright install chromium` |
| application `failed` with `selector_drift` | the site changed: `sandbox heal --pending` |
| application paused `consent` every time | decide once at the terminal, or `applier consent set DOMAIN "<question>" "<choice>"` |
| `Kill switch is on` | `sachin-agent-run resume` (and unset `KILL_SWITCH`) |
| a Remotive board is "skipped" | its ToS allows ≤4 calls a day; `--force` to override |

## Tests

```bash
make test      # pytest: ~230 tests, ~6 min; offline (fixtures, mock HTTP, fake LLM)
make lint      # ruff + mypy
```

Tests blank every API key and Telegram setting, so they can never call a real
service, and they use a fresh temporary database.
