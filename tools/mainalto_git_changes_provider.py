#!/usr/bin/env python3
"""Bounded local Git CRUD candidate; no remote effects, hooks or retries."""

import argparse
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata
import uuid


REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
OP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,99}\Z")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)),
            *(f"LPT{n}" for n in range(1, 10))}
SCHEMA = "mainalto.git-change-set/1"


class Blocked(Exception):
    pass


class Unknown(Exception):
    pass


def _strict_dict(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise Blocked("DUPLICATE_JSON_KEY")
        obj[key] = value
    return obj


def _read_manifest(path):
    try:
        data = Path(path).read_bytes()
        if len(data) > 2 * 1024 * 1024:
            raise Blocked("MANIFEST_TOO_LARGE")
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=_strict_dict)
    except (OSError, UnicodeError, ValueError) as exc:
        raise Blocked("MANIFEST_UNREADABLE_OR_INVALID") from exc
    if not isinstance(obj, dict) or set(obj) != {"schema_version", "operation_id", "repository", "branch", "base_head", "commit_message", "changes"}:
        raise Blocked("MANIFEST_SHAPE_INVALID")
    if obj["schema_version"] != SCHEMA:
        raise Blocked("MANIFEST_SCHEMA_UNSUPPORTED")
    if not isinstance(obj["operation_id"], str) or not OP_ID.fullmatch(obj["operation_id"]):
        raise Blocked("OPERATION_ID_INVALID")
    if not isinstance(obj["repository"], str) or not REPOSITORY.fullmatch(obj["repository"]):
        raise Blocked("REPOSITORY_INVALID")
    if not isinstance(obj["branch"], str) or not 1 <= len(obj["branch"]) <= 160 or obj["branch"].startswith("-"):
        raise Blocked("BRANCH_INVALID")
    if not isinstance(obj["base_head"], str) or not HEX40.fullmatch(obj["base_head"]):
        raise Blocked("BASE_HEAD_INVALID")
    msg = obj["commit_message"]
    if not isinstance(msg, str) or not 1 <= len(msg) <= 120 or "\n" in msg or "\r" in msg or "\x00" in msg or not msg.strip():
        raise Blocked("COMMIT_MESSAGE_INVALID")
    entries = obj["changes"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
        raise Blocked("CHANGES_COUNT_INVALID")
    seen = set()
    total = 0
    decoded = []
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"op", "path", "expected_sha256", "content_base64"}:
            raise Blocked("CHANGE_SHAPE_INVALID")
        op, rel = item["op"], item["path"]
        if op not in ("create", "update", "delete"):
            raise Blocked("CHANGE_OPERATION_INVALID")
        _check_relative_path(rel)
        key = unicodedata.normalize("NFC", rel).casefold()
        if key in seen:
            raise Blocked("CHANGE_PATH_COLLISION")
        seen.add(key)
        digest, encoded = item["expected_sha256"], item["content_base64"]
        if op == "create":
            if digest is not None:
                raise Blocked("CREATE_PREIMAGE_MUST_BE_NULL")
        elif not isinstance(digest, str) or not HEX64.fullmatch(digest):
            raise Blocked("PREIMAGE_SHA256_INVALID")
        if op == "delete":
            if encoded is not None:
                raise Blocked("DELETE_CONTENT_MUST_BE_NULL")
            content = None
        else:
            if not isinstance(encoded, str) or len(encoded) > 1400000:
                raise Blocked("CONTENT_BASE64_INVALID")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise Blocked("CONTENT_BASE64_INVALID") from exc
            total += len(content)
            if total > 1024 * 1024:
                raise Blocked("CHANGESET_CONTENT_TOO_LARGE")
        decoded.append((op, rel, digest, content))
    return obj, decoded


def _check_relative_path(rel):
    if not isinstance(rel, str) or not rel or len(rel) > 260 or rel.startswith("/") or "\\" in rel or "\x00" in rel:
        raise Blocked("CHANGE_PATH_INVALID")
    if any(c in rel for c in ':*?"<>|') or any(ord(c) < 32 for c in rel):
        raise Blocked("CHANGE_PATH_INVALID")
    for segment in rel.split("/"):
        if (not segment or segment in (".", "..") or segment.endswith((" ", "."))
                or len(segment) > 120 or segment.casefold().startswith(".git")
                or segment.split(".", 1)[0].upper() in RESERVED):
            raise Blocked("CHANGE_PATH_INVALID")


