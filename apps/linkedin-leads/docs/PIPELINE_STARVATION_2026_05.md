# BAP pipeline diagnosis: why `scored_eligible` and `in_progress` are empty (2026-05-12)

Context: the Applications tab on `review.178-104-92-205.sslip.io` showed
zero rows under both the `scored_eligible` and `in_progress` filter chips.
This doc records what's actually in the BAP DB and why those filters
are empty.

## Snapshot

Queried `/opt/jobhunt/data/jobhunt.db` (read-only) on Hetzner.

```
total rows: 103

apply_status distribution
  (null)      87
  failed      10
  applied      6
  in_progress  0     <-- exhausted

fit_score buckets
  >= 7 (eligible)   68
  5.0 - 6.99        19
  3.0 - 4.99        13
  < 3                3

scored in last 7d: 0
applied in last 7d: 0
ever set apply_status: 16
```

## Finding 1: `scored_eligible` is empty but 68 rows score ≥ 7

The Applications filter is a *stage* filter, not an attribute filter.
`_derive_status` in `pipeline/review_server.py:1390` collapses each row
into the single stage it currently occupies, in priority order:

```
applied_at set                → applied
apply_status = in_progress    → in_progress
apply_status in (failed,…)    → that error status
tailored_resume_path + URL    → ready
tailored_resume_path          → tailored
fit_score IS NOT NULL         → scored_eligible | scored_below
full_description              → enriched
else                           → discovered
```

So a row that scored 8.2 *and* got tailored is labeled `tailored` or
`ready`, not `scored_eligible`. The `scored_eligible` bucket only
contains rows whose latest stage is scoring (scored but not yet
tailored). All 68 eligible rows have advanced past scoring, which is
why the chip reads 0.

**This is working as designed.** The legend wording in
`FILTER_LEGEND` was misleading — it implied a permanent attribute. Fixed
in the same commit as this doc.

If you want to see "everything that ever scored eligible regardless of
current stage," use the `tailored+` chip, or add a new attribute filter.

## Finding 2: `in_progress` is empty because nothing is mid-apply

Only 16 of 103 rows have ever had `apply_status` set (10 failed +
6 applied). Zero rows have `apply_status = 'in_progress'`. Zero
applies in the last 7 days.

This is data starvation: the apply automation hasn't run in the past
week. When it does run, rows transition through `in_progress` to
`applied` or `failed` quickly enough that catching them in the
`in_progress` bucket requires hitting the page during an active run.

Possible causes (not investigated in this pass — out of scope for 3c):
- the `applypilot` apply pipeline is not on the Hetzner cron
- it is on cron but recent runs hit blockers (login wall, captcha,
  no rows met the eligibility AND URL precondition)
- it ran but every row failed before reaching `in_progress`
  (instrument the writer side if this is suspected)

## Follow-up actions

- [done] Sharpen `FILTER_LEGEND` text to call out the stage semantics.
- [open] Diagnose the apply pipeline cadence — is `applypilot apply`
  on the Hetzner cron? If not, add it. If yes, why no runs in 7d?
  (Tracked separately — out of scope for this pass.)
