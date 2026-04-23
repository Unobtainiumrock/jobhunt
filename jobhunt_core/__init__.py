"""jobhunt_core: shared library for BetterApplyPilot + linkedin-leads."""

from jobhunt_core.profile import load_profile_from_yaml
from jobhunt_core.entities import Opportunity, OpportunityStatus, OpportunitySource

__version__ = "0.1.0"

__all__ = [
    "load_profile_from_yaml",
    "Opportunity",
    "OpportunityStatus",
    "OpportunitySource",
    "__version__",
]
