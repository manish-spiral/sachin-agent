"""LLM help for questions no rule or answers-bank entry covers.

The LLM may only *derive* an answer from facts it is shown (e.g. "Are you
based in the US?" from "Based in Bengaluru, India" → "No"), must cite them,
and must be confident. Anything short of that stays unknown and goes to the
human — this is the `detect_unknown_field` confidence gate.
"""

from __future__ import annotations

from loguru import logger
from pydantic import BaseModel, ConfigDict

from applier.context import Answer, AnswerSource
from shared.llm import StructuredLLM, Task
from shared.profile.facts import format_facts_for_prompt, unknown_citations
from shared.schemas.profile_fact import ProfileFact

PROMPT_VERSION = "form_answer_v1"
CONFIDENCE_THRESHOLD = 0.85

SYSTEM_PROMPT = """\
You answer one job-application question for a candidate, using ONLY the
candidate facts given as `[id] (kind) text`.

- Answer only if the facts directly determine the answer. Cite their ids.
- If options are given, the answer must be one option, copied exactly.
- If the facts do not settle it (opinions, preferences not stated, anything
  you would have to guess), return answer "" with confidence 0.
- confidence: 0–1, how certain the cited facts make the answer.
"""


class InferredAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str
    fact_ids: list[str]
    confidence: float


class FactInferrer:
    def __init__(self, llm: StructuredLLM, facts: list[ProfileFact]) -> None:
        self.llm = llm
        self.facts = [f for f in facts if not f.pii]

    def __call__(self, question: str, options: list[str] | None) -> Answer | None:
        user = (
            f"question: {question}\noptions: {options or 'free text'}\n\n"
            f"<candidate_facts>\n{format_facts_for_prompt(self.facts)}\n</candidate_facts>"
        )
        try:
            result = self.llm(Task.FORM_ANSWER, SYSTEM_PROMPT, user, InferredAnswer)
        except Exception as exc:
            logger.warning(f"answer inference failed, asking the human instead: {exc}")
            return None
        shown = [f.id for f in self.facts]
        if (
            not result.answer.strip()
            or result.confidence < CONFIDENCE_THRESHOLD
            or not result.fact_ids
            or unknown_citations(result.fact_ids, shown)
            or (options and result.answer not in options)
        ):
            return None
        return Answer(result.answer, AnswerSource.INFERRED, ",".join(result.fact_ids))
