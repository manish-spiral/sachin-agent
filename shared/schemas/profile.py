"""The applicant profile — single source of truth. Validated strictly."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    first_name: str = ""
    last_name: str = ""
    email: EmailStr | None = None
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""

class WorkAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requires_sponsorship: bool | None = None
    authorized_countries: list[str] = Field(default_factory=list)
    visa_type: str | None = None


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_titles: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    salary_floor: int | None = None
    excluded_companies: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = ""
    title: str = ""
    start_date: str = ""
    end_date: str = ""
    bullets: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


class Education(BaseModel):
    model_config = ConfigDict(extra="forbid")
    institution: str
    degree: str = ""
    field: str = ""
    start_date: str = ""
    end_date: str = ""


class Skills(BaseModel):
    model_config = ConfigDict(extra="forbid")
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    url: str = ""
    technologies: list[str] = Field(default_factory=list)


class Certification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    issuer: str = ""
    date: str = ""


class Answer(BaseModel):
    """A fact the CV doesn't hold but applications ask for (notice period,
    relocation, ...). Stating it here means the agent never has to guess it."""

    model_config = ConfigDict(extra="forbid")
    question: str
    answer: str


class Profile(BaseModel):
    """Frozen top-level shape. Only additive changes permitted."""

    model_config = ConfigDict(extra="forbid")

    identity: Identity = Field(default_factory=Identity)
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    preferences: Preferences = Field(default_factory=Preferences)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: Skills = Field(default_factory=Skills)
    projects: list[Project] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    answers: list[Answer] = Field(default_factory=list)
    finalized: bool = False

        # --- Anti-fabrication gate -------------------------------------------
    @model_validator(mode="after")
    def _finalized_requires_complete(self) -> Profile:
        if self.finalized:
            issues = self.completeness_issues()
            if issues:
                raise ValueError(
                    "Cannot set finalized=true while incomplete:\n- "
                    + "\n- ".join(issues)
                )
        return self

    def completeness_issues(self) -> list[str]:
        """Return every reason this profile cannot be finalized."""
        issues: list[str] = []
        if not self.identity.first_name.strip():
            issues.append("identity.first_name is empty")
        if not self.identity.last_name.strip():
            issues.append("identity.last_name is empty")
        if self.identity.email is None:
            issues.append("identity.email is empty")
        if not self.experience and not self.education:
            issues.append("at least one experience or education entry is required")
        for i, exp in enumerate(self.experience):
            if not exp.company.strip() or not exp.title.strip():
                issues.append(f"experience[{i}] is missing company or title")
        return issues