"""
Shared configuration for the linkedin-leads pipeline.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOTENV_FILE = PROJECT_ROOT / ".env"


def _load_dotenv() -> None:
    if not DOTENV_FILE.exists():
        return
    for raw_line in DOTENV_FILE.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()

DATA_DIR = PROJECT_ROOT / "data"
PROFILE_DIR = PROJECT_ROOT / "profile"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
DOCS_DIR = PROJECT_ROOT / "docs"
SCHEMAS_DIR = PROJECT_ROOT / "schemas"
PREP_DIR = PROJECT_ROOT / "prep"
ENTITY_DATA_DIR = DATA_DIR / "entities"
KNOWLEDGE_DATA_DIR = DATA_DIR / "knowledge"
LEADS_DIR = ENTITY_DATA_DIR / "leads"
OPPORTUNITIES_DIR = ENTITY_DATA_DIR / "opportunities"
CONVERSATIONS_DIR = ENTITY_DATA_DIR / "conversations"
APPLICATIONS_DIR = ENTITY_DATA_DIR / "applications"
INTERVIEW_LOOPS_DIR = ENTITY_DATA_DIR / "interview_loops"
TASKS_DIR = ENTITY_DATA_DIR / "tasks"
SIGNALS_DIR = ENTITY_DATA_DIR / "signals"
PREP_ARTIFACTS_DIR = ENTITY_DATA_DIR / "prep_artifacts"
ENTITY_MANIFEST_FILE = ENTITY_DATA_DIR / "manifest.json"
ENTITY_OVERRIDES_FILE = ENTITY_DATA_DIR / "overrides.json"
FOLLOWUP_QUEUE_FILE = ENTITY_DATA_DIR / "followups.json"
WORKFLOW_STATE_FILE = ENTITY_DATA_DIR / "workflow_state.json"
ENRICHMENT_QUEUE_FILE = ENTITY_DATA_DIR / "research_enrichment_queue.json"
ENRICHMENT_ARTIFACTS_DIR = KNOWLEDGE_DATA_DIR / "company_research"

INBOX_FILE = DATA_DIR / "inbox.json"
CLASSIFIED_FILE = DATA_DIR / "inbox_classified.json"
CONTACTS_CSV = DATA_DIR / "contacts.csv"
LEAD_STATE_FILE = DATA_DIR / "lead_states.json"

PROFILE_FILE = PROFILE_DIR / "user_profile.yaml"

# OpenAI models -- override via env vars for cost control
CLASSIFY_MODEL: str = os.getenv("CLASSIFY_MODEL", "gpt-4o-mini")
GENERATION_MODEL: str = os.getenv("GENERATION_MODEL", "gpt-4o-mini")
REASONING_MODEL: str = os.getenv("REASONING_MODEL", "o3")
FAST_MODEL: str = os.getenv("FAST_MODEL", "gpt-4o-mini")

# Optional Gemini Deep Research enrichment
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY")
GEMINI_API_BASE: str = os.getenv("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta")
GEMINI_DEEP_RESEARCH_AGENT: str = os.getenv(
    "GEMINI_DEEP_RESEARCH_AGENT",
    "deep-research-pro-preview-12-2025",
)

# Qdrant
QDRANT_URL: str = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY: str | None = os.getenv("QDRANT_API_KEY")
CONVERSATIONS_COLLECTION: str = "linkedin_conversations"
PROFILE_COLLECTION: str = "user_profile"
EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
VECTOR_DIM: int = 384
EMBED_BATCH_SIZE: int = 256

# Scoring thresholds
SCORE_AUTO_REPLY: int = 80
SCORE_REVIEW: int = 50

# Follow-up timing (business days)
FOLLOWUP_1_DAYS: int = 4
FOLLOWUP_2_DAYS: int = 9

MAX_CONCURRENT: int = 8

# User identity (used in safety validation and reply templates)
USER_NAME: str = "Nicholas J. Fleischhauer"
USER_PHONE: str = "510-906-5492"
USER_WEBSITE: str = "https://fleischhauer.dev/"
