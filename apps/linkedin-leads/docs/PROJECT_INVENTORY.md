# Job-Hunt Project Inventory

This inventory records which existing Desktop and GitHub assets should feed the unified hunt system.

Current rule: `linkedin-leads` is the runtime system of record. Legacy folders may remain as source material or historical reference, but the recruiter/prep workflow should not depend on them at runtime.

## Core System

- `~/Desktop/github/linkedin-leads`
  - Role: operational core
  - Why: already has ingestion, classification, scoring, replies, follow-ups, search, profile handling, and daily briefings
  - Status: keep active

## Knowledge and Prep Sources

- `~/Desktop/systems-design-practice`
  - Role: topic knowledge source
  - Destination: `prep/topics/`
  - Current state: the single `to-study.md` file was migrated into `prep/topics/systems-design-foundations.md`
  - Recommendation: safe to archive or delete once you are comfortable treating the migrated copy as canonical

- `~/Desktop/vy-prep`
  - Role: company dossier source
  - Destination: `prep/companies/`
  - Current state: `vye_interview_final.md` was migrated into `prep/companies/vye-health.md`
  - Recommendation: safe to archive or delete; remaining `.claude` and `.mcp` files are local tooling residue, not source data

- interview flashcard assets
  - Role: structured drill material
  - Destination: `prep/flashcards/`
  - Current state: canonical runtime now reads `data/knowledge/interview_flashcards.json` inside `linkedin-leads`
  - Recommendation: the old `bayesian-flashcards` repo is no longer a runtime dependency; keep it only if you still want the standalone flashcard application

- `~/Desktop/job-hunt`
  - Role: personal administrative notes
  - Destination: selective migration into `prep/` or future private ops storage
  - Current state: `unemployment-notes.md` has not been fully migrated into the unified hunt system
  - Recommendation: keep or privately archive until those notes are intentionally moved somewhere else

## Reference-Only Projects

- `~/Desktop/github/career-deer-product`
  - Role: product vision and UX reference
  - Decision: do not merge code directly
  - Current state: useful as historical product/design reference, not operational dependency
  - Recommendation: archive if you still want the reference, otherwise delete intentionally rather than assuming it was absorbed

- `~/Desktop/task-system-interview`
  - Role: prioritization and dependency-logic reference
  - Decision: reuse concepts only
  - Current state: small reference repo, not part of the runtime path
  - Recommendation: archive or snapshot any ideas you still care about, then delete if it no longer has value

## Migration Principle

Every source should be classified as one of:

- core implementation
- structured knowledge input
- reference-only
- archive-only

This prevents the repo from becoming a dumping ground for unrelated code and notes.

## Current Archive/Delete View

- Safe to archive/delete now:
  - `~/Desktop/systems-design-practice`
  - `~/Desktop/vy-prep`
- Safe to delete from runtime perspective, but keep if you still want the old standalone app/reference:
  - `~/Desktop/github/bayesian-flashcards`
  - `~/Desktop/github/career-deer-product`
  - `~/Desktop/task-system-interview`
- Keep for now because not fully migrated:
  - `~/Desktop/job-hunt`
