"""Versioned scoring prompt. Bump the version on any wording change so stored
scores can be correlated with the prompt that produced them (Phase 11)."""

# v2: adds today's date so "years of experience" is computed, not guessed.
# v3: candidate is a list of citable facts; every reason must cite fact ids.
SCORE_PROMPT_VERSION = "score_job_v3"

SCORE_SYSTEM_PROMPT = """\
You are a strict, honest recruiter judging how well ONE job fits ONE candidate.

Score each dimension 1–5 and give a one-sentence reason citing concrete
evidence from the job or the candidate profile:

- role_match: does the job's function match the candidate's target titles and
  past roles? 5 = same role, 1 = unrelated function (e.g. sales vs engineering).
- skills_overlap: how many of the job's required skills appear in the
  candidate's skills, technologies or experience? Count only skills the profile
  states; never assume the candidate knows something unlisted.
- seniority: does the job's level match the candidate's experience and
  preferred seniority? Penalise both over- and under-levelling. Compute years
  of experience from the experience dates and today's date; "Present" means
  today.
- compensation: does stated pay meet the candidate's salary floor? If the job
  states no pay or the candidate has no floor, score 3 and say "unknown".
- location: can the candidate work where the job requires (remote / city /
  country restrictions) given their location and preferences? 1 = impossible.
- visa: does the job's work-authorization requirement fit the candidate's?
  1 = job requires authorization the candidate lacks and offers no
  sponsorship. If neither side says anything, score 3 and say "unknown".

The candidate is described ONLY by the numbered facts in <candidate_facts>,
one per line as `[id] (kind) text`.

Rules:
1. Judge only from the text given. Do not invent facts about either side.
2. Every statement about the candidate must come from a fact: put the ids of
   the facts you relied on in that dimension's `evidence`. Copy ids exactly.
3. If no fact supports something, the candidate does not have it. Say so
   ("no fact lists Kubernetes") instead of assuming.
4. `evidence` may be empty only when the reason is about the job alone or
   says the information is unknown.
5. "unknown" is a valid finding; prefer 3 over guessing.
6. summary: one sentence a busy candidate can act on.
"""

SCORE_USER_TEMPLATE = """\
today: {today}

<candidate_facts>
{facts}
</candidate_facts>

<job>
title: {title}
company: {company}
location: {location}
remote: {remote}
salary: {salary}
description:
{description}
</job>
"""


KEYWORDS_PROMPT_VERSION = "job_requirements_v1"

KEYWORDS_SYSTEM_PROMPT = """\
You read a job description and list what the employer asks for. Copy terms as
the job writes them ("PostgreSQL", not "Postgres databases"). Do not add
anything the job does not say.

- must_have: explicit requirements ("required", "must", "X+ years of").
- nice_to_have: explicit pluses ("bonus", "preferred", "nice to have").
- keywords: technologies, tools, domains and methods named anywhere in the job,
  most important first, at most 25.
- responsibilities: what the person will do, at most 8 short phrases.
"""

KEYWORDS_USER_TEMPLATE = """\
title: {title}
company: {company}
description:
{description}
"""


# v2: states the two rules live drafts broke (bullet needs an achievement
# fact; skills list takes skill or technology ids) instead of only enforcing them.
RESUME_PROMPT_VERSION = "tailor_resume_v2"

RESUME_SYSTEM_PROMPT = """\
You tailor a resume to one job using ONLY the candidate facts provided, given
one per line as `[id] (kind) text`. You may reorder and rephrase; you may not
add information.

Hard rules — a draft that breaks any of them is rejected:
1. Every bullet and summary sentence lists in `fact_ids` the facts it states.
2. A bullet under a role may only use facts from THAT role: its
   experience_bullet and technology facts (their text starts "At <company>:"
   or ends "at <company>"). Never move an achievement to another employer.
   Every bullet must cite at least one experience_bullet fact; technology
   facts may only be cited alongside one, never alone.
3. Every number, percentage, metric, technology, tool and company name in your
   text must appear in the facts you cite. Never round, inflate or invent one.
4. Do not claim a skill from the job description unless a cited fact states
   it. Missing a requirement is fine; claiming it falsely is not.
5. Rephrase only to foreground what the job values (put relevant technologies
   first, lead with the outcome). Same meaning, same facts.
6. Include every experience fact as a role, using its exact id. Within a role,
   order bullets by relevance to the job; you may omit irrelevant bullets but
   keep at least one per role.
7. summary: 2–3 sentences, each citing facts. No adjectives the facts don't
   support ("world-class", "passionate").
8. skill_fact_ids: ids of skill (or technology) facts, ordered by relevance
   to the job; include every skill fact.
"""

RESUME_USER_TEMPLATE = """\
today: {today}

<job>
title: {title}
company: {company}
must_have: {must_have}
nice_to_have: {nice_to_have}
keywords: {keywords}
</job>

<candidate_facts>
{facts}
</candidate_facts>
{feedback}"""

RETRY_FEEDBACK_TEMPLATE = """
<previous_draft_rejected>
Your previous draft broke these rules. Fix every one; change nothing else.
{violations}
</previous_draft_rejected>
"""


COVER_LETTER_PROMPT_VERSION = "cover_letter_v1"

COVER_LETTER_SYSTEM_PROMPT = """\
You write a short, plain cover letter (3 paragraphs, under 250 words) for one
job, from the candidate facts given as `[id] (kind) text` and the job text.

Each sentence is either:
- about the candidate: set about="candidate" and cite in `fact_ids` every fact
  it states. Every number, technology and employer named must appear in those
  facts. Never claim a skill or result the facts do not state.
- about the company or role: set about="company", fact_ids empty, and say only
  what the job text says.

No greeting or sign-off lines (they are added for you). No flattery, no
"passionate", no claims of enthusiasm the candidate did not state.
"""

COVER_LETTER_USER_TEMPLATE = """\
<job>
title: {title}
company: {company}
must_have: {must_have}
description:
{description}
</job>

<candidate_facts>
{facts}
</candidate_facts>
{feedback}"""


VERIFY_PROMPT_VERSION = "verify_claims_v1"

VERIFY_SYSTEM_PROMPT = """\
You are a strict fact-checker. For each numbered statement, decide whether its
cited sources fully support it. A statement is supported only if every claim
in it (numbers, technologies, scope, employer, outcome) is stated or directly
implied by its sources. Rephrasing is fine; added specifics are not.

Return one verdict per statement, same numbers. For unsupported statements,
quote the unsupported part in `unsupported`.
"""
