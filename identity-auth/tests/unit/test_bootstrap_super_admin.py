"""Smoke tests for scripts/bootstrap_super_admin.py — this script is a
human-run, real-AWS/real-DB operation (see its own docstring), so these
tests only verify argument parsing and the dry-run path, which must
touch neither AWS nor a database."""

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import bootstrap_super_admin  # noqa: E402


def test_dry_run_makes_no_changes_and_returns_zero(capsys):
    exit_code = bootstrap_super_admin.main(
        [
            "--email",
            "superadmin@milkful.test",
            "--name",
            "First Super Admin",
            "--admin-pool-id",
            "ap-south-1_fake",
            "--database-url",
            "postgresql+psycopg2://user:pass@host:5432/admin",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out
    assert "No changes made" in captured.out


def test_dry_run_with_set_password_makes_no_changes(capsys):
    # --set-password only changes behavior on the --execute path (see
    # module docstring) — a dry run must still touch nothing.
    exit_code = bootstrap_super_admin.main(
        [
            "--email",
            "superadmin@milkful.test",
            "--name",
            "First Super Admin",
            "--admin-pool-id",
            "ap-south-1_fake",
            "--database-url",
            "postgresql+psycopg2://user:pass@host:5432/admin",
            "--set-password",
            "Sup3rS3cret!",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out
    assert "No changes made" in captured.out


def test_missing_required_argument_exits_nonzero():
    with pytest.raises(SystemExit):
        bootstrap_super_admin.main(["--email", "x@milkful.test"])


def test_dry_run_does_not_require_boto3_credentials_or_db(monkeypatch, capsys):
    # Explicitly no AWS_* / DB env vars set — a dry run must never touch
    # either, so this must succeed regardless.
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(var, raising=False)

    exit_code = bootstrap_super_admin.main(
        [
            "--email",
            "x@milkful.test",
            "--name",
            "X",
            "--admin-pool-id",
            "ap-south-1_fake",
            "--database-url",
            "sqlite:///:memory:",
        ]
    )

    assert exit_code == 0
