"""End-to-end sanity check for retention.purge_expired.

Spins up an isolated APPLYPILOT_DIR, seeds:
  - an "old" unapplied tailored resume (tailored 200d ago — should purge)
  - a "young" unapplied tailored resume (now — keep)
  - an unapplied 179d boundary case (keep; unapplied cutoff is 180d)
  - an "old" applied resume (195d; UNDER applied cutoff of 210d — keep)
  - an "ancient" applied resume (215d; OVER applied cutoff — purge)
  - an "old" cover letter (unapplied)
  - an orphan file (untracked, 300d — purge)

Runs purge_expired first in dry-run (nothing changes), then for real.
Asserts differentiated retention: unapplied uses 180d, applied uses 210d.

Run:
    python scripts/retention_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


def _touch_backdate(path: Path, days_old: int) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_old)).timestamp()
    os.utime(path, (ts, ts))


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="applypilot_retention_") as tmp:
        os.environ["APPLYPILOT_DIR"] = tmp

        # Import AFTER setting APPLYPILOT_DIR so config picks it up
        from applypilot import config
        # Force re-evaluation of paths since config module was cached
        import importlib
        importlib.reload(config)

        from applypilot.database import init_db, get_connection
        from applypilot.retention import purge_expired

        config.ensure_dirs()
        init_db()
        conn = get_connection()

        tailored = config.TAILORED_DIR
        covers = config.COVER_LETTER_DIR

        # ── Seed: old tailored resume (200 days old) ──
        old_prefix = tailored / "Acme_OldRole"
        old_txt = old_prefix.with_suffix(".txt")
        old_pdf = old_prefix.with_suffix(".pdf")
        old_job = Path(f"{old_prefix}_JOB.txt")
        old_report = Path(f"{old_prefix}_REPORT.json")
        for f in (old_txt, old_pdf, old_job, old_report):
            f.write_text("old")
            _touch_backdate(f, days_old=200)

        # ── Seed: young tailored resume (now) ──
        new_prefix = tailored / "Acme_NewRole"
        new_txt = new_prefix.with_suffix(".txt")
        new_pdf = new_prefix.with_suffix(".pdf")
        for f in (new_txt, new_pdf):
            f.write_text("new")

        # ── Seed: boundary case (179 days old — should survive) ──
        boundary_prefix = tailored / "Acme_BoundaryRole"
        boundary_txt = boundary_prefix.with_suffix(".txt")
        boundary_pdf = boundary_prefix.with_suffix(".pdf")
        for f in (boundary_txt, boundary_pdf):
            f.write_text("boundary")
            _touch_backdate(f, days_old=179)

        # ── Seed: old cover letter (200 days old) ──
        old_cover_txt = covers / "Acme_OldRole_CL.txt"
        old_cover_pdf = covers / "Acme_OldRole_CL.pdf"
        for f in (old_cover_txt, old_cover_pdf):
            f.write_text("old cover")
            _touch_backdate(f, days_old=200)

        # ── Seed: APPLIED, 195 days old (should survive — under 210d) ──
        applied_survives_prefix = tailored / "Acme_AppliedSurvives"
        applied_survives_txt = applied_survives_prefix.with_suffix(".txt")
        applied_survives_pdf = applied_survives_prefix.with_suffix(".pdf")
        for f in (applied_survives_txt, applied_survives_pdf):
            f.write_text("applied 195d")
            _touch_backdate(f, days_old=195)

        # ── Seed: APPLIED, 215 days old (should purge — over 210d) ──
        applied_expired_prefix = tailored / "Acme_AppliedExpired"
        applied_expired_txt = applied_expired_prefix.with_suffix(".txt")
        applied_expired_pdf = applied_expired_prefix.with_suffix(".pdf")
        for f in (applied_expired_txt, applied_expired_pdf):
            f.write_text("applied 215d")
            _touch_backdate(f, days_old=215)

        # ── Seed: orphan file (no DB row, 300 days old) ──
        orphan = tailored / "Orphan_Stale.txt"
        orphan.write_text("orphan")
        _touch_backdate(orphan, days_old=300)

        # ── Seed: old + fresh logs ──
        old_log = config.LOG_DIR / "claude_old_w0.txt"
        old_log.write_text("old log with PII")
        _touch_backdate(old_log, days_old=250)

        boundary_log = config.LOG_DIR / "claude_boundary_w0.txt"
        boundary_log.write_text("209d log")
        _touch_backdate(boundary_log, days_old=209)

        fresh_log = config.LOG_DIR / "claude_fresh_w0.txt"
        fresh_log.write_text("fresh log")

        # ── Seed: old + fresh worker artifacts (nested under worker-N) ──
        worker_dir = config.APPLY_WORKER_DIR / "current"
        worker_dir.mkdir(parents=True, exist_ok=True)
        old_worker_pdf = worker_dir / "Jane_Doe_Resume_old.pdf"
        old_worker_pdf.write_text("old resume upload")
        _touch_backdate(old_worker_pdf, days_old=250)

        fresh_worker_pdf = worker_dir / "Jane_Doe_Resume.pdf"
        fresh_worker_pdf.write_text("fresh resume upload")

        # ── Seed: DB rows ──
        conn.executemany(
            "INSERT INTO jobs (url, title, site, tailored_resume_path, tailored_at, "
            "cover_letter_path, cover_letter_at, applied_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("https://acme.example/old", "Old Role", "acme",
                 str(old_txt), _iso_days_ago(200),
                 str(old_cover_txt), _iso_days_ago(200), None),
                ("https://acme.example/new", "New Role", "acme",
                 str(new_txt), _iso_days_ago(0),
                 None, None, None),
                ("https://acme.example/boundary", "Boundary Role", "acme",
                 str(boundary_txt), _iso_days_ago(179),
                 None, None, None),
                ("https://acme.example/applied-survives", "Applied Recent", "acme",
                 str(applied_survives_txt), _iso_days_ago(195),
                 None, None, _iso_days_ago(190)),
                ("https://acme.example/applied-expired", "Applied Ancient", "acme",
                 str(applied_expired_txt), _iso_days_ago(215),
                 None, None, _iso_days_ago(210)),
            ],
        )
        conn.commit()

        print(f"Seeded {tmp}")
        print(f"  tailored/: {sorted(f.name for f in tailored.iterdir())}")
        print(f"  cover_letters/: {sorted(f.name for f in covers.iterdir())}")
        print(f"  logs/: {sorted(f.name for f in config.LOG_DIR.iterdir())}")
        print(f"  apply-workers/current/: {sorted(f.name for f in worker_dir.iterdir())}")

        # ── Dry-run: nothing should change ──
        dry = purge_expired(dry_run=True)
        print(f"\nDry-run result: {dry}")
        assert dry["dry_run"] is True
        # Unapplied 200d + applied 215d = 2 resume prunes
        assert dry["db_pruned_resumes"] == 2, dry
        assert dry["db_pruned_covers"] == 1, dry
        assert dry["orphans_pruned"] == 1, dry
        assert dry["logs_pruned"] == 1, dry  # 250d > 210d; 209d stays
        assert dry["worker_artifacts_pruned"] == 1, dry
        assert dry["retention_days"] == 180, dry
        assert dry["retention_days_applied"] == 210, dry
        # Nothing should actually be gone
        for f in (old_txt, old_pdf, old_job, old_report,
                  new_txt, new_pdf, boundary_txt, boundary_pdf,
                  old_cover_txt, old_cover_pdf, orphan,
                  applied_survives_txt, applied_survives_pdf,
                  applied_expired_txt, applied_expired_pdf,
                  old_log, boundary_log, fresh_log,
                  old_worker_pdf, fresh_worker_pdf):
            assert f.exists(), f"dry-run removed {f}"
        # DB still has the paths
        row = conn.execute(
            "SELECT tailored_resume_path FROM jobs WHERE url=?",
            ("https://acme.example/old",),
        ).fetchone()
        assert row["tailored_resume_path"] == str(old_txt), "dry-run mutated DB"
        print("[OK] dry-run: counts correct, nothing deleted, DB unchanged")

        # ── Real run ──
        real = purge_expired(dry_run=False)
        print(f"\nReal run result: {real}")
        assert real["dry_run"] is False
        assert real["db_pruned_resumes"] == 2, real
        assert real["db_pruned_covers"] == 1, real
        assert real["orphans_pruned"] == 1, real
        assert real["logs_pruned"] == 1, real
        assert real["worker_artifacts_pruned"] == 1, real

        # Old unapplied artifacts gone
        for f in (old_txt, old_pdf, old_job, old_report):
            assert not f.exists(), f"old unapplied resume sibling still exists: {f}"
        for f in (old_cover_txt, old_cover_pdf):
            assert not f.exists(), f"old cover sibling still exists: {f}"
        assert not orphan.exists(), "orphan not deleted"
        # Applied-and-expired (215d > 210d) artifacts gone
        for f in (applied_expired_txt, applied_expired_pdf):
            assert not f.exists(), f"expired applied sibling still exists: {f}"
        print("[OK] expired files deleted (unapplied>180d + applied>210d)")

        # Young artifacts preserved
        for f in (new_txt, new_pdf, boundary_txt, boundary_pdf):
            assert f.exists(), f"fresh file was wrongly deleted: {f}"
        # Applied-but-still-in-window (195d < 210d) preserved
        for f in (applied_survives_txt, applied_survives_pdf):
            assert f.exists(), f"applied 195d file wrongly deleted (should survive to 210d): {f}"
        print("[OK] fresh + 179d boundary + 195d-applied (within 210d) preserved")

        # DB row nulled
        row = conn.execute(
            "SELECT tailored_resume_path, tailored_at, cover_letter_path, cover_letter_at "
            "FROM jobs WHERE url=?",
            ("https://acme.example/old",),
        ).fetchone()
        assert row["tailored_resume_path"] is None, row
        assert row["tailored_at"] is None, row
        assert row["cover_letter_path"] is None, row
        assert row["cover_letter_at"] is None, row
        print("[OK] DB path and timestamp columns NULL'd for expired row")

        # Fresh DB rows untouched
        row = conn.execute(
            "SELECT tailored_resume_path FROM jobs WHERE url=?",
            ("https://acme.example/new",),
        ).fetchone()
        assert row["tailored_resume_path"] == str(new_txt), "fresh row was wrongly nulled"
        print("[OK] fresh DB rows untouched")

        # Applied 195d row STILL has its path (not yet expired under 210d)
        row = conn.execute(
            "SELECT tailored_resume_path, applied_at FROM jobs WHERE url=?",
            ("https://acme.example/applied-survives",),
        ).fetchone()
        assert row["tailored_resume_path"] == str(applied_survives_txt), \
            f"applied 195d row wrongly nulled: {dict(row)}"
        assert row["applied_at"] is not None
        print("[OK] applied 195d row preserved (DB path + applied_at intact)")

        # Applied 215d row WAS nulled (exceeded 210d)
        row = conn.execute(
            "SELECT tailored_resume_path, applied_at FROM jobs WHERE url=?",
            ("https://acme.example/applied-expired",),
        ).fetchone()
        assert row["tailored_resume_path"] is None, \
            f"applied 215d row should be nulled: {dict(row)}"
        # applied_at itself is not cleared — it stays as history
        assert row["applied_at"] is not None, "applied_at should NOT be nulled — it is history"
        print("[OK] applied 215d row nulled, applied_at history retained")

        # Log + worker sweep
        assert not old_log.exists(), "old log still exists"
        assert boundary_log.exists(), "209d boundary log wrongly deleted"
        assert fresh_log.exists(), "fresh log wrongly deleted"
        assert not old_worker_pdf.exists(), "old worker PDF still exists"
        assert fresh_worker_pdf.exists(), "fresh worker PDF wrongly deleted"
        print("[OK] old logs + worker artifacts pruned, fresh + 209d boundary kept")

        # Idempotence — second run should be a no-op
        second = purge_expired(dry_run=False)
        assert second["db_pruned_resumes"] == 0
        assert second["db_pruned_covers"] == 0
        assert second["orphans_pruned"] == 0
        assert second["logs_pruned"] == 0
        assert second["worker_artifacts_pruned"] == 0
        print("[OK] second run is a no-op")

        print("\nAll retention checks passed.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
