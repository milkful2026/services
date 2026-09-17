#!/usr/bin/env python3
"""One-time, HUMAN-RUN bootstrap for the very first Super-Admin account
(spec MA-129 §7 "Migration note" / §12 Open Question 3).

Why this exists as a standalone script and not an API call: FR-3's
`POST /v1/admin/users` is itself Super-Admin-only — there is no
Super-Admin yet to authorize creating the first one. It is also not a
plain SQL migration (migrations/0001_admin_user.sql only creates the
`admin_user` table; it deliberately seeds no rows), because creating the
first admin also requires a paired Cognito Admin Pool user, and
`services/README.md` §3.6 requires production migrations/data changes to
be explicit and human-approved rather than silently bundled into schema
changes.

**This resolves spec §12 Q3 as "a one-time seed script requiring human
execution"** — the other two options the spec floated (a manual Cognito
console step, or a CDK custom resource) were considered and rejected:
a console-only step would leave the Aurora row uncreated (both halves
must exist, mirroring the normal create-admin saga), and a CDK custom
resource would run automatically on every `cdk deploy`, which is exactly
the "not a self-service action" property this needs to avoid.

Defaults to a DRY RUN (prints what it would do, touches nothing). Pass
--execute to actually create the Cognito user and Aurora row. Requires
real AWS credentials and DB connectivity — this script is intentionally
NOT covered by the offline pytest suite beyond a dry-run smoke test.

Usage:
    python scripts/bootstrap_super_admin.py \\
        --email superadmin@milkful.example \\
        --name "First Super Admin" \\
        --admin-pool-id ap-south-1_XXXXXXXXX \\
        --database-url postgresql+psycopg2://user:pass@host:5432/admin \\
        --region ap-south-1 \\
        --execute
"""

import argparse
import sys
import uuid

import boto3
from botocore.exceptions import ClientError


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True, help="Email address for the first Super Admin")
    parser.add_argument("--name", required=True, help="Display name for the first Super Admin")
    parser.add_argument("--admin-pool-id", required=True, help="Admin Cognito User Pool ID")
    parser.add_argument(
        "--database-url", required=True, help="SQLAlchemy URL for this service's Admin Aurora database"
    )
    parser.add_argument("--region", default="ap-south-1", help="AWS region (default: ap-south-1)")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform the bootstrap. Without this flag, only a dry-run preview is printed.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    email = args.email.strip().lower()
    name = args.name.strip()

    print(f"{'EXECUTING' if args.execute else 'DRY RUN'} bootstrap for Super Admin {email!r}")
    print(f"  Admin Cognito Pool: {args.admin_pool_id}")
    print(f"  Admin database:     {args.database_url.split('@')[-1] if '@' in args.database_url else args.database_url}")

    if not args.execute:
        print("\nNo changes made. Re-run with --execute to actually bootstrap this account.")
        return 0

    # Local imports so `python scripts/bootstrap_super_admin.py --help`
    # and the dry-run path work without src/ on sys.path or SQLAlchemy
    # installed in every environment that just wants to preview usage.
    sys.path.insert(0, "src")
    from adapters.admin_user_repository import SqlAlchemyAdminUserRepository
    from domain.admin_models import AdminRole, AdminStatus, AdminUser
    from sqlalchemy import create_engine

    engine = create_engine(args.database_url)
    repo = SqlAlchemyAdminUserRepository(engine)

    existing = repo.get_by_email(email)
    if existing is not None:
        print(f"ERROR: an admin_user row already exists for {email!r} (id={existing.id}). Aborting.")
        return 1

    client = boto3.client("cognito-idp", region_name=args.region)
    print(f"Creating Cognito user {email!r} in pool {args.admin_pool_id!r} ...")
    try:
        client.admin_create_user(
            UserPoolId=args.admin_pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "name", "Value": name},
            ],
            MessageAction="SUPPRESS",
        )
        created_user = client.admin_get_user(UserPoolId=args.admin_pool_id, Username=email)
    except ClientError as exc:
        print(f"ERROR: Cognito admin_create_user failed: {exc}")
        return 1

    try:
        client.admin_add_user_to_group(UserPoolId=args.admin_pool_id, Username=email, GroupName="SuperAdmin")
    except ClientError as exc:
        print(f"ERROR: failed to add user to SuperAdmin group, compensating (deleting Cognito user): {exc}")
        client.admin_delete_user(UserPoolId=args.admin_pool_id, Username=email)
        return 1

    cognito_sub = {a["Name"]: a["Value"] for a in created_user["UserAttributes"]}["sub"]

    try:
        repo.create(
            AdminUser(
                id=str(uuid.uuid4()),
                cognito_sub=cognito_sub,
                name=name,
                email=email,
                role=AdminRole.SUPER_ADMIN,
                status=AdminStatus.PENDING,
                ip_allowlist=[],
                max_concurrent_sessions=None,
                created_by=None,  # the one and only admin_user row with no creator (spec §7)
            )
        )
    except Exception as exc:
        # Broadened from `except AdminEmailExistsError` — any Aurora
        # failure here (not just a uniqueness conflict; a transient
        # connection error is wrapped as ExternalServiceUnavailableError
        # by the repository, not AdminEmailExistsError) leaves the same
        # orphaned-Cognito-user risk and needs the same compensation.
        print(f"ERROR: Aurora insert failed ({exc}), compensating (deleting Cognito user)")
        try:
            client.admin_delete_user(UserPoolId=args.admin_pool_id, Username=email)
        except ClientError as compensation_exc:
            print(
                f"ERROR: compensation FAILED — Cognito user {email!r} is orphaned "
                f"(no matching Aurora row) and needs manual cleanup: {compensation_exc}"
            )
        return 1

    print(
        f"\nBootstrap complete. {email!r} exists in both the Admin Cognito Pool "
        "(Pending — FORCE_CHANGE_PASSWORD) and admin_user (status=Pending).\n"
        "This admin still needs to complete password-set + TOTP enrollment before "
        "they can log in (spec's own gap — see this service's README "
        "'Architecture decisions flagged for review' for the Pending -> Active "
        "transition question) and the invite delivery mechanism itself is not "
        "triggered by this script (no admin.user.created event is published "
        "here — this bootstrap is intentionally out of band)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
