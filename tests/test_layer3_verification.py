"""Fixture checks for the read-only verifier; no kubectl or attestation is executed."""

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


SPEC = importlib.util.spec_from_file_location(
    "verify_consumer", Path(__file__).resolve().parents[1] / "scripts/layer3/verify-consumer.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)
REAL_KUBECTL = verifier.kubectl


@pytest.fixture
def records(monkeypatch):
    args = argparse.Namespace(job="consumer-l3-fixture", revision="a" * 40,
                              image="example.invalid/consumer@sha256:" + "b" * 64, expect="allowed", signed=False)
    job_uid = "11111111-1111-4111-8111-111111111111"
    job = {"metadata": {"name": args.job, "namespace": verifier.NAMESPACE, "uid": job_uid},
           "status": {"conditions": [{"type": "Complete", "status": "True"}]}}
    pod = {"metadata": {"name": args.job + "-abcde", "namespace": verifier.NAMESPACE,
                        "uid": "22222222-2222-4222-8222-222222222222",
                        "ownerReferences": [{"kind": "Job", "name": args.job, "uid": job_uid, "controller": True}]},
           "spec": {"runtimeClassName": "kata-qemu-coco-dev", "automountServiceAccountToken": False,
                    "volumes": [{"name": "work", "emptyDir": {}}],
                    "containers": [{"name": "consumer", "image": args.image,
                                    "args": ["--revision", args.revision, "--cdh-resource", "default/key/my-model"]}]},
           "status": {"containerStatuses": [{"name": "consumer", "imageID": args.image,
                                             "state": {"terminated": {"exitCode": 0}}}]}}
    report = {"status": "loaded", "revision": args.revision, "key_source": "cdh",
              "model": {"device": "cpu", "finite_output": True, "parameter_count": 4400000,
                        "input_tokens": 12, "output_shape": [1, 12, 30522]}}
    data = {"job": job, "pods": [pod], "report": report, "logs": None, "calls": []}

    def read(*arguments):
        data["calls"].append(arguments)
        if arguments[:2] == ("get", "job"):
            return json.dumps(job)
        if arguments[:2] == ("get", "pods"):
            return json.dumps({"items": data["pods"]})
        assert arguments[0] == "logs"
        return data["logs"] if data["logs"] is not None else json.dumps(report)

    monkeypatch.setattr(verifier, "kubectl", read)
    return args, data, pod


@pytest.mark.parametrize("signed", [False, True])
def test_allowed_report_is_verified_and_filtered(records, signed):
    args, data, pod = records
    args.signed = signed
    if signed:
        pod["spec"]["containers"][0]["args"].append("--require-signature")
        data["report"]["signature_verified"] = True
    data["report"]["extra"] = data["report"]["model"]["model_class"] = "SENSITIVE_MARKER"
    result = verifier.verify(args)
    assert result["signature_verified"] is signed
    assert result["exit_code"] == 0 and result["key_source"] == "cdh"
    assert "SENSITIVE_MARKER" not in json.dumps(result)
    assert all(call[0] in {"get", "logs"} for call in data["calls"])


@pytest.mark.parametrize("image_id", ["sha256:" + "c" * 64,
                                    "docker-pullable://example.invalid/consumer@sha256:" + "c" * 64])
def test_runtime_image_id_can_differ_from_pinned_manifest_digest(records, image_id):
    args, _, pod = records
    pod["status"]["containerStatuses"][0]["imageID"] = image_id
    assert image_id != args.image
    assert verifier.verify(args)["image_id"] == image_id


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_saturated_logs_are_rejected_before_newline_normalization(records, monkeypatch, newline):
    args, data, _ = records
    fixture_read, real_run = verifier.kubectl, subprocess.run

    def local_process(command, **options):
        assert command[0] == "kubectl" and "--limit-bytes=65537" in command
        # A real subprocess exercises Python's text-mode CRLF normalization.
        script = ("import sys; prefix=(sys.argv[1]+'\\n').encode(); "
                  "padding=sys.argv[2].encode()*65537; "
                  "sys.stdout.buffer.write((prefix+padding)[:65537])")
        return real_run([sys.executable, "-c", script, json.dumps(data["report"]), newline], **options)

    def read(*arguments):
        return REAL_KUBECTL(*arguments) if arguments[0] == "logs" else fixture_read(*arguments)

    monkeypatch.setattr(verifier.subprocess, "run", local_process)
    monkeypatch.setattr(verifier, "kubectl", read)
    with pytest.raises(RuntimeError, match="logs exceed"):
        verifier.verify(args)


@pytest.mark.parametrize("problem", ["old-owner", "two-pods", "projected-secret", "wrong-image", "signature-mode"])
def test_identity_or_configuration_failure_is_rejected_before_logs(records, problem):
    args, data, pod = records
    if problem == "old-owner":
        pod["metadata"]["ownerReferences"][0]["uid"] = "33333333-3333-4333-8333-333333333333"
    elif problem == "two-pods":
        data["pods"].append(pod)
    elif problem == "projected-secret":
        pod["spec"]["volumes"].append({"projected": {"sources": [{"secret": {"name": "key"}}]}})
    elif problem == "wrong-image":
        pod["spec"]["containers"][0]["image"] = "example.invalid/consumer:latest"
    else:
        args.signed = True
    with pytest.raises(RuntimeError):
        verifier.verify(args)
    assert all(call[0] != "logs" for call in data["calls"])


@pytest.mark.parametrize("problem", [None, "timeout", "missing-resource", "loaded", "image-pull"])
def test_denied_requires_cdh_http_error_and_no_load(records, problem):
    args, data, pod = records
    args.expect = "denied"
    data["job"]["status"]["conditions"][0]["type"] = "Failed"
    pod["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"] = 1
    data["logs"] = "error: CDH key retrieval failed (HTTP 500); no Secret fallback is permitted.\n"
    if problem == "timeout":
        data["logs"] = "error: CDH key request failed; no Secret fallback is permitted.\n"
    elif problem == "missing-resource":
        data["logs"] = data["logs"].replace("500", "404")
    elif problem == "loaded":
        data["logs"] += json.dumps(data["report"])
    elif problem == "image-pull":
        pod["status"]["containerStatuses"][0]["state"] = {"waiting": {"reason": "ImagePullBackOff"}}
    if problem is None:
        result = verifier.verify(args)
        assert result["exit_code"] == 1 and "model" not in result
    else:
        with pytest.raises(RuntimeError):
            verifier.verify(args)


def test_cli_withholds_subprocess_output(records, monkeypatch, capsys):
    args, _, _ = records
    marker = "SENSITIVE_KUBECTL_STDERR"

    def fail(*arguments):
        raise subprocess.CalledProcessError(1, "kubectl", output=marker, stderr=marker)

    monkeypatch.setattr(verifier, "kubectl", fail)
    monkeypatch.setattr(sys, "argv", ["verify-consumer", "--job", args.job, "--revision", args.revision,
                                     "--image", args.image, "--expect", "allowed"])
    with pytest.raises(SystemExit) as error:
        verifier.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "raw output withheld" in output.err
    assert marker not in output.out + output.err
