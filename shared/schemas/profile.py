"""The applicant profile — single source of truth. Validated strictly."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class Identity(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class WorkAuthorization(BaseModel):
    requires_sponsorship: bool | None = None
    authorized_countries: list[str] = Field(default_factory=list)
    visa_type: str | None = None


class Preferences(BaseModel):
    target_titles: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    salary_floor: int | None = None
    excluded_companies: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    company: str
    title: str
    start_date: str = ""
    end_date: str = ""
    bullets: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


class Education(BaseModel):
    institution: str
    degree: str = ""
    field: str = ""
    start_date: str = ""
    end_date: str = ""


class Skills(BaseModel):
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)


class Project(BaseModel):
    name: str
    description: str = ""
    url: str = ""
    technologies: list[str] = Field(default_factory=list)


class Certification(BaseModel):
    name: str
    issuer: str = ""
    date: str = ""


class Profile(BaseModel):
    """Frozen top-level shape. Only additive changes permitted."""

    identity: Identity
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    preferences: Preferences = Field(default_factory=Preferences)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: Skills = Field(default_factory=Skills)
    projects: list[Project] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    finalized: bool = False