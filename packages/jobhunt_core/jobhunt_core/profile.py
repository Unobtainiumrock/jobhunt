"""Unified profile loader.

The canonical profile YAML lives at ``linkedin-leads/profile/user_profile.yaml``
with an ``ats:`` top-level section added for form-fill fields. This module
renders the dict shape that BetterApplyPilot's existing call sites expect
(``personal``, ``work_authorization``, ``compensation``, ``experience``,
``skills_boundary``, ``resume_facts``, ``eeo_voluntary``, ``availability``)
so no call-site needs to change when we migrate BAP from reading its own
``profile.json`` to reading the unified YAML.

Precedence rule: ``ats.identity_extra`` overrides top-level ``identity``
where they overlap (e.g., an ATS-specific email distinct from the
recruiter-facing one). Top-level identity is the fallback so minimal YAML
configurations still work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_profile_from_yaml(yaml_path: Path | str) -> dict[str, Any]:
    """Render the legacy BetterApplyPilot profile.json shape from unified YAML.

    Args:
        yaml_path: Path to a ``user_profile.yaml`` with the ``ats:`` section.

    Returns:
        Dict matching the structure produced by a direct ``json.loads()`` of
        BetterApplyPilot's legacy ``profile.json``.
    """
    path = Path(yaml_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    identity = data.get("identity") or {}
    ats = data.get("ats") or {}
    extra = ats.get("identity_extra") or {}

    full_name = identity.get("name", "")
    first_name = full_name.split()[0] if full_name else ""

    def pick(ats_key: str, identity_key: str) -> str:
        return extra.get(ats_key) or identity.get(identity_key, "") or ""

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
        "site_logins": ats.get("site_logins") or {},
        "eligibility": ats.get("eligibility") or {},
    }
