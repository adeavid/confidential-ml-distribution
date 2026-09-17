"""Offline orchestration checks; only cryptography is real, not CDH or Hub."""

from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

import artifact
import cdh
import consumer
from model_demo import SOURCE_FILES
import signing


REPO, REVISION, RESOURCE = "fixture/model", "a" * 40, "default/key/my-model"


@pytest.fixture
def model(tmp_path, monkeypatch):
    source, private, public, work = (tmp_path / name for name in ("source", "private", "public", "work"))
    for directory in (source, private, public, work):
        directory.mkdir()
    for name in SOURCE_FILES:
        (source / name).write_bytes(f"fixture: {name}".encode())
    encrypted, key = artifact.encrypt_package(artifact.pack_model(source))
    ciphertext, signature = public / "model.cml", public / "model.cml.sig"
    ciphertext.write_bytes(encrypted)
    signing.generate_key_pair(private / "sign.pem", public / "verify.pem")
    signing.sign_artifact(ciphertext, private / "sign.pem", signature)

    def download(repo, revision, destination):
        assert (repo, revision) == (REPO, REVISION)
        destination.write_bytes(encrypted)

    def download_signature(repo, revision, destination):
        assert (repo, revision) == (REPO, REVISION)
        destination.write_bytes(signature.read_bytes())

    monkeypatch.setattr(consumer, "download_artifact", download)
    monkeypatch.setattr(consumer, "download_signature", download_signature)
    return SimpleNamespace(work=work, encrypted=encrypted, key=key,
                           public_key=public / "verify.pem", signature=signature)


@pytest.mark.parametrize("signed", [False, True])
def test_cdh_loads_authenticated_folder_and_preserves_verified_buffer(model, monkeypatch, signed):
    steps = []
    real_decrypt = consumer.decrypt_package

    def verify(path, signature, public_key):
        verified = signing.verify_artifact(path, signature, public_key)
        path.write_bytes(b"replacement after verification")
        steps.append("verify")
        return verified

    def key(resource):
        assert resource == RESOURCE
        assert steps == (["verify"] if signed else [])
        steps.append("cdh")
        return model.key

    def decrypt(encrypted, key):
        assert encrypted == model.encrypted and steps[-1] == "cdh"
        steps.append("decrypt")
        return real_decrypt(encrypted, key)

    def load(directory, cache):
        assert steps[-1] == "decrypt"
        assert {p.name for p in directory.iterdir()} == set(artifact.PACKAGE_FILES)
        steps.append("load")
        return {"device": "cpu", "finite_output": True}

    monkeypatch.setattr(consumer, "verify_artifact", verify)
    monkeypatch.setattr(consumer, "read_cdh_key", key)
    monkeypatch.setattr(consumer, "decrypt_package", decrypt)
    monkeypatch.setattr(consumer, "_load_offline", load)
    monkeypatch.setattr(consumer, "decrypt_model", Mock(side_effect=AssertionError("File-key fallback")))
    report = consumer.run_consumer(REPO, REVISION, None, model.work, cdh_resource=RESOURCE,
                                   require_signature=signed, public_key_path=model.public_key if signed else None)
    assert steps == (["verify"] if signed else []) + ["cdh", "decrypt", "load"]
    assert report["key_source"] == "cdh" and report["status"] == "loaded"
    assert list(model.work.iterdir()) == []


@pytest.mark.parametrize("problem", ["missing", "invalid", "wrong-public-key"])
def test_signature_failure_never_requests_cdh_key(model, monkeypatch, problem):
    if problem == "missing":
        model.signature.unlink()
    elif problem == "invalid":
        model.signature.write_bytes(b"x" * 64)
    else:
        signing.generate_key_pair(model.work.parent / "private" / "other.pem", model.public_key.with_name("other.pem"))
        model.public_key = model.public_key.with_name("other.pem")
    guards = []
    for name in ("read_cdh_key", "_read_regular", "decrypt_package", "decrypt_model", "extract_package", "_load_offline"):
        guard = Mock(side_effect=AssertionError(f"Signature failure reached {name}"))
        monkeypatch.setattr(consumer, name, guard)
        guards.append(guard)
    with pytest.raises((ValueError, OSError)):
        consumer.run_consumer(REPO, REVISION, None, model.work, cdh_resource=RESOURCE,
                              require_signature=True, public_key_path=model.public_key)
    for guard in guards:
        guard.assert_not_called()
    assert list(model.work.iterdir()) == []


