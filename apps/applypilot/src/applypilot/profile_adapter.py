"""Thin re-export: applypilot continues to import from here for backward
compatibility; the actual adapter logic now lives in ``jobhunt_core.profile``
so ``linkedin-leads`` can consume the same loader (Phase 4 of the unification
plan).

Kept as a module (rather than deleted) so pre-existing imports of
``applypilot.profile_adapter.load_profile_from_yaml`` keep working.
"""

from jobhunt_core.profile import load_profile_from_yaml

__all__ = ["load_profile_from_yaml"]
