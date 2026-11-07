#!/usr/bin/env python3
"""GitHub repo-create v1 candidate: private, self-owned, one-shot and typed.

Requires GitHub CLI auth already configured. Never acquires, stores or prints
credentials; creates no local checkout and never pushes source code.
"""

import argparse
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import sys
import uuid


NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
HTTP_STATUS = re.compile(r"^HTTP/\S+\s+(\d{3})\b", re.MULTILINE)


class Blocked(Exception):
    pass


class Unknown(Exception):
    pass


def _gh(*args, timeout=30):
    env = dict(os.environ)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    # The repository contract only authorizes github.com, never GH Enterprise.
    env["GH_HOST"] = "github.com"
    try:
        return subprocess.run(["gh", *args], input=b"", stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout,
                              check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unknown("GH_UNAVAILABLE_OR_TIMEOUT") from exc


def _authenticated_owner():
    result = _gh("api", "user", "--jq", ".login")
    if result.returncode:
        raise Blocked("GH_AUTH_UNAVAILABLE")
    try:
        login = result.stdout.decode("utf-8", "strict").strip()
    except UnicodeError as exc:
        raise Unknown("GH_IDENTITY_INVALID") from exc
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
        raise Unknown("GH_IDENTITY_INVALID")
    return login


def _remote_repo(repository):
    """Return (PRESENT|ABSENT, visibility), require HTTP evidence.

    A 404 is interpretable as absent only because caller has already verified
    the authenticated identity is the requested personal repository owner.
    """
    response = _gh("api", "-i", "repos/" + repository)
    try:
        raw = (response.stdout + b"\n" + response.stderr).decode("utf-8", "strict")
    except UnicodeError as exc:
        raise Unknown("GH_RESPONSE_INVALID") from exc
    codes = HTTP_STATUS.findall(raw)
    if not codes:
        raise Unknown("GH_HTTP_STATUS_UNPROVEN")
    status = int(codes[-1])
    if status == 404:
        return "ABSENT", None
    if status != 200 or response.returncode:
        raise Unknown("GH_REMOTE_INSPECTION_FAILED")
    # gh api -i prints HTTP headers, a blank separator, then a JSON body.
    marker = re.search(r"\r?\n\r?\n(\{[\s\S]*)", raw)
    if marker is None:
        raise Unknown("GH_JSON_BODY_MISSING")
    try:
        obj = json.loads(marker.group(1))
    except (ValueError, TypeError) as exc:
        raise Unknown("GH_JSON_BODY_INVALID") from exc
    if not isinstance(obj, dict) or str(obj.get("full_name", "")).lower() != repository.lower():
        raise Unknown("GH_REPOSITORY_ID_MISMATCH")
    vis = obj.get("visibility")
    if vis not in ("private", "public", "internal"):
        raise Unknown("GH_VISIBILITY_UNPROVEN")
    return "PRESENT", vis.upper()


def observe(repository, *, mode="Inspect", confirm_repository=None):
    if not isinstance(repository, str) or not NAME.fullmatch(repository):
        raise Blocked("REPOSITORY_INVALID")
    if mode not in ("Inspect", "Create"):
        raise Blocked("MODE_INVALID")
    owner = repository.split("/", 1)[0]
    actor = _authenticated_owner()
    if owner.lower() != actor.lower():
        raise Blocked("ONLY_AUTHENTICATED_PERSONAL_OWNER_ALLOWED")
    if mode == "Create" and confirm_repository != repository:
        raise Blocked("EXPLICIT_REPOSITORY_CONFIRMATION_REQUIRED")
    state, visibility = _remote_repo(repository)
    base = dict(schema_version="mainalto.repo-create-observation/1",
                observation_id=str(uuid.uuid4()), repository=repository,
                authenticated_owner=actor, visibility=visibility,
                observed_at=datetime.now(timezone.utc).isoformat(),
                mode=mode, attempted_create=False)
    if state == "PRESENT":
        return dict(base, status="PRESENT", reason_code="REPOSITORY_ALREADY_PRESENT")
    if mode == "Inspect":
        return dict(base, status="READY", reason_code="PRIVATE_CREATE_NOT_REQUESTED")

    base["attempted_create"] = True
    creation_command_succeeded = False
    try:
        creation_command_succeeded = (_gh("repo", "create", repository, "--private", timeout=60).returncode == 0)
    except Unknown:
        # The create may have completed even when the process disappeared.
        pass
    try:
        after, visibility = _remote_repo(repository)
    except Unknown:
        return dict(base, status="UNKNOWN", reason_code="CREATE_READBACK_UNAVAILABLE")
    base["visibility"] = visibility
    if after == "PRESENT" and visibility == "PRIVATE" and creation_command_succeeded:
        return dict(base, status="CREATED", reason_code="PRIVATE_REPO_READBACK_VERIFIED")
    if after == "PRESENT" and visibility == "PRIVATE":
        return dict(base, status="UNKNOWN", reason_code="CREATE_COMMAND_UNCONFIRMED_REMOTE_PRESENT")
    return dict(base, status="UNKNOWN", reason_code="CREATE_OUTCOME_UNPROVEN")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--mode", choices=("Inspect", "Create"), default="Inspect")
    parser.add_argument("--confirm-repository")
    args = parser.parse_args(argv)
    try:
        result = observe(args.repository, mode=args.mode,
                         confirm_repository=args.confirm_repository)
    except (Blocked, Unknown) as exc:
        result = dict(schema_version="mainalto.repo-create-observation/1",
                      observation_id=str(uuid.uuid4()), repository=args.repository,
                      authenticated_owner=None, visibility=None,
                      observed_at=datetime.now(timezone.utc).isoformat(), mode=args.mode,
                      attempted_create=None if isinstance(exc, Unknown) else False,
                      status="UNKNOWN" if isinstance(exc, Unknown) else "BLOCKED",
                      reason_code=str(exc))
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0 if result["status"] in ("PRESENT", "READY", "CREATED") else (3 if result["status"] == "UNKNOWN" else 2)


if __name__ == "__main__":
    sys.exit(main())
