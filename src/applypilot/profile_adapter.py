"""Adapter: unified user_profile.yaml -> applypilot's legacy profile.json shape.

Phase 2 of the job-hunt unification. The canonical profile lives in
linkedin-leads/profile/user_profile.yaml with an `ats:` section added for
form-fill fields. This adapter renders the exact dict shape applypilot's
existing code expects (personal, work_authorization, compensation,
experience, skills_boundary, resume_facts, eeo_voluntary, availability) so
no call site needs to change.

Precedence: ats.identity_extra fields override top-level identity where they
overlap (e.g., an ATS-specific email / phone). Top-level identity is the
fallback so minimal YAML configurations still work.
"""

from pathlib import Path

import yaml


def load_profile_from_yaml(yaml_path: Path) -> dict:
    """Render legacy profile.json shape from a unified user_profile.yaml.

    Args:
        yaml_path: Path to user_profile.yaml.

    Returns:
        Dict matching the shape produced by a direct json.loads() of
        applypilot's legacy profile.json.
    """
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    identity = data.get("identity") or {}
    ats = data.get("ats") or {}
    extra = ats.get("identity_extra") or {}

    full_name = identity.get("name", "")
    first_name = full_name.split()[0] if full_name else ""

    def pick(a: str, b: str) -> str:
        return extra.get(a) or identity.get(b, "") or ""

    personal = {
        "full_name": full_name,
        "preferred_name": extra.get("preferred_name") or first_name,
        "email": pick("email", "email"),
        "phone": pick("phone", "phone"),
        "city": extra.get("city", ""),
        "province_state": extra.get("province_state", ""),
        "country": extra.get("country", ""),
        "postal_code": extra.get("postal_code", ""),
        "address": extra.get("address", ""),
        "linkedin_url": pick("linkedin_url", "linkedin"),
        "github_url": pick("github_url", "github"),
        "portfolio_url": pick("portfolio_url", "website"),
        "website_url": extra.get("website_url", ""),
        "password": extra.get("password", ""),
    }

    return {
        "personal": personal,
        "work_authorization": ats.get("work_authorization") or {},
        "compensation": ats.get("compensation") or {},
        "experience": ats.get("experience") or {},
        "skills_boundary": ats.get("skills_boundary") or {},
        "resume_facts": ats.get("resume_facts") or {},
        "eeo_voluntary": ats.get("eeo_voluntary") or {},
        "availability": ats.get("availability") or {},
    }
