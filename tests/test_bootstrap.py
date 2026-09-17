"""Offline bootstrap safeguards; these mocks do not execute Kubernetes Jobs."""

import importlib.util
import base64
import copy
import json
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml


SPEC = importlib.util.spec_from_file_location(
    "layer1_bootstrap", Path(__file__).resolve().parents[1] / "scripts" / "run_layer1.py",
)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


@pytest.fixture
def kube(tmp_path):
    return bootstrap.Kubernetes(tmp_path / "kubeconfig", "kind-test")


def fake_terminal_job(kube, logs, *, terminal="Complete", owner="job-uid", pod_count=1, exit_code=None):
    job = {"metadata": {"uid": "job-uid"}, "status": {
        "conditions": [{"type": terminal, "status": "True"}],
    }}
    pod = {"metadata": {"name": "consumer-pod", "uid": "pod-uid", "ownerReferences": [{"uid": owner}]},
           "status": {"containerStatuses": [{"imageID": "fixture-image", "state": {"terminated": {
               "exitCode": (0 if terminal == "Complete" else 1) if exit_code is None else exit_code,
           }}}]}}
    kube.call = Mock(side_effect=[json.dumps(job), json.dumps({"items": [pod] * pod_count}), logs])


@pytest.mark.parametrize("returncode", [0, 1])
def test_secret_document_is_stdin_and_command_errors_withhold_sensitive_output(kube, monkeypatch, returncode):
    token = "hf_dummy_secret_do_not_log"
    document = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "fixture"},
                "stringData": {"token": token}}
    run = Mock(return_value=SimpleNamespace(returncode=returncode, stdout="secret/fixture\n", stderr=token))
    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    if returncode:
        with pytest.raises(RuntimeError, match="output withheld") as caught:
            kube.call(["create", "-f", "-"], document)
        assert token not in str(caught.value)
    else:
        assert kube.call(["create", "-f", "-"], document) == "secret/fixture\n"
    assert json.loads(run.call_args.kwargs["input"]) == document
    assert token not in " ".join(run.call_args.args[0])
    assert run.call_args.args[0][-3:] == ["create", "-f", "-"]


def test_template_removes_dry_run_namespace_before_targeted_creation(kube):
    document = {"apiVersion": "v1", "kind": "Secret",
                "metadata": {"name": "model-key", "namespace": "default"}}
    kube.call = Mock(side_effect=[json.dumps(document), "secret/model-key\n"])
    resource = kube.template("model-key-secret.yaml")
    assert resource == {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "model-key"}}
    kube.create(resource, "model-demo-l1-fixture")
    assert kube.call.call_args.args[0][-2:] == ["-n", "model-demo-l1-fixture"]
    assert kube.call.call_args.args[1] == resource


@pytest.mark.parametrize("problem", ["two-pods", "wrong-owner"])
def test_ambiguous_or_unowned_pod_is_rejected_before_logs(kube, tmp_path, problem):
    fake_terminal_job(kube, '{"status":"loaded"}',
                      pod_count=2 if problem == "two-pods" else 1,
                      owner="other-job" if problem == "wrong-owner" else "job-uid")
    with pytest.raises(RuntimeError):
        kube.wait_job("namespace", "consumer", tmp_path, ())
    assert kube.call.call_count == 2
    assert list(tmp_path.iterdir()) == []


def test_sensitive_logs_are_never_saved(kube, tmp_path):
    token = "hf_dummy_secret_do_not_log"
    fake_terminal_job(kube, f"unexpected secret: {token}\n")
    with pytest.raises(RuntimeError, match="logs withheld") as caught:
        kube.wait_job("namespace", "consumer", tmp_path, (token,))
    assert token not in str(caught.value)
    assert list(tmp_path.iterdir()) == []


def test_complete_owned_job_has_one_report_and_saved_log(kube, tmp_path):
    logs = '{"status":"loaded","revision":"fixture-revision"}\n'
    fake_terminal_job(kube, logs)
    result = kube.wait_job("namespace", "consumer", tmp_path, ())
    assert result["condition"] == "Complete" and result["exit_code"] == 0
    assert result["pod_uid"] == "pod-uid"
    assert result["report"] == {"status": "loaded", "revision": "fixture-revision"}
    assert (tmp_path / "consumer.log").read_text() == logs


