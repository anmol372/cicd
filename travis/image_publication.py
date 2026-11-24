#!/usr/bin/env python3
"""Publish GHA images with durable per-attempt receipts and aliases last."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from scan_result import collect, environment, execute, stamp, DIGEST
import rehearsal_quay

ACI_IMAGES = {"aci-containers-host", "aci-containers-host-ovscni", "aci-containers-controller", "cnideploy",
              "aci-containers-operator", "openvswitch", "aci-containers-webhook", "aci-containers-certmanager"}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def publish(args):
    directory = Path(args.artifacts)
    receipt_path = directory / "publication" / (args.image + ".json")
    built = f"{args.registry}/{args.image}:{args.tag}"
    rehearsal = rehearsal_quay.active()
    expected_built_registry = rehearsal_quay.REGISTRY if rehearsal else "quay.io/noirolabs"
    if args.registry != expected_built_registry:
        raise ValueError("image build registry does not match the workflow destination")
    policy = os.environ.get("CI_SCAN_POLICY", "fatal")
    if policy not in ("fatal", "advisory"):
        write(receipt_path, {
            "schema_version": 1, "image": args.image, "state": "failed", "attempts": [],
            "critical_failure": "unknown-scan-policy", "scan_policy": policy, "finished_at": stamp()})
        return 1
    try:
        ttl_label = execute(["docker", "image", "inspect", "--format",
                             '{{index .Config.Labels "quay.expires-after"}}', built],
                            timeout=args.timeout).strip()
        if rehearsal and ttl_label != "6h":
            raise ValueError("rehearsal image needs the six-hour expiry label")
        if not rehearsal and ttl_label not in ("", "<no value>"):
            raise ValueError("unexpected expiry label")
    except (OSError, subprocess.SubprocessError, ValueError):
        write(receipt_path, {"schema_version": 1, "image": args.image, "state": "failed",
                             "attempts": [], "references": [],
                             "critical_failure": "invalid-test-expiration-label", "finished_at": stamp()})
        return 1
    report_dir = directory / "reports" / args.image
    base = directory / "base-provenance" / (args.image + ".json")
    scan_data = collect(args.registry + "/" + args.image + ":" + args.tag,
                        base if base.exists() else None, report_dir,
                        lock=os.environ.get("SCAN_TOOL_LOCK"), timeout=float(os.environ.get("SCAN_TIMEOUT_SECONDS", "600")))
    build_inputs, build_input_error = [], None
    if args.image == "opflex":
        try:
            captured = json.loads((directory / "base-provenance/opflex-build.json").read_text())
            values = dict(line.split("=", 1) for line in (directory / "opflex-build-base-input.txt").read_text().splitlines() if line)
            reference, expected = values["opflex_build_base_ref"], values["opflex_build_base_digest"]
            if (captured.get("state") != "ok" or captured.get("captured_before_build") is not True
                    or captured.get("reference") != reference or captured.get("digest") != expected
                    or not DIGEST.fullmatch(expected)):
                raise ValueError("child input and prebuild capture disagree")
            build_inputs.append({"role": "opflex-build-base", "reference": reference, "digest": expected,
                                 "image_id": captured["image_id"], "captured_before_build": True,
                                 "immutable": reference.endswith("@" + expected)})
        except (OSError, ValueError, KeyError, TypeError):
            build_input_error = "actual-build-base-input-not-bound"
    elif args.image in ACI_IMAGES:
        try:
            captured = json.loads((directory / "base-provenance/aci-iptables-base.json").read_text())
            if not isinstance(captured, dict):
                raise ValueError("invalid ACI iptables input proof")
            reference, expected = captured["reference"], captured["digest"]
            if (captured.get("state") != "ok" or captured.get("role") != "opflex-build-base"
                    or captured.get("captured_before_build") is not True or captured.get("immutable") is not True
                    or not DIGEST.fullmatch(expected) or not DIGEST.fullmatch(captured.get("image_id", ""))
                    or reference != f"{expected_built_registry}/opflex-build-base@" + expected):
                raise ValueError("ACI iptables input was not immutably verified before extraction")
            build_inputs.append({key: captured[key] for key in
                                 ("role", "reference", "digest", "image_id", "captured_before_build", "immutable")})
        except (OSError, ValueError, KeyError, TypeError):
            build_input_error = "actual-iptables-build-input-not-bound"
    receipt = {"schema_version": 1, "image": args.image, "image_id": scan_data.get("image_id"),
               "state": "in-progress", "attempts": [], "references": [], "alias_policy": "aliases-last",
               "started_at": stamp(), "critical_failure": None, "scan_policy": policy}
    write(receipt_path, receipt)
    expected_id = scan_data.get("image_id")
    other_tags = list(dict.fromkeys(tag for tag in re.split(r"[, ]+", args.other_tags) if tag and tag != args.z_tag))

    def command(argv, phase, reference, config=None):
        item = {"phase": phase, "reference": reference, "command": argv[:2], "state": "started", "started_at": stamp()}
        receipt["attempts"].append(item)
        write(receipt_path, receipt)
        env = environment()
        if config:
            env["DOCKER_CONFIG"] = config
        try:
            output = subprocess.run(argv, env=env, check=True, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=args.timeout).stdout
            item.update(state="ok", finished_at=stamp())
            return output
        except (OSError, subprocess.SubprocessError) as exc:
            item.update(state="error", finished_at=stamp(), reason="timeout" if isinstance(exc, subprocess.TimeoutExpired) else "command-failed")
            raise RuntimeError("critical Docker publication operation failed") from exc
        finally:
            write(receipt_path, receipt)

    def push(reference, registry, config=None, tag_from=True, verify=True):
        repository = reference.rsplit(":", 1)[0]
        existing = None
        reused = False
        if rehearsal:
            try:
                existing = rehearsal_quay.current_tag(reference)
                if existing is not None:
                    previous_digest = existing["manifest_digest"]
                    if not DIGEST.fullmatch(previous_digest):
                        raise ValueError("invalid active tag digest")
                    previous = f"{repository}@{previous_digest}"
                    command(["docker", "pull", previous], "inspect-active-tag", reference, config)
                    reused = command(["docker", "image", "inspect", "--format", "{{.Id}}", previous],
                                     "inspect-active-image-id", reference, config).strip() == expected_id
            except (OSError, ValueError, KeyError, TypeError):
                receipt.update(critical_failure="test-tag-metadata-unavailable", expiry_failed_tag=reference)
                raise
        if not reused:
            if tag_from:
                command(["docker", "tag", built, reference], "tag", reference, config)
            output = command(["docker", "push", reference], "push", reference, config)
        inspected_reference = reference
        expiry = None
        if rehearsal:
            try:
                digest = existing["manifest_digest"] if reused else re.findall(
                    r"\bdigest:\s+(sha256:[a-f0-9]{64})\b", output)[-1]
                if not DIGEST.fullmatch(digest):
                    raise ValueError("invalid manifest digest")
                expiry = rehearsal_quay.verify_expiry(reference, digest, created=not reused)
            except (ValueError, IndexError, KeyError, TypeError):
                receipt.update(critical_failure="test-tag-expiry-unverified", expiry_failed_tag=reference)
                raise ValueError("test tag expiry could not be confirmed") from None
            inspected_reference = reference.rsplit(":", 1)[0] + "@" + digest
        if verify:
            command(["docker", "pull", inspected_reference], "verify-pull", reference, config)
            raw = command(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", inspected_reference], "verify-digest", reference, config)
            entries = json.loads(raw)
            repository = repository.removeprefix("docker.io/")
            observed_digest = next(value.split("@", 1)[1] for value in entries
                                   if value.split("@", 1)[0].removeprefix("docker.io/") == repository
                                   and (not rehearsal or value.split("@", 1)[1] == digest))
            observed_id = command(["docker", "image", "inspect", "--format", "{{.Id}}", inspected_reference], "verify-image-id", reference, config).strip()
            if not DIGEST.fullmatch(observed_digest) or observed_id != expected_id:
                receipt["attempts"][-1].update(state="error", reason="published-image-identity-mismatch",
                                                expected_image_id=expected_id, observed_image_id=observed_id,
                                                observed_digest=observed_digest)
                write(receipt_path, receipt)
                raise RuntimeError("published image identity verification failed")
            result = {"registry": registry, "reference": reference,
                      "tag": reference.rsplit(":", 1)[1], "digest": observed_digest,
                      "image_id": observed_id, "verified_at": stamp()}
            if rehearsal:
                result["expires_at"] = expiry
                result["reused"] = reused
            receipt["references"].append(result)
            write(receipt_path, receipt)

    try:
        if not expected_id or not DIGEST.fullmatch(expected_id):
            raise RuntimeError("unable to resolve built image ID")
        if policy == "fatal" and any(scan_data.get(target, {}).get("state") != "ok" for target in ("image", "base", "sbom", "trivy")):
            receipt["critical_failure"] = "fatal-scan-policy-failed"
            raise RuntimeError("fatal scan policy rejected unavailable/failed evidence")
        if rehearsal:
            if build_input_error:
                receipt["critical_failure"] = build_input_error
                raise ValueError("rehearsal build input proof is missing")
            stream = os.environ.get("CI_REHEARSAL_STREAM", "")
            if stream not in ("z", "rc", "release"):
                receipt["critical_failure"] = "invalid-rehearsal-stream"
                raise ValueError("invalid rehearsal stream")
            aliases = list(other_tags)
            if stream == "rc" and args.image not in ("opflex-build-base", "opflex-build"):
                rc_number = os.environ.get("CI_REHEARSAL_RC_NUMBER", "")
                if not re.fullmatch(r"[1-9][0-9]*", rc_number):
                    raise ValueError("rehearsal RC requires a positive number")
                aliases.append(f"{args.tag}.rc{rc_number}")
            aliases.append(args.z_tag)
            for tag in dict.fromkeys([args.tag, *aliases]):
                push(f"{args.registry}/{args.image}:{tag}", "rehearsal", tag_from=tag != args.tag)
            receipt["state"] = "complete"
            return 0
        push(built, "noirolabs", tag_from=False)
        for tag in other_tags:
            push(f"quay.io/noirolabs/{args.image}:{tag}", "noirolabs")
        if args.image == "opflex-build":
            # Preserve the runtime build-image visibility boundary.
            push(f"quay.io/noirolabs/{args.image}:{args.z_tag}", "noirolabs")
            receipt["state"] = "complete"
            return 0
        for tag in (args.dated_tag,):
            push(f"quay.io/noiro/{args.image}:{tag}", "quay", args.quay_config)
            push(f"docker.io/noiro/{args.image}:{tag}", "docker")
        # No mutable alias is pushed until all immutable destinations verify.
        push(f"quay.io/noiro/{args.image}:{args.z_tag}", "quay", args.quay_config)
        push(f"docker.io/noiro/{args.image}:{args.z_tag}", "docker")
        # Downstream builds observe noirolabs only after every replica verifies.
        push(f"quay.io/noirolabs/{args.image}:{args.z_tag}", "noirolabs")
        receipt["state"] = "complete"
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, StopIteration):
        pushed = any(item["phase"] == "push" for item in receipt["attempts"])
        receipt["state"] = "partial" if receipt["references"] or (rehearsal and pushed) else "failed"
        receipt["critical_failure"] = receipt.get("critical_failure") or "image-publication-failed"
        return 1
    finally:
        receipt["finished_at"] = stamp()
        write(receipt_path, receipt)
        # A failed image still has publishable scan and publication evidence.
        if args.image not in ("opflex-build", "opflex-build-base"):
            row = {"registry": args.registry if rehearsal else "quay.io/noiro", "image": args.image, "tag": args.tag,
                   "other_tags": other_tags, "sha": expected_id,
                   "base_image": scan_data.get("base", {}).get("provenance", {}).get("reference", args.base_image),
                   "image_z_tag": args.z_tag, "dated_tag": args.dated_tag,
                   "scan": scan_data, "scan_policy": policy, "publication": receipt,
                   "build_inputs": build_inputs, "build_input_error": build_input_error,
                   "reports": {kind: f"reports/{args.image}/{kind}.txt" for kind in ("sbom", "cve", "cve-base")}}
            for kind in ("scan", "trivy"):
                if (report_dir / (kind + ".json")).exists():
                    row["reports"][kind] = f"reports/{args.image}/{kind}.json"
            for target, filename in (("image", "cve"), ("base", "cve-base")):
                if (report_dir / (filename + ".json")).exists():
                    row["reports"]["grype-" + target] = f"reports/{args.image}/{filename}.json"
            if build_inputs:
                if args.image == "opflex":
                    row["reports"]["build-input"] = "opflex-build-base-input.txt"
                    row["reports"]["build-input-provenance"] = "base-provenance/opflex-build.json"
                else:
                    row["reports"]["build-input-provenance"] = "base-provenance/aci-iptables-base.json"
            metadata = directory / "image-metadata.jsonl"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            with metadata.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("artifacts", "registry", "image", "tag", "other-tags", "base-image", "z-tag", "dated-tag", "quay-config"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    sys.exit(publish(args))


if __name__ == "__main__":
    main()