def _git(checkout, *args, timeout=15):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0" if args and args[0] in ("status", "diff") else "1"
    try:
        return subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.quotePath=false", "-C", str(checkout), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, timeout=timeout, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unknown("GIT_COMMAND_UNAVAILABLE_OR_TIMEOUT") from exc


def _checked(checkout, *args):
    result = _git(checkout, *args)
    if result.returncode:
        raise Blocked("GIT_PREFLIGHT_FAILED")
    return result.stdout.decode("utf-8", "strict").strip()


def _preflight(checkout, manifest, entries):
    if not checkout.is_dir():
        raise Blocked("CHECKOUT_MISSING")
    try:
        top = Path(_checked(checkout, "rev-parse", "--show-toplevel")).resolve(strict=True)
    except (OSError, UnicodeError, ValueError) as exc:
        raise Blocked("CHECKOUT_UNRESOLVED") from exc
    if os.path.normcase(str(top)) != os.path.normcase(str(checkout.resolve(strict=True))):
        raise Blocked("CHECKOUT_ROOT_MISMATCH")
    branch = _checked(checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch != manifest["branch"]:
        raise Blocked("BRANCH_MISMATCH")
    actual = _checked(checkout, "rev-parse", "HEAD")
    if actual != manifest["base_head"]:
        raise Blocked("BASE_HEAD_MISMATCH")
    origin = _checked(checkout, "config", "--get", "remote.origin.url")
    owner_repo = manifest["repository"]
    allowed = (f"https://github.com/{owner_repo}", f"https://github.com/{owner_repo}.git",
               f"git@github.com:{owner_repo}", f"git@github.com:{owner_repo}.git",
               f"ssh://git@github.com/{owner_repo}", f"ssh://git@github.com/{owner_repo}.git")
    if origin.lower() not in {url.lower() for url in allowed}:
        raise Blocked("ORIGIN_REPOSITORY_MISMATCH")
    if _checked(checkout, "status", "--porcelain=v1", "--untracked-files=all"):
        raise Blocked("CHECKOUT_NOT_CLEAN")
    # Git branch validation is local, cannot run hooks, filters or network.
    _checked(checkout, "check-ref-format", "--branch", manifest["branch"])
    for identity in ("user.name", "user.email"):
        if not _checked(checkout, "config", "--get", identity):
            raise Blocked("GIT_AUTHOR_NOT_CONFIGURED")
    for op, rel, digest, content in entries:
        target = checkout.joinpath(*rel.split("/"))
        cursor = checkout
        for segment in rel.split("/"):
            cursor = cursor / segment
            if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
                raise Blocked("CHANGE_PATH_LINK_DENIED")
            if cursor != target and cursor.exists() and not cursor.is_dir():
                raise Blocked("CREATE_PARENT_NOT_DIRECTORY")
        if not target.resolve(strict=False).is_relative_to(checkout.resolve(strict=True)):
            raise Blocked("CHANGE_PATH_ESCAPES_CHECKOUT")
        if op == "create":
            if target.exists() or target.is_symlink() or (hasattr(target, "is_junction") and target.is_junction()):
                raise Blocked("CREATE_PATH_ALREADY_EXISTS")
            if target.parent.exists() and not target.parent.is_dir():
                raise Blocked("CREATE_PARENT_NOT_DIRECTORY")
        else:
            if not target.is_file() or target.is_symlink():
                raise Blocked("PREIMAGE_FILE_ABSENT_OR_NONREGULAR")
            if target.stat().st_size > 2 * 1024 * 1024:
                raise Blocked("PREIMAGE_FILE_TOO_LARGE")
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise Blocked("PREIMAGE_SHA256_MISMATCH")
            if op == "update" and target.read_bytes() == content:
                raise Blocked("UPDATE_IS_NO_OP")
            if not _git(checkout, "ls-files", "--error-unmatch", "--", rel).returncode == 0:
                raise Blocked("PREIMAGE_NOT_TRACKED")
        # Prevent repository-configured clean/process filters from launching
        # arbitrary commands when Git stages the requested paths.
        attrs = _git(checkout, "check-attr", "filter", "--", rel)
        if attrs.returncode or not attrs.stdout.decode("utf-8", "strict").strip().endswith(": filter: unspecified"):
            raise Blocked("GIT_FILTER_ATTRIBUTE_DENIED")
        ignored = _git(checkout, "check-ignore", "-q", "--", rel)
        if ignored.returncode not in (0, 1) or ignored.returncode == 0:
            raise Blocked("GIT_IGNORED_PATH_DENIED")
    return actual


def observe(manifest_path, checkout_path, *, mode="Plan"):
    if mode not in ("Plan", "Commit"):
        raise Blocked("MODE_INVALID")
    manifest, entries = _read_manifest(manifest_path)
    checkout = Path(checkout_path).resolve(strict=False)
    _preflight(checkout, manifest, entries)
    base = dict(schema_version="mainalto.git-change-observation/1",
                observation_id=str(uuid.uuid4()), operation_id=manifest["operation_id"],
                repository=manifest["repository"], branch=manifest["branch"],
                base_head=manifest["base_head"], new_head=None,
                changed_paths=[rel for _, rel, _, _ in entries], mode=mode,
                observed_at=datetime.now(timezone.utc).isoformat(), attempted_commit=False)
    if mode == "Plan":
        return dict(base, status="READY", reason_code="EXACT_CHANGESET_PREFLIGHT_VERIFIED",
                    next_action="EXPLICIT_COMMIT")
    # Single-process preflight again immediately before touching the checkout.
    _preflight(checkout, manifest, entries)
    base["attempted_commit"] = True
    try:
        for op, rel, _, content in entries:
            target = checkout.joinpath(*rel.split("/"))
            if op == "delete":
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation for new files. Others had preimage hash checks.
                with target.open("xb" if op == "create" else "wb") as out:
                    out.write(content)
        paths = [rel for _, rel, _, _ in entries]
        added = _git(checkout, "add", "-A", "--", *paths)
        if added.returncode:
            raise Unknown("GIT_STAGE_RESULT_UNKNOWN")
        # Never run hooks. Set a non-existent hooks directory, separately ensure absent.
        no_hooks = checkout / ".mainalto-hooks-disabled"
        if no_hooks.exists():
            raise Unknown("HOOKS_DISABLED_DIRECTORY_CONFLICT")
        committed = _git(checkout, "-c", f"core.hooksPath={no_hooks}",
                         "-c", "commit.gpgsign=false", "commit", "--no-verify",
                         "-m", manifest["commit_message"],
                         "-m", "MainAlto-Operation-Id: " + manifest["operation_id"], timeout=30)
        if committed.returncode:
            raise Unknown("GIT_COMMIT_RESULT_UNKNOWN")
        new_head = _checked(checkout, "rev-parse", "HEAD")
        parent = _checked(checkout, "rev-parse", "HEAD^1")
        paths_now = _checked(checkout, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
        if (new_head == manifest["base_head"] or parent != manifest["base_head"]
                or set(paths_now) != set(paths) or len(paths_now) != len(paths)
                or _checked(checkout, "status", "--porcelain=v1", "--untracked-files=all")):
            raise Unknown("COMMIT_READBACK_INCONSISTENT")
        return dict(base, status="COMMITTED", reason_code="LOCAL_COMMIT_READBACK_VERIFIED",
                    new_head=new_head, next_action="EXPLICIT_V0_PUBLISH")
    except (OSError, Blocked, UnicodeError, ValueError) as exc:
        raise Unknown("LOCAL_COMMIT_OUTCOME_UNPROVEN") from exc


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--mode", choices=("Plan", "Commit"), default="Plan")
    args = parser.parse_args(argv)
    try:
        report = observe(args.manifest, args.checkout, mode=args.mode)
    except (Blocked, Unknown) as exc:
        report = dict(schema_version="mainalto.git-change-observation/1",
                      observation_id=str(uuid.uuid4()), operation_id=None,
                      repository=None, branch=None, base_head=None, new_head=None,
                      changed_paths=None, mode=args.mode,
                      observed_at=datetime.now(timezone.utc).isoformat(),
                      attempted_commit=None if isinstance(exc, Unknown) else False,
                      status="UNKNOWN" if isinstance(exc, Unknown) else "BLOCKED",
                      reason_code=str(exc), next_action="RECONCILE_READ_ONLY" if isinstance(exc, Unknown) else "FIX_PREFLIGHT")
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if report["status"] in ("READY", "COMMITTED") else (3 if report["status"] == "UNKNOWN" else 2)


if __name__ == "__main__":
    sys.exit(main())
