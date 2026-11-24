#!/usr/bin/env python3
"""Resolve fixed-destination manual rehearsal inputs without publication credentials.

Run this in a secretless job. Credential-bearing jobs must independently compare
source_sha and the selected helper SHA with their Environment's reviewed pins.
"""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys

EXECUTION_REF = "refs/heads/anmrasto/gha-rehearsal-20261119"
REPOSITORIES = {f"anmol372/{name}" for name in ("opflex", "aci-containers", "acc-provision")}
SHA = re.compile(r"[0-9a-f]{40}\Z")
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){3}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args], text=True).strip()


def resolve_source(directory, requested):
    if SHA.fullmatch(requested):
        ref = requested
    else:
        branch = requested.removeprefix("refs/heads/")
        if not BRANCH.fullmatch(branch):
            raise ValueError("source_ref must be a branch or a full lowercase commit SHA")
        git(directory, "check-ref-format", "--branch", branch)
        ref = f"refs/heads/{branch}"
    git(directory, "fetch", "--no-tags", "origin", ref)
    commit = git(directory, "rev-parse", "FETCH_HEAD^{commit}")
    if not SHA.fullmatch(commit):
        raise ValueError("source_ref did not resolve to a full commit SHA")
    if SHA.fullmatch(requested) and requested != commit:
        raise ValueError("requested source SHA did not match fetched commit")
    return commit


def resolve(args):
    if (os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != EXECUTION_REF
            or os.environ.get("GITHUB_REPOSITORY") not in REPOSITORIES):
        raise ValueError("rehearsal requires the reviewed personal-fork execution branch")
    if not VERSION.fullmatch(args.test_version):
        raise ValueError("test_version must have form N.N.N.N")
    if args.stream == "rc":
        if not re.fullmatch(r"[1-9][0-9]*", args.rc_number):
            raise ValueError("RC stream requires a positive rc_number")
    elif args.rc_number:
        raise ValueError("rc_number is only valid for the RC stream")
    if args.operation == "runtime" and not DIGEST.fullmatch(args.base_digest):
        raise ValueError("runtime images require the approved captured base_digest")
    if args.base_digest and not DIGEST.fullmatch(args.base_digest):
        raise ValueError("base_digest must be a sha256 digest")
    # Validate all values before any fetch. A mutable branch is captured exactly once.
    commit = resolve_source(args.source_directory, args.source_ref)
    return {
        "source_sha": commit, "source_ref": args.source_ref,
        "release_tag": args.test_version, "stream": args.stream,
        "rc_number": args.rc_number, "operation": args.operation,
        "base_digest": args.base_digest,
        "date_tag": datetime.now(timezone.utc).strftime("%m%d%y"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-directory", type=Path, required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--test-version", required=True)
    parser.add_argument("--stream", choices=("z", "rc", "release"), required=True)
    parser.add_argument("--rc-number", default="")
    parser.add_argument("--operation", choices=("runtime", "base", "publish-package", "refresh-image-status"), required=True)
    parser.add_argument("--base-digest", default="")
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        values = resolve(args)
        with args.github_output.open("a", encoding="utf-8") as output:
            for name, value in values.items():
                output.write(f"{name}={value}\n")
    except (ValueError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
