"""Rehearsal publication must verify each tag, including reused aliases."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "travis"))
import image_publication
import rehearsal_quay


DIGEST = "sha256:" + "a" * 64
IMAGE_ID = "sha256:" + "b" * 64
REF = "quay.io/anmrasto/opflex-build-base:0.0.123.1.a378a7d"
CONTEXT = {"CI_REHEARSAL": "1", "GITHUB_ACTIONS": "true", "CI_SCAN_POLICY": "advisory",
           "CI_REHEARSAL_STREAM": "z",
           "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REPOSITORY": "anmol372/opflex",
           "GITHUB_REF": rehearsal_quay.EXECUTION_REF}


class RehearsalExpiryTests(unittest.TestCase):
    def setUp(self):
        context = patch.dict(os.environ, CONTEXT)
        context.start()
        self.addCleanup(context.stop)

    def test_verify_new_alias_sets_only_expiry_and_ignores_history(self):
        old = {"name": REF.split(":")[1], "manifest_digest": "sha256:" + "c" * 64,
               "start_ts": 1, "end_ts": 2}
        new = {"name": old["name"], "manifest_digest": DIGEST, "start_ts": 10000}
        verified = dict(new, end_ts=31600, expiration="future")
        with patch.object(rehearsal_quay.time, "time", return_value=10001), \
                patch.object(rehearsal_quay, "request", side_effect=[
                    {"tags": [old, new]}, None, {"tags": [old, verified]}]) as request:
            self.assertEqual(rehearsal_quay.verify_expiry(REF, DIGEST, created=True), 31600)
        self.assertEqual(request.call_args_list[1].args[1], {"expiration": 31600})

    def test_new_tag_with_label_expiry_needs_only_a_read(self):
        row = {"name": REF.split(":")[1], "manifest_digest": DIGEST, "start_ts": 10000,
               "end_ts": 31600, "expiration": "future"}
        with patch.object(rehearsal_quay.time, "time", return_value=10001), \
                patch.object(rehearsal_quay, "request", return_value={"tags": [row]}) as request:
            self.assertEqual(rehearsal_quay.verify_expiry(REF, DIGEST, created=True), 31600)
        request.assert_called_once()
        self.assertEqual(len(request.call_args.args), 1)

    def test_expiry_of_existing_alias_is_verified_without_mutation(self):
        row = {"name": REF.split(":")[1], "manifest_digest": DIGEST, "start_ts": 10000,
               "end_ts": 11000, "expiration": "future"}
        with patch.object(rehearsal_quay.time, "time", return_value=10001), \
                patch.object(rehearsal_quay, "request", return_value={"tags": [row]}) as request:
            self.assertEqual(rehearsal_quay.verify_expiry(REF, DIGEST, created=False), 11000)
        request.assert_called_once()
        self.assertEqual(len(request.call_args.args), 1)

    def test_unverified_first_tag_stops_all_other_pushes(self):
        def docker(argv, **_):
            if argv[1] == "push":
                return argparse.Namespace(stdout=f"digest: {DIGEST} size: 20")
            self.fail(f"unexpected command after failed expiry: {argv}")

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(image_publication, "execute", return_value="6h"), \
                patch.object(image_publication, "collect", return_value={"image_id": IMAGE_ID}), \
                patch.object(image_publication.subprocess, "run", side_effect=docker) as run, \
                patch.object(rehearsal_quay, "current_tag", return_value=None), \
                patch.object(rehearsal_quay, "verify_expiry", side_effect=ValueError("bad expiry")):
            args = argparse.Namespace(artifacts=directory, registry=rehearsal_quay.REGISTRY,
                                      image="opflex-build-base", tag=REF.split(":")[1], other_tags="later",
                                      z_tag="z", timeout=1)
            self.assertEqual(image_publication.publish(args), 1)
            receipt = json.loads((Path(directory) / "publication/opflex-build-base.json").read_text())
            self.assertEqual(receipt["state"], "partial")
            self.assertEqual(receipt["expiry_failed_tag"], REF)
        run.assert_called_once()

    def test_reuses_current_image_and_replaces_different_image(self):
        old_digest = "sha256:" + "c" * 64
        old_id = "sha256:" + "d" * 64
        repo, tag = REF.rsplit(":", 1)

        for reused in (False, True):
            with self.subTest(reused=reused):
                active_digest = DIGEST if reused else old_digest

                def docker(argv, **_):
                    if argv[1] == "push":
                        return argparse.Namespace(stdout=f"digest: {DIGEST} size: 20")
                    if "{{.Id}}" in argv:
                        value = old_id if argv[-1].endswith(old_digest) else IMAGE_ID
                        return argparse.Namespace(stdout=value)
                    if "{{json .RepoDigests}}" in argv:
                        return argparse.Namespace(stdout=json.dumps([f"{repo}@{DIGEST}"]))
                    return argparse.Namespace(stdout="")

                with tempfile.TemporaryDirectory() as directory, \
                        patch.object(image_publication, "execute", return_value="6h"), \
                        patch.object(image_publication, "collect", return_value={"image_id": IMAGE_ID}), \
                        patch.object(image_publication.subprocess, "run", side_effect=docker) as run, \
                        patch.object(rehearsal_quay, "current_tag", side_effect=[
                            {"manifest_digest": active_digest}, None]), \
                        patch.object(rehearsal_quay, "verify_expiry", return_value=31600) as expiry:
                    args = argparse.Namespace(artifacts=directory, registry=rehearsal_quay.REGISTRY,
                                              image="opflex-build-base", tag=tag, other_tags="",
                                              z_tag="z", timeout=1)
                    self.assertEqual(image_publication.publish(args), 0)
                    self.assertEqual(expiry.call_args_list[0].args, (REF, DIGEST))
                    self.assertEqual(expiry.call_args_list[0].kwargs, {"created": not reused})
                    pushes = [call.args[0][2] for call in run.call_args_list if call.args[0][1] == "push"]
                    self.assertEqual(pushes, [f"{repo}:z"] if reused else [REF, f"{repo}:z"])

    def test_rc_alias_must_verify_before_rolling_alias_moves(self):
        image = "test-image"
        tag = REF.rsplit(":", 1)[1]
        repo = f"{rehearsal_quay.REGISTRY}/{image}"
        rc = f"{repo}:{tag}.rc2"

        def docker(argv, **_):
            output = {
                "push": f"digest: {DIGEST} size: 20",
                "{{json .RepoDigests}}": json.dumps([f"{repo}@{DIGEST}"]),
                "{{.Id}}": IMAGE_ID,
            }
            operation = argv[-2] if argv[1] == "image" else argv[1]
            return argparse.Namespace(stdout=output.get(operation, ""))

        def expiry(reference, *_args, **_kwargs):
            if reference == rc:
                raise ValueError("RC alias is missing expiration")
            return 31600

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"CI_REHEARSAL_STREAM": "rc", "CI_REHEARSAL_RC_NUMBER": "2"}), \
                patch.object(image_publication, "execute", return_value="6h"), \
                patch.object(image_publication, "collect", return_value={"image_id": IMAGE_ID}), \
                patch.object(image_publication.subprocess, "run", side_effect=docker) as run, \
                patch.object(rehearsal_quay, "current_tag", return_value=None), \
                patch.object(rehearsal_quay, "verify_expiry", side_effect=expiry):
            args = argparse.Namespace(artifacts=directory, registry=rehearsal_quay.REGISTRY, image=image,
                                      tag=tag, other_tags=f"{tag}.dated", z_tag=f"{tag}.z",
                                      base_image="ubi", dated_tag=f"{tag}.dated", timeout=1)
            self.assertEqual(image_publication.publish(args), 1)
            receipt = json.loads((Path(directory) / f"publication/{image}.json").read_text())
            self.assertEqual(receipt["expiry_failed_tag"], rc)
            pushes = [call.args[0][2] for call in run.call_args_list if call.args[0][1] == "push"]
            self.assertEqual(pushes, [f"{repo}:{tag}", f"{repo}:{tag}.dated", rc])


if __name__ == "__main__":
    unittest.main()