@pytest.mark.parametrize("problem", [None, "different-error", "loaded-report"])
def test_wrong_key_check_requires_authentication_failure_without_load(kube, tmp_path, problem):
    logs = "error: Artifact authentication failed: wrong key or modified artifact.\n"
    if problem == "different-error":
        logs = "error: network unavailable\n"
    elif problem == "loaded-report":
        logs += '{"status":"loaded"}\n'
    fake_terminal_job(kube, logs, terminal="Failed")
    if problem is None:
        result = kube.wait_job("namespace", "consumer-wrong-key", tmp_path, (), success=False)
        assert result["condition"] == "Failed" and result["exit_code"] == 1
        assert result["report"] is None
    else:
        with pytest.raises(RuntimeError, match="did not fail at the expected check"):
            kube.wait_job("namespace", "consumer-wrong-key", tmp_path, (), success=False)


@pytest.mark.parametrize("layer", [1, 2])
def test_producer_cancellation_stops_job_before_removing_publisher_secret(tmp_path, monkeypatch, layer):
    fake = Mock(spec=bootstrap.Kubernetes)
    fake.call.return_value = ""
    fake.template.side_effect = lambda name: {"metadata": {"name": name}, "data": {}}
    cancellation = KeyboardInterrupt("fixture cancellation")
    fake.wait_job.side_effect = cancellation
    monkeypatch.setattr(bootstrap, "Kubernetes", Mock(return_value=fake))
    monkeypatch.setattr(bootstrap, "get_token", lambda: "hf_dummy_fixture_token")
    api = Mock()
    api.whoami.return_value = {"name": "test-owner"}
    monkeypatch.setattr(bootstrap, "HfApi", Mock(return_value=api))
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path / "project")
    monkeypatch.setattr(bootstrap.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(bootstrap.os, "umask", Mock())
    arguments = SimpleNamespace(
        repo="test-owner/encrypted-demo", namespace=f"model-demo-l{layer}-cancel", layer=layer,
        kubeconfig=tmp_path / "kubeconfig", context="kind-test", verify_wrong_key=False,
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        bootstrap.run(arguments)
    assert caught.value is cancellation
    deletions = [call.args[0] for call in fake.call.call_args_list if call.args[0][0] == "delete"]
    assert len(deletions) == 2
    assert deletions[0][:3] == ["delete", "job", "producer"]
    assert "--cascade=foreground" in deletions[0] and "--wait=true" in deletions[0]
    assert deletions[1][:3] == ["delete", "secret", "hf-publisher-token"]
    assert ("signing-key" in deletions[1]) == (layer == 2)
    fake.wait_job.assert_called_once()
    assert fake.wait_job.call_args.args[1] == "producer"
    assert (tmp_path / f"home/.config/confidential-ml-distribution/runs/model-demo-l{layer}-cancel/model.key").is_file()


@pytest.mark.parametrize("terminal,exit_code", [("Failed", 0), ("Complete", 1)])
def test_terminal_condition_requires_matching_container_exit(kube, tmp_path, terminal, exit_code):
    success = terminal == "Complete"
    logs = '{"status":"loaded"}\n' if success else "Artifact authentication failed\n"
    fake_terminal_job(kube, logs, terminal=terminal, exit_code=exit_code)
    with pytest.raises(RuntimeError, match="exit code"):
        kube.wait_job("namespace", "consumer", tmp_path, (), success=success)


@pytest.mark.parametrize("problem", [None, "different-error", "loaded-report"])
def test_wrong_public_key_requires_signature_failure_without_success(kube, tmp_path, problem):
    logs = "error: Signature verification failed: wrong public key or modified artifact/signature.\n"
    if problem == "different-error":
        logs = "error: Artifact authentication failed\n"
    elif problem == "loaded-report":
        logs += '{"status":"loaded"}\n'
    fake_terminal_job(kube, logs, terminal="Failed")
    if problem is None:
        result = kube.wait_job("namespace", "consumer", tmp_path, (), success=False,
                               expected_error="Signature verification failed")
        assert result["exit_code"] == 1 and result["report"] is None
    else:
        with pytest.raises(RuntimeError, match="did not fail at the expected check"):
            kube.wait_job("namespace", "consumer", tmp_path, (), success=False,
                          expected_error="Signature verification failed")


@pytest.fixture
def isolated_bootstrap(tmp_path, monkeypatch):
    """Use checked-in templates and real ephemeral keys, but no kubectl or Hub calls."""
    kubernetes_type = bootstrap.Kubernetes
    manifest_directory = bootstrap.ROOT / "k8s"
    fake = Mock(spec=kubernetes_type)
    events = []
    created = []

    def call(arguments, document=None):
        events.append(("call", copy.deepcopy(arguments)))
        return ""

    def create(resource, namespace=None):
        created.append((copy.deepcopy(resource), namespace))
        events.append(("create", resource["kind"], resource["metadata"]["name"]))

    def wait(namespace, name, record_directory, sensitive, **kwargs):
        events.append(("wait", name))
        if kwargs.get("success", True) is False:
            return {"condition": "Failed", "exit_code": 1, "report": None}
        report = {"repo_id": "test-owner/encrypted-demo", "revision": "a" * 40}
        if name == "producer":
            report.update(status="published", signature_filename="model.cml.sig")
        else:
            report.update(status="loaded", signature_verified=True)
        return {"condition": "Complete", "exit_code": 0, "report": report}

    fake.call.side_effect = call
    fake.create.side_effect = create
    # PyYAML is already pinned transitively in uv.lock; no new dependency is needed.
    fake.template.side_effect = lambda name: yaml.safe_load((manifest_directory / name).read_text())
    fake.secret.side_effect = lambda *args, **kwargs: kubernetes_type.secret(fake, *args, **kwargs)
    fake.verification_key.side_effect = lambda *args, **kwargs: kubernetes_type.verification_key(fake, *args, **kwargs)
    fake.wait_job.side_effect = wait
    monkeypatch.setattr(bootstrap, "Kubernetes", Mock(return_value=fake))
    monkeypatch.setattr(bootstrap, "get_token", lambda: "hf_dummy_fixture_token")
    api = Mock()
    api.whoami.return_value = {"name": "test-owner"}
    monkeypatch.setattr(bootstrap, "HfApi", Mock(return_value=api))
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path / "project")
    monkeypatch.setattr(bootstrap.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(bootstrap.os, "umask", Mock())
    args = SimpleNamespace(
        repo="test-owner/encrypted-demo", namespace="model-demo-l2-fixture", layer=2,
        kubeconfig=tmp_path / "kubeconfig", context="kind-test", verify_wrong_key=False,
        verify_wrong_public_key=False,
    )
    return SimpleNamespace(kube=fake, args=args, created=created, events=events,
                           wait=wait, root=tmp_path, api=api)


def test_layer2_provisions_isolated_key_roles_and_negative_public_key(isolated_bootstrap, capsys):
    fixture = isolated_bootstrap
    fixture.args.verify_wrong_key = True
    fixture.args.verify_wrong_public_key = True
    result = bootstrap.run(fixture.args)
    resources = {resource["metadata"]["name"]: resource for resource, _ in fixture.created}
    private = resources["signing-key"]
    public = resources["producer-verification-key"]
    assert private["kind"] == "Secret" and private["immutable"] is True
    assert public["kind"] == "ConfigMap" and public["immutable"] is True
    assert set(private["data"]) == {"private.pem"}
    assert set(public["data"]) == {"public.pem"}
    private_pem = base64.b64decode(private["data"]["private.pem"])
    key = bootstrap.serialization.load_pem_private_key(private_pem, password=None)
    assert key.public_key().public_bytes(
        bootstrap.serialization.Encoding.PEM, bootstrap.serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode() == public["data"]["public.pem"]
    assert resources["wrong-producer-verification-key"]["data"] != public["data"]
    private_path = fixture.root / "home/.config/confidential-ml-distribution/runs/model-demo-l2-fixture/producer-signing.pem"
    assert private_path.read_bytes() == private_pem
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
    records = fixture.root / "project/runtime/model-demo-l2-fixture"
    assert (records / "producer-verification.public.pem").read_text() == public["data"]["public.pem"]

    producer = resources["producer"]["spec"]["template"]["spec"]
    consumer = resources["consumer"]["spec"]["template"]["spec"]
    assert producer["containers"][0]["image"] == "model-producer:layer2"
    assert "--signing-key-file" in producer["containers"][0]["args"]
    assert {v["name"] for v in producer["volumes"]} == {"work", "model-key", "publisher-token", "signing-key"}
    assert consumer["containers"][0]["image"] == "model-consumer:layer2"
    assert "--require-signature" in consumer["containers"][0]["args"]
    assert "--public-key-file" in consumer["containers"][0]["args"]
    assert {v["name"] for v in consumer["volumes"]} == {"work", "model-key", "verification-key"}
    verification_volume = next(v for v in consumer["volumes"] if v["name"] == "verification-key")
    assert verification_volume["configMap"]["name"] == "producer-verification-key"
    assert verification_volume["configMap"]["defaultMode"] == 0o440
    for name in ("producer", "consumer"):
        pod = resources[name]["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsUser"] == pod["securityContext"]["fsGroup"] == 10001
        assert all(mount.get("readOnly") for mount in pod["containers"][0]["volumeMounts"] if mount["name"] != "work")
    wrong_pod = resources["consumer-wrong-public-key"]["spec"]["template"]["spec"]
    assert "--require-signature" in wrong_pod["containers"][0]["args"]
    assert next(v for v in wrong_pod["volumes"] if v["name"] == "verification-key")["configMap"]["name"] == "wrong-producer-verification-key"
    assert next(v for v in wrong_pod["volumes"] if v["name"] == "model-key")["secret"]["secretName"] == "model-key"
    assert result["layer"] == 2 and result["wrong_public_key"]["exit_code"] == 1
    assert result["wrong_key"]["exit_code"] == 1
    assert fixture.kube.wait_job.call_args.kwargs["expected_error"] == "Signature verification failed"
    cleanup = ("call", ["delete", "secret", "hf-publisher-token", "signing-key", "-n",
                         "model-demo-l2-fixture", "--ignore-not-found"])
    assert fixture.events.index(cleanup) < fixture.events.index(("create", "Job", "consumer"))
    guarded = fixture.kube.wait_job.call_args.args[3]
    recorded_output = capsys.readouterr().out + (records / "result.json").read_text()
    for secret in (private_pem.decode(), base64.b64encode(private_pem).decode(), key.private_bytes_raw().hex()):
        assert secret in guarded
        assert secret not in recorded_output


def test_default_layer1_does_not_provision_signing_material(isolated_bootstrap):
    fixture = isolated_bootstrap
    del fixture.args.layer
    del fixture.args.verify_wrong_public_key
    fixture.args.namespace = "model-demo-l1-fixture"
    result = bootstrap.run(fixture.args)
    resources = {resource["metadata"]["name"]: resource for resource, _ in fixture.created}
    assert result["layer"] == 1
    assert "signing-key" not in resources and "producer-verification-key" not in resources
    assert resources["producer"]["spec"]["template"]["spec"]["containers"][0]["image"] == "model-producer:layer1"
    consumer = resources["consumer"]["spec"]["template"]["spec"]
    assert "--require-signature" not in consumer["containers"][0]["args"]
    assert not list(fixture.root.rglob("*.pem"))


@pytest.mark.parametrize("missing", ["producer", "consumer"])
def test_layer2_requires_signed_publication_and_verified_consumer_report(isolated_bootstrap, missing):
    fixture = isolated_bootstrap

    def wait(*args, **kwargs):
        result = fixture.wait(*args, **kwargs)
        if args[1] == missing:
            result["report"].pop("signature_filename" if missing == "producer" else "signature_verified")
        return result

    fixture.kube.wait_job.side_effect = wait
    with pytest.raises(RuntimeError, match="Layer 2"):
        bootstrap.run(fixture.args)
    if missing == "producer":
        assert not any(resource["metadata"]["name"] == "consumer" for resource, _ in fixture.created)


def test_wrong_public_key_option_is_rejected_for_layer1_before_external_calls(isolated_bootstrap):
    fixture = isolated_bootstrap
    fixture.args.layer = 1
    fixture.args.verify_wrong_public_key = True
    with pytest.raises(ValueError, match="requires --layer 2"):
        bootstrap.run(fixture.args)
    fixture.kube.call.assert_not_called()
    fixture.api.whoami.assert_not_called()
