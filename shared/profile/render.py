"""Render Profile to human-readable Markdown."""

from __future__ import annotations

from shared.schemas.profile import Profile


def render_markdown(p: Profile) -> str:
    status = "✅ finalized" if p.finalized else "📝 draft"
    lines: list[str] = [f"# {p.identity.first_name} {p.identity.last_name}", "", f"_{status}_", ""]

    contact = []
    if p.identity.email:
        contact.append(f"✉️ {p.identity.email}")
    if p.identity.phone:
        contact.append(f"📞 {p.identity.phone}")
    if p.identity.location:
        contact.append(f"📍 {p.identity.location}")
    if p.identity.linkedin:
        contact.append(f"[LinkedIn]({p.identity.linkedin})")
    if p.identity.github:
        contact.append(f"[GitHub]({p.identity.github})")
    if contact:
        lines += [" | ".join(contact), ""]

    if p.experience:
        lines += ["## Experience", ""]
        for exp in p.experience:
            dates = f"{exp.start_date} – {exp.end_date or 'Present'}"
            lines.append(f"### {exp.title} @ {exp.company}")
            lines.append(f"_{dates}_")
            for b in exp.bullets:
                lines.append(f"- {b}")
            if exp.technologies:
                lines.append(f"**Tech:** {', '.join(exp.technologies)}")
            lines.append("")

    if p.education:
        lines += ["## Education", ""]
        for ed in p.education:
            dates = f"{ed.start_date} – {ed.end_date}"
            lines.append(f"- **{ed.degree} {ed.field}**, {ed.institution} ({dates})")
        lines.append("")

    if p.skills:
        lines += ["## Skills", ""]
        for label, items in [
            ("Languages", p.skills.languages),
            ("Frameworks", p.skills.frameworks),
            ("Tools", p.skills.tools),
            ("Soft skills", p.skills.soft_skills),
        ]:
            if items:
                lines.append(f"- **{label}:** {', '.join(items)}")
        lines.append("")

    if p.projects:
        lines += ["## Projects", ""]
        for proj in p.projects:
            lines.append(f"- **{proj.name}** — {proj.description}")
            if proj.technologies:
                lines.append(f"  _Tech: {', '.join(proj.technologies)}_")
        lines.append("")

    if p.certifications:
        lines += ["## Certifications", ""]
        for c in p.certifications:
            lines.append(f"- {c.name} — {c.issuer} ({c.date})")
        lines.append("")

    if p.answers:
        lines += ["## Application answers", ""]
        for qa in p.answers:
            lines.append(f"- **{qa.question}** {qa.answer}")
        lines.append("")

    if not p.finalized:
        issues = p.completeness_issues()
        if issues:
            lines += ["## ⚠️ Missing before finalizing", ""]
            lines += [f"- {i}" for i in issues]

    return "\n".join(lines)