"""Set and verify tag expiry in the isolated rehearsal registry."""

import base64
import json
import os
import re
import time
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

REGISTRY = "quay.io/anmrasto"
EXECUTION_REF = "refs/heads/anmrasto/gha-rehearsal-20261119"
EXPIRY_LABEL = "quay.expires-after"
TTL_SECONDS = 6 * 60 * 60


def active():
    if os.environ.get("CI_REHEARSAL") != "1":
        return False
    if (os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != EXECUTION_REF
            or os.environ.get("GITHUB_REPOSITORY") not in {
                "anmol372/opflex", "anmol372/aci-containers", "anmol372/acc-provision",
            }):
        raise ValueError("rehearsal requires the reviewed test-fork workflow")
    return True


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(path, data=None):
    username = os.environ.get("CI_QUAY_USERNAME")
    token = os.environ.get("CI_QUAY_TOKEN")
    if not username or not token:
        raise ValueError("rehearsal Quay credentials are missing")
    auth = base64.b64encode(f"{username}:{token}".encode()).decode()
    req = Request("https://quay.io/api/v1/repository/" + path,
                  data=json.dumps(data).encode() if data is not None else None,
                  method="PUT" if data is not None else "GET",
                  headers={"Authorization": f"Basic {auth}", "Accept": "application/json",
                           "Content-Type": "application/json"})
    with build_opener(NoRedirect()).open(req, timeout=15) as response:
        return json.load(response) if data is None else None


def parts(reference):
    match = re.fullmatch(r"quay\.io/anmrasto/([a-z0-9-]+):([a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127})", reference)
    if not active() or not match:
        raise ValueError("rehearsal writes must target a private test image tag")
    return "anmrasto/" + match[1], match[2]


def current_tag(reference):
    repo, tag = parts(reference)
    query = urlencode({"specificTag": tag, "onlyActiveTags": "true"})
    data = request(f"{repo}/tag/?{query}")
    rows = [row for row in data["tags"] if row["name"] == tag and
            (row.get("end_ts") is None or row["end_ts"] > time.time())]
    if len(rows) > 1:
        raise ValueError("ambiguous active rehearsal tag")
    return rows[0] if rows else None


def verify_expiry(reference, digest, *, created):
    """Apply expiry only to a tag this attempt pushed, then verify readback."""
    repo, tag = parts(reference)
    for attempt in range(3):
        try:
            row = current_tag(reference)
            if row is None or row["manifest_digest"] != digest:
                raise ValueError("rehearsal tag digest differs from the pushed image")
            start = row["start_ts"]
            if type(start) is not int or start > time.time() + 30:
                raise ValueError("invalid rehearsal tag start time")
            deadline = start + TTL_SECONDS
            expiry = row.get("end_ts")
            if created:
                desired = min(expiry, deadline) if type(expiry) is int else deadline
                request(f"{repo}/tag/{quote(tag, safe='')}", {"expiration": desired})
                row = current_tag(reference)
                expiry = row.get("end_ts") if row else None
            if (row is not None and row["manifest_digest"] == digest and row["start_ts"] == start
                    and type(expiry) is int and time.time() < expiry <= deadline
                    and row.get("expiration")):
                return expiry
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if attempt != 2:
            time.sleep(1)
    raise ValueError("unable to verify six-hour expiration for rehearsal tag")
