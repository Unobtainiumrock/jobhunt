"""ApplyPilot first-time setup wizard.

Interactive flow that creates ~/.applypilot/ with:
  - resume.txt (and optionally resume.pdf)
  - profile.yaml  (unified schema used by both applypilot + linkedin-leads;
                   legacy profile.json is still loaded if the YAML is absent,
                   but every new install produces YAML)
  - searches.yaml
  - .env (LLM API key)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from applypilot.config import (
    APP_DIR,
    ENV_PATH,
    PROFILE_PATH,
    RESUME_PATH,
    RESUME_PDF_PATH,
    SEARCH_CONFIG_PATH,
    ensure_dirs,
)

# Unified profile YAML. The YAML loader in config.load_profile prefers this
# over the legacy JSON when both exist. Writing YAML future-proofs users for
# the ats.site_logins and ats.eligibility blocks that have no JSON analogue.
PROFILE_YAML_PATH = APP_DIR / "profile.yaml"

console = Console()


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

def _setup_resume() -> None:
    """Prompt for resume file and copy into APP_DIR."""
    console.print(Panel("[bold]Step 1: Resume[/bold]\nPoint to your master resume file (.txt or .pdf)."))

    while True:
        path_str = Prompt.ask("Resume file path")
        src = Path(path_str.strip().strip('"').strip("'")).expanduser().resolve()

        if not src.exists():
            console.print(f"[red]File not found:[/red] {src}")
            continue

        suffix = src.suffix.lower()
        if suffix not in (".txt", ".pdf"):
            console.print("[red]Unsupported format.[/red] Provide a .txt or .pdf file.")
            continue

        if suffix == ".txt":
            shutil.copy2(src, RESUME_PATH)
            console.print(f"[green]Copied to {RESUME_PATH}[/green]")
        elif suffix == ".pdf":
            shutil.copy2(src, RESUME_PDF_PATH)
            console.print(f"[green]Copied to {RESUME_PDF_PATH}[/green]")

            # Also ask for a plain-text version for LLM consumption
            txt_path_str = Prompt.ask(
                "Plain-text version of your resume (.txt)",
                default="",
            )
            if txt_path_str.strip():
                txt_src = Path(txt_path_str.strip().strip('"').strip("'")).expanduser().resolve()
                if txt_src.exists():
                    shutil.copy2(txt_src, RESUME_PATH)
                    console.print(f"[green]Copied to {RESUME_PATH}[/green]")
                else:
                    console.print("[yellow]File not found, skipping plain-text copy.[/yellow]")
        break


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def _csv_to_list(raw: str) -> list[str]:
    """Split a user-entered comma string into a cleaned list."""
    return [s.strip() for s in raw.split(",") if s.strip()]


def _setup_profile() -> dict:
    """Walk through profile questions and emit unified user_profile.yaml.

    Produces the same shape ``linkedin-leads/profile/user_profile.yaml`` uses,
    so the single file feeds recruiter-reply drafting (identity block) and
    apply-stage form fill (ats block). Missing sections are either omitted
    entirely or emitted with empty placeholders — the adapter tolerates both.
    """
    console.print(Panel(
        "[bold]Step 2: Profile[/bold]\n"
        "Tell ApplyPilot about yourself. This powers scoring, tailoring, and "
        "auto-fill. Output is ~/.applypilot/profile.yaml — keep it local; "
        "the repo's .gitignore already excludes it."
    ))

    # -- Identity (recruiter-facing + baseline for ATS identity_extra) --
    console.print("\n[bold cyan]Identity (recruiter-facing)[/bold cyan]")
    full_name = Prompt.ask("Full name")
    first_name = full_name.split()[0] if full_name else ""
    identity = {
        "name": full_name,
        "phone": Prompt.ask("Phone number (with country code, e.g. +15105551234)", default=""),
        "email": Prompt.ask("Recruiter-facing email (e.g. Berkeley/work address)"),
        "website": Prompt.ask("Personal website URL (optional)", default=""),
        "linkedin": Prompt.ask("LinkedIn URL"),
        "github": Prompt.ask("GitHub URL (optional)", default=""),
        "location": Prompt.ask("Location (e.g. 'San Francisco, CA 94131')"),
        "remote_preference": Prompt.ask(
            "Remote preference (flexible / remote / hybrid / on-site)",
            default="flexible",
        ),
    }

    # -- ATS block --
    console.print("\n[bold cyan]ATS Form-Fill (applications)[/bold cyan]")
    ats_email = Prompt.ask(
        "Disposable ATS email for Workday/Greenhouse/etc signups "
        "(defaults to identity email)",
        default=identity["email"],
    )
    ats_password = Prompt.ask(
        "Strong disposable password for ATS account creation",
        password=True, default="",
    )
    identity_extra = {
        "preferred_name": Prompt.ask(
            "Preferred name (shown on resume + cover letter sign-off)",
            default=first_name,
        ),
        "email": ats_email,
        "phone": Prompt.ask("Phone with dashes (for US ATS forms, e.g. 510-555-1234)", default=""),
        "address": Prompt.ask("Street address", default=""),
        "city": Prompt.ask("City"),
        "province_state": Prompt.ask("State/Province"),
        "country": Prompt.ask("Country", default="United States"),
        "postal_code": Prompt.ask("Postal/ZIP", default=""),
        "linkedin_url": identity["linkedin"],
        "github_url": identity["github"],
        "portfolio_url": identity["website"],
        "website_url": "",
        "password": ats_password,
    }

    # -- Eligibility (used by geo_fit classifier + apply filter) --
    console.print("\n[bold cyan]Work Eligibility[/bold cyan]")
    authorized_raw = Prompt.ask(
        "Countries you're legally authorized to work in (comma-separated)",
        default="United States",
    )
    acceptable_remote_raw = Prompt.ask(
        "Countries you'd accept remote roles in "
        "(comma-separated; include 'Remote (global)' if any country is OK)",
        default="United States, Canada, Remote (global)",
    )
    eligibility = {
        "countries_authorized_to_work": _csv_to_list(authorized_raw),
        "countries_acceptable_if_remote": _csv_to_list(acceptable_remote_raw),
        "relocation_willing": Confirm.ask("Willing to relocate for the right role?", default=False),
        "sponsorship_willing": Confirm.ask("Open to roles requiring visa sponsorship?", default=False),
    }

    # -- Work Authorization (ATS form fields — can differ from eligibility
    #    policy; e.g. 'Citizen' is an ATS-form dropdown value, independent of
    #    geo_fit policy above) --
    console.print("\n[bold cyan]Work Authorization (ATS form fields)[/bold cyan]")
    work_auth = {
        "legally_authorized_to_work": Confirm.ask(
            "Are you legally authorized to work in your primary target country?",
            default=True,
        ),
        "require_sponsorship": Confirm.ask(
            "Do you now or in the future require sponsorship?",
            default=False,
        ),
        "work_permit_type": Prompt.ask(
            "Work permit type (Citizen / Permanent Resident / Visa / N/A)",
            default="Citizen",
        ),
    }

    # -- Compensation --
    console.print("\n[bold cyan]Compensation[/bold cyan]")
    salary = Prompt.ask("Expected annual salary (number)", default="")
    salary_currency = Prompt.ask("Currency", default="USD")
    salary_range = Prompt.ask("Acceptable range (e.g. 160000-220000)", default="")
    range_parts = salary_range.split("-") if "-" in salary_range else [salary, salary]
    compensation = {
        "salary_expectation": salary,
        "salary_currency": salary_currency,
        "salary_range_min": range_parts[0].strip(),
        "salary_range_max": range_parts[1].strip() if len(range_parts) > 1 else range_parts[0].strip(),
    }

    # -- Experience + skills_boundary --
    console.print("\n[bold cyan]Experience[/bold cyan]")
    current_title = Prompt.ask("Current/most recent job title", default="")
    experience = {
        "years_of_experience_total": Prompt.ask("Years of professional experience", default=""),
        "education_level": Prompt.ask(
            "Highest education (Bachelor's / Master's / PhD / Self-taught)",
            default="Bachelor's",
        ),
        "current_title": current_title,
        "target_role": Prompt.ask(
            "Target role (e.g. 'Machine Learning Engineer')",
            default=current_title,
        ),
    }

    console.print("\n[bold cyan]Skills Allow-List[/bold cyan] (comma-separated)")
    console.print("[dim]Tailor/judge stages will refuse to mention any tool not listed here.[/dim]")
    skills_boundary = {
        "programming_languages": _csv_to_list(Prompt.ask("Programming languages", default="")),
        "frameworks": _csv_to_list(Prompt.ask("Frameworks & libraries", default="")),
        "tools": _csv_to_list(Prompt.ask("Tools & platforms (e.g. Docker, AWS, Git)", default="")),
    }

    # -- Resume Facts --
    console.print("\n[bold cyan]Resume Facts (preserved truths)[/bold cyan]")
    console.print("[dim]Preserved exactly during tailoring — the AI will never alter them.[/dim]")
    resume_facts = {
        "preserved_companies": _csv_to_list(Prompt.ask("Companies to always keep", default="")),
        "preserved_projects": _csv_to_list(Prompt.ask("Projects to always keep", default="")),
        "preserved_school": _csv_to_list(Prompt.ask("School(s) to preserve", default="")),
        "real_metrics": _csv_to_list(Prompt.ask(
            "Real metrics to preserve (e.g. '99.9% uptime, 50k users')",
            default="",
        )),
    }

    # -- EEO defaults --
    eeo_voluntary = {
        "gender": "Decline to self-identify",
        "race_ethnicity": "Decline to self-identify",
        "veteran_status": "Decline to self-identify",
        "disability_status": "Decline to self-identify",
    }

    # -- Availability --
    availability = {
        "earliest_start_date": Prompt.ask("Earliest start date", default="Immediately"),
    }

    # -- Optional site_logins (for sites whose account predates the ATS
    #    signup — most commonly LinkedIn if user has an existing account
    #    under a non-ATS email) --
    ats_block: dict = {
        "identity_extra": identity_extra,
        "work_authorization": work_auth,
        "compensation": compensation,
        "experience": experience,
        "skills_boundary": skills_boundary,
        "resume_facts": resume_facts,
        "eeo_voluntary": eeo_voluntary,
        "availability": availability,
        "eligibility": eligibility,
    }
    if Confirm.ask(
        "\nDo you have a LinkedIn account under a non-ATS email? "
        "(adds site-specific credentials so apply uses the right login)",
        default=False,
    ):
        li_email = Prompt.ask("  LinkedIn login email")
        li_pw = Prompt.ask("  LinkedIn password", password=True)
        li_alt = Prompt.ask(
            "  Alt password (optional — agent tries this if primary fails)",
            password=True, default="",
        )
        ats_block["site_logins"] = {
            "linkedin": {
                "email": li_email,
                "password": li_pw,
                "alt_password": li_alt,
            }
        }

    profile_doc = {
        "identity": identity,
        "ats": ats_block,
    }

    PROFILE_YAML_PATH.write_text(
        yaml.safe_dump(profile_doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    console.print(f"\n[green]Profile saved to {PROFILE_YAML_PATH}[/green]")

    # Validate by round-tripping through the adapter — catches shape bugs now
    # rather than at first apply.
    try:
        from jobhunt_core.profile import load_profile_from_yaml
        loaded = load_profile_from_yaml(PROFILE_YAML_PATH)
        missing = [k for k in (
            "personal", "compensation", "work_authorization",
            "experience", "skills_boundary", "eligibility",
        ) if not loaded.get(k)]
        if missing:
            console.print(
                f"[yellow]Warning: adapter output missing keys {missing}. "
                f"Apply stage may error — re-run init or edit YAML manually.[/yellow]"
            )
    except Exception as exc:
        console.print(f"[red]Profile validation failed: {exc}[/red]")
    return profile_doc


# ---------------------------------------------------------------------------
# Search config
# ---------------------------------------------------------------------------

def _setup_searches() -> None:
    """Generate a searches.yaml from user input."""
    console.print(Panel("[bold]Step 3: Job Search Config[/bold]\nDefine what you're looking for."))

    location = Prompt.ask("Target location (e.g. 'Remote', 'Canada', 'New York, NY')", default="Remote")
    distance_str = Prompt.ask("Search radius in miles (0 for remote-only)", default="0")
    try:
        distance = int(distance_str)
    except ValueError:
        distance = 0

    roles_raw = Prompt.ask(
        "Target job titles (comma-separated, e.g. 'Backend Engineer, Full Stack Developer')"
    )
    roles = [r.strip() for r in roles_raw.split(",") if r.strip()]

    if not roles:
        console.print("[yellow]No roles provided. Using a default set.[/yellow]")
        roles = ["Software Engineer"]

    # Build YAML content
    lines = [
        "# ApplyPilot search configuration",
        "# Edit this file to refine your job search queries.",
        "",
        "defaults:",
        f'  location: "{location}"',
        f"  distance: {distance}",
        "  hours_old: 72",
        "  results_per_site: 50",
        "",
        "locations:",
        f'  - location: "{location}"',
        f"    remote: {str(distance == 0).lower()}",
        "",
        "queries:",
    ]
    for i, role in enumerate(roles):
        lines.append(f'  - query: "{role}"')
        lines.append(f"    tier: {min(i + 1, 3)}")

    SEARCH_CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green]Search config saved to {SEARCH_CONFIG_PATH}[/green]")


# ---------------------------------------------------------------------------
# AI Features
# ---------------------------------------------------------------------------

def _setup_ai_features() -> None:
    """Ask about AI scoring/tailoring — optional LLM configuration."""
    console.print(Panel(
        "[bold]Step 4: AI Features (optional)[/bold]\n"
        "An LLM powers job scoring, resume tailoring, and cover letters.\n"
        "Without this, you can still discover and enrich jobs."
    ))

    if not Confirm.ask("Enable AI scoring and resume tailoring?", default=True):
        console.print("[dim]Discovery-only mode. You can configure AI later with [bold]applypilot init[/bold].[/dim]")
        return

    console.print("Supported providers: [bold]Gemini[/bold] (recommended, free tier), OpenAI, local (Ollama/llama.cpp)")
    provider = Prompt.ask(
        "Provider",
        choices=["gemini", "openai", "local"],
        default="gemini",
    )

    env_lines = ["# ApplyPilot configuration", ""]

    if provider == "gemini":
        api_key = Prompt.ask("Gemini API key (from aistudio.google.com)")
        model = Prompt.ask("Model", default="gemini-2.0-flash")
        env_lines.append(f"GEMINI_API_KEY={api_key}")
        env_lines.append(f"LLM_MODEL={model}")
    elif provider == "openai":
        api_key = Prompt.ask("OpenAI API key")
        model = Prompt.ask("Model", default="gpt-4o-mini")
        env_lines.append(f"OPENAI_API_KEY={api_key}")
        env_lines.append(f"LLM_MODEL={model}")
    elif provider == "local":
        url = Prompt.ask("Local LLM endpoint URL", default="http://localhost:8080/v1")
        model = Prompt.ask("Model name", default="local-model")
        env_lines.append(f"LLM_URL={url}")
        env_lines.append(f"LLM_MODEL={model}")

    env_lines.append("")
    ENV_PATH.write_text("\n".join(env_lines), encoding="utf-8")
    console.print(f"[green]AI configuration saved to {ENV_PATH}[/green]")


# ---------------------------------------------------------------------------
# Auto-Apply
# ---------------------------------------------------------------------------

def _setup_auto_apply() -> None:
    """Configure autonomous job application (requires Claude Code CLI)."""
    console.print(Panel(
        "[bold]Step 5: Auto-Apply (optional)[/bold]\n"
        "ApplyPilot can autonomously fill and submit job applications\n"
        "using Claude Code as the browser agent."
    ))

    if not Confirm.ask("Enable autonomous job applications?", default=True):
        console.print("[dim]You can apply manually using the tailored resumes ApplyPilot generates.[/dim]")
        return

    # Check for Claude Code CLI
    if shutil.which("claude"):
        console.print("[green]Claude Code CLI detected.[/green]")
    else:
        console.print(
            "[yellow]Claude Code CLI not found on PATH.[/yellow]\n"
            "Install it from: [bold]https://claude.ai/code[/bold]\n"
            "Auto-apply won't work until Claude Code is installed."
        )

    # Optional: CapSolver for CAPTCHAs
    console.print("\n[dim]Some job sites use CAPTCHAs. CapSolver can handle them automatically.[/dim]")
    if Confirm.ask("Configure CapSolver API key? (optional)", default=False):
        capsolver_key = Prompt.ask("CapSolver API key")
        # Append to existing .env or create
        if ENV_PATH.exists():
            existing = ENV_PATH.read_text(encoding="utf-8")
            if "CAPSOLVER_API_KEY" not in existing:
                ENV_PATH.write_text(
                    existing.rstrip() + f"\nCAPSOLVER_API_KEY={capsolver_key}\n",
                    encoding="utf-8",
                )
        else:
            ENV_PATH.write_text(f"# ApplyPilot configuration\nCAPSOLVER_API_KEY={capsolver_key}\n", encoding="utf-8")
        console.print("[green]CapSolver key saved.[/green]")
    else:
        console.print("[dim]Skipped. Add CAPSOLVER_API_KEY to .env later if needed.[/dim]")


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def run_wizard() -> None:
    """Run the full interactive setup wizard."""
    console.print()
    console.print(
        Panel.fit(
            "[bold green]ApplyPilot Setup Wizard[/bold green]\n\n"
            "This will create your configuration at:\n"
            f"  [cyan]{APP_DIR}[/cyan]\n\n"
            "You can re-run this anytime with [bold]applypilot init[/bold].",
            border_style="green",
        )
    )

    ensure_dirs()
    console.print(f"[dim]Created {APP_DIR}[/dim]\n")

    # Step 1: Resume
    _setup_resume()
    console.print()

    # Step 2: Profile
    _setup_profile()
    console.print()

    # Step 3: Search config
    _setup_searches()
    console.print()

    # Step 4: AI features (optional LLM)
    _setup_ai_features()
    console.print()

    # Step 5: Auto-apply (Claude Code detection)
    _setup_auto_apply()
    console.print()

    # Done — show tier status
    from applypilot.config import get_tier, TIER_LABELS, TIER_COMMANDS

    tier = get_tier()

    tier_lines: list[str] = []
    for t in range(1, 4):
        label = TIER_LABELS[t]
        cmds = ", ".join(f"[bold]{c}[/bold]" for c in TIER_COMMANDS[t])
        if t <= tier:
            tier_lines.append(f"  [green]✓ Tier {t} — {label}[/green]  ({cmds})")
        elif t == tier + 1:
            tier_lines.append(f"  [yellow]→ Tier {t} — {label}[/yellow]  ({cmds})")
        else:
            tier_lines.append(f"  [dim]✗ Tier {t} — {label}  ({cmds})[/dim]")

    unlock_hint = ""
    if tier == 1:
        unlock_hint = "\n[dim]To unlock Tier 2: configure an LLM API key (re-run [bold]applypilot init[/bold]).[/dim]"
    elif tier == 2:
        unlock_hint = "\n[dim]To unlock Tier 3: install Claude Code CLI + Chrome.[/dim]"

    console.print(
        Panel.fit(
            "[bold green]Setup complete![/bold green]\n\n"
            f"[bold]Your tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]\n\n"
            + "\n".join(tier_lines)
            + unlock_hint,
            border_style="green",
        )
    )