@pytest.mark.parametrize("problem", ["denied", "wrong-key"])
def test_cdh_failure_never_falls_back_or_loads(model, monkeypatch, problem):
    read = Mock(side_effect=ValueError("CDH key retrieval failed (HTTP 500)")) if problem == "denied" else Mock(return_value=b"x" * 32)
    monkeypatch.setattr(consumer, "read_cdh_key", read)
    guards = []
    for name in ("_read_regular", "decrypt_model", "extract_package", "_load_offline"):
        guard = Mock(side_effect=AssertionError(f"CDH failure reached {name}"))
        monkeypatch.setattr(consumer, name, guard)
        guards.append(guard)
    with pytest.raises(ValueError, match="CDH|authentication"):
        consumer.run_consumer(REPO, REVISION, None, model.work, cdh_resource=RESOURCE,
                              require_signature=True, public_key_path=model.public_key)
    read.assert_called_once_with(RESOURCE)
    for guard in guards:
        guard.assert_not_called()


@pytest.mark.parametrize("key,resource", [(Path("mounted.key"), RESOURCE), (None, None), (None, "default/../key")])
def test_invalid_key_source_configuration_fails_before_network(tmp_path, monkeypatch, key, resource):
    download = Mock()
    monkeypatch.setattr(consumer, "download_artifact", download)
    with pytest.raises(ValueError):
        consumer.run_consumer(REPO, REVISION, key, tmp_path, cdh_resource=resource)
    download.assert_not_called()


@pytest.mark.parametrize("flags,expected_key,expected_resource", [
    ([], Path("/run/secrets/model/key"), None),
    (["--key-file", "/mounted/key"], Path("/mounted/key"), None),
    (["--cdh-resource", RESOURCE], None, RESOURCE),
])
def test_cli_preserves_existing_file_defaults_and_selects_cdh(monkeypatch, flags, expected_key, expected_resource):
    run = Mock(return_value={"status": "loaded"})
    monkeypatch.setattr(consumer, "run_consumer", run)
    monkeypatch.setattr(sys, "argv", ["consumer", "--repo", REPO, "--revision", REVISION, *flags])
    consumer.main()
    assert run.call_args.args[2] == expected_key
    assert run.call_args.kwargs["cdh_resource"] == expected_resource


def test_cli_rejects_both_sources(monkeypatch):
    run = Mock()
    monkeypatch.setattr(consumer, "run_consumer", run)
    monkeypatch.setattr(sys, "argv", ["consumer", "--repo", REPO, "--revision", REVISION,
                                     "--key-file", "/mounted/key", "--cdh-resource", RESOURCE])
    with pytest.raises(SystemExit) as error:
        consumer.main()
    assert error.value.code == 2
    run.assert_not_called()


def test_cli_cdh_transport_error_withholds_sensitive_message(model, monkeypatch, capsys):
    real_client = httpx.Client
    marker = "REMOTE_SECRET_MUST_NOT_APPEAR"

    def fail(request):
        raise httpx.ConnectError(marker, request=request)

    monkeypatch.setattr(cdh.httpx, "Client", lambda **options: real_client(
        transport=httpx.MockTransport(fail), **options))
    monkeypatch.setattr(sys, "argv", ["consumer", "--repo", REPO, "--revision", REVISION,
                                     "--work-dir", str(model.work), "--cdh-resource", RESOURCE])
    with pytest.raises(SystemExit) as error:
        consumer.main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert "CDH key request failed" in captured.err
    assert marker not in captured.out + captured.err and "Traceback" not in captured.err
