#!/usr/bin/env python3
"""One-shot, operator-invoked Git publisher for an already committed branch.

This is a source-repository provider, not the GitPS7 outbox writer. It never
creates commits, changes checkout files, runs hooks, retries a push, or stores
credentials. Failure after a possible push is UNKNOWN until remote readback.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid


SHA = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,179}\Z")


class Blocked(Exception):
    pass


class TransportUncertain(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Blocked(code)


def valid_branch(value):
    return (isinstance(value, str) and BRANCH.fullmatch(value) is not None
            and "@{" not in value and ".." not in value and "//" not in value
            and not value.endswith(("/", ".")) and
            all(part not in ("", ".", "..") and not part.startswith(".")
                and not part.endswith((".", ".lock")) for part in value.split("/")))


def _run_git(checkout, *args, timeout=35):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    no_hooks = "NUL" if os.name == "nt" else os.devnull
    try:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=" + no_hooks, *args],
            cwd=checkout, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, shell=False, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TransportUncertain("GIT_PROCESS_UNAVAILABLE_OR_TIMEOUT") from exc


def _git(checkout, *args, timeout=35):
    result = _run_git(checkout, *args, timeout=timeout)
    if result.returncode:
        raise Blocked("GIT_COMMAND_FAILED")
    try:
        return result.stdout.decode("utf-8", "strict").strip()
    except UnicodeError as exc:
        raise Blocked("GIT_OUTPUT_INVALID") from exc


def _remote_head(checkout, branch):
    try:
        result = _git(checkout, "ls-remote", "--heads", "origin", "refs/heads/" + branch)
    except Blocked as exc:
        if str(exc) == "GIT_COMMAND_FAILED":
            raise TransportUncertain("REMOTE_INSPECTION_UNAVAILABLE") from exc
        raise
    if not result:
        return None
    lines = result.splitlines()
    require(len(lines) == 1, "REMOTE_REF_AMBIGUOUS")
    fields = lines[0].split("\t")
    require(len(fields) == 2 and SHA.fullmatch(fields[0]) is not None
            and fields[1] == "refs/heads/" + branch, "REMOTE_REF_INVALID")
    return fields[0]


def _github_remote_url(repository):
    return {"https://github.com/" + repository,
            "https://github.com/" + repository + ".git",
            "git@github.com:" + repository,
            "git@github.com:" + repository + ".git",
            "ssh://git@github.com/" + repository,
            "ssh://git@github.com/" + repository + ".git"}


def publish(checkout, repository, branch, expected_head, *, mode="Inspect",
            allow_create_branch=False, fixture_remote=None):
    """Read/verify, optionally attempt one fast-forward push, then read back.

    `fixture_remote` exists only for local disposable tests; CLI cannot supply
    it. The ordinary provider accepts only the explicitly named GitHub remote.
    """
    require(isinstance(repository, str) and REPOSITORY.fullmatch(repository) is not None,
            "REPOSITORY_INVALID")
    require(valid_branch(branch), "BRANCH_INVALID")
    require(isinstance(expected_head, str) and SHA.fullmatch(expected_head) is not None,
            "HEAD_INVALID")
    require(mode in ("Inspect", "Publish"), "MODE_INVALID")
    require(type(allow_create_branch) is bool, "CREATE_BRANCH_FLAG_INVALID")
    checkout = Path(checkout).resolve(strict=True)
    require(checkout.is_dir() and not checkout.is_symlink(), "CHECKOUT_INVALID")
    require(_git(checkout, "rev-parse", "--is-inside-work-tree") == "true",
            "NOT_A_WORKTREE")
    require(_git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD") == branch,
            "LOCAL_BRANCH_MISMATCH")
    require(_git(checkout, "rev-parse", "HEAD") == expected_head,
            "LOCAL_HEAD_MISMATCH")
    require(not _git(checkout, "status", "--porcelain=v1", "-uall"),
            "UNCOMMITTED_CHANGES")
    remote = _git(checkout, "remote", "get-url", "--push", "origin")
    if fixture_remote is not None:
        require(remote == str(fixture_remote), "REMOTE_MISMATCH")
    else:
        require(remote in _github_remote_url(repository), "REMOTE_MISMATCH")
    observed = _remote_head(checkout, branch)
    base = dict(schema_version="mainalto.git-publish-observation/1",
                observation_id=str(uuid.uuid4()),
                repository=repository, branch=branch, expected_head=expected_head,
                remote_head=observed, observed_at=datetime.now(timezone.utc).isoformat(),
                mode=mode, attempted_push=False)
    if observed == expected_head:
        return dict(base, status="VERIFIED", reason_code="REMOTE_ALREADY_MATCHED")
    if observed is None and not allow_create_branch:
        return dict(base, status="BLOCKED", reason_code="REMOTE_BRANCH_ABSENT")
    if mode == "Inspect":
        return dict(base, status="READY", reason_code="PUSH_NOT_REQUESTED")

    # A changed local branch is not a reason to publish a different commit.
    require(_git(checkout, "rev-parse", "HEAD") == expected_head,
            "LOCAL_HEAD_CHANGED")
    # No force, deletion, tag, multi-ref, or implicit upstream update.
    base["attempted_push"] = True
    # Even a failed/timeout return might mean the server accepted the push.
    # Never retry it automatically; consult exact remote ref once.
    try:
        _run_git(checkout, "push", "--no-verify", "--porcelain", "origin",
                 expected_head + ":refs/heads/" + branch, timeout=60)
    except TransportUncertain:
        # Read back after a process timeout as well; never issue a second push.
        pass
    try:
        after = _remote_head(checkout, branch)
    except (Blocked, TransportUncertain):
        return dict(base, status="UNKNOWN", reason_code="REMOTE_READBACK_UNAVAILABLE",
                    remote_head=None)
    base["remote_head"] = after
    if after == expected_head:
        return dict(base, status="VERIFIED", reason_code="REMOTE_COMMIT_VERIFIED")
    return dict(base, status="UNKNOWN", reason_code="PUSH_OUTCOME_UNCONFIRMED")


def main(argv=None):
    parser = argparse.ArgumentParser(description="MainAlto native PS7 -> Git provider")
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--mode", choices=["Inspect", "Publish"], default="Inspect")
    parser.add_argument("--allow-create-branch", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = publish(args.checkout, args.repository, args.branch,
                         args.expected_head, mode=args.mode,
                         allow_create_branch=args.allow_create_branch)
    except (Blocked, TransportUncertain, OSError, ValueError) as exc:
        reason = str(exc)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,100}", reason):
            reason = "LOCAL_PROVIDER_ERROR"
        result = dict(schema_version="mainalto.git-publish-observation/1",
                      observation_id=str(uuid.uuid4()),
                      repository=args.repository, branch=args.branch,
                      expected_head=args.expected_head, remote_head=None,
                      observed_at=datetime.now(timezone.utc).isoformat(),
                      mode=args.mode, attempted_push=False,
                      status="UNKNOWN" if isinstance(exc, TransportUncertain) else "BLOCKED",
                      reason_code=reason)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] in ("READY", "VERIFIED") else (3 if result["status"] == "UNKNOWN" else 2)


if __name__ == "__main__":
    sys.exit(main())
