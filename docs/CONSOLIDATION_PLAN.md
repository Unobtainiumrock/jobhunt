# Consolidation Plan

This document turns the unified architecture into an execution sequence.

Status note: phases 1 through 4 now exist in working form inside `linkedin-leads`. The remaining work is refinement, broader data coverage, and selective archival of old source folders.

## Phase 1: Canonical Model

- Establish canonical schemas for `Lead`, `Opportunity`, `Conversation`, `Application`, `InterviewLoop`, `PrepArtifact`, `Task`, and `Signal`
- Reserve storage locations under `data/entities/` and `data/knowledge/`
- Keep all initial changes additive so current recruiter pipeline commands continue to work
- Current state: complete

## Phase 2: Knowledge Migration

- Migrate `systems-design-practice/to-study.md` into structured topic artifacts
- Migrate `vy-prep` style company notes into company prep dossiers
- Migrate interview flashcards into `prep/flashcards/`
- Normalize interview debriefs into reusable prep artifacts instead of leaving them as isolated notes
- Current state: materially complete for the current prep corpus; additional notes can still be migrated later

## Phase 3: Hunt System Expansion

- Introduce first-class `Opportunity` and `Application` records that are not tied only to LinkedIn scraping
- Allow manual entry for jobs found outside LinkedIn
- Attach recruiter threads, applications, and interview loops to the same opportunity record
- Current state: canonical entities and workflow state exist; manual opportunity entry remains a future extension

## Phase 4: Workflow Orchestration

- Generate `Task` records from recruiter messages, follow-up deadlines, interview signals, and prep gaps
- Expand the morning briefing to include:
  - top leads
  - active applications
  - upcoming interviews
  - due prep tasks
  - company-specific notes
  - system design study blocks
- Current state: complete in first working form, including canonical briefing, review UI, and optional external research enrichment

## Phase 5: Product Surface

- Reintroduce the strongest `Career Deer` ideas as a modern thin interface on top of the unified data model
- Prefer a board-style workflow UI after the entity model is stable
- Avoid importing the old monolith directly
- Current state: not started; `career-deer-product` remains reference material only

## Migration Rules

- Preserve current pipeline behavior while the new model is added
- Normalize raw notes before building more automation on top of them
- Prefer one source of truth per concept
- Keep personal support artifacts private unless intentionally productized

## Legacy Source Status

- `linkedin-leads` is now the runtime source of truth
- old source folders are no longer required at runtime
- remaining work on legacy folders is content curation and archival, not implementation dependency removal
