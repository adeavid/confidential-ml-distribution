"""Offline workload orchestration checks; no Kubernetes or Hub integration claims."""

import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from huggingface_hub.errors import HfHubHTTPError
import pytest

import artifact
import consumer
import hub_artifact
from model_demo import SOURCE_FILES, SOURCE_REVISION
import producer


REPO = "test-owner/encrypted-demo"
REVISION = "a" * 40


def make_source(destination):
    destination.mkdir()
    for name in SOURCE_FILES:
        (destination / name).write_bytes(f"fixture: {name}".encode())


@pytest.fixture
def workspace(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    key_path = tmp_path / "mounted.key"
    key_path.write_bytes(os.urandom(32))
    key_path.chmod(0o440)
    return work, key_path


def test_producer_encrypts_with_mounted_key_and_publishes_only_artifact(workspace, monkeypatch, capsys):
    work, key_path = workspace
    key = key_path.read_bytes()
    captured = {}

    def publish(repo_id, path):
        assert repo_id == REPO
        assert path.is_relative_to(work)
        assert path.name == "model.cml"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert [child.name for child in path.parent.iterdir()] == ["model.cml"]
        captured["artifact"] = path.read_bytes()
        captured["package"] = artifact.decrypt_package(captured["artifact"], key)
        return {"repo_id": repo_id, "revision": REVISION, "filename": "model.cml"}

    download = Mock(side_effect=make_source)
    monkeypatch.setattr(producer, "download_source", download)
    monkeypatch.setattr(producer, "publish_artifact", publish)
    result = producer.run_producer(REPO, key_path, work)
    assert result == {
        "status": "published", "repo_id": REPO, "revision": REVISION,
        "filename": "model.cml", "artifact_bytes": len(captured["artifact"]),
        "source_revision": SOURCE_REVISION,
    }
    assert captured["package"].startswith(b"PK")
    download.assert_called_once()
    assert list(work.iterdir()) == []
    assert key_path.read_bytes() == key
    assert key.hex() not in json.dumps(result) + capsys.readouterr().out


@pytest.mark.parametrize("key_size", [0, 31, 33])
def test_invalid_producer_key_fails_before_network(workspace, monkeypatch, key_size):
    work, key_path = workspace
    key_path.chmod(0o600)
    key_path.write_bytes(b"k" * key_size)
    download, publish = Mock(), Mock()
    monkeypatch.setattr(producer, "download_source", download)
    monkeypatch.setattr(producer, "publish_artifact", publish)
    with pytest.raises(ValueError):
        producer.run_producer(REPO, key_path, work)
    download.assert_not_called()
    publish.assert_not_called()
    assert list(work.iterdir()) == []


def test_mounted_key_mode_uses_fresh_nonce_for_each_encryption():
    key = os.urandom(32)
    first, first_key = artifact.encrypt_package(b"tiny package", key=key)
    second, second_key = artifact.encrypt_package(b"tiny package", key=key)
    assert first_key == second_key == key
    assert first[len(artifact.MAGIC):artifact.HEADER_BYTES] != second[len(artifact.MAGIC):artifact.HEADER_BYTES]
    assert artifact.decrypt_package(first, key) == artifact.decrypt_package(second, key) == b"tiny package"


def test_producer_publication_failure_cleans_plaintext(workspace, monkeypatch):
    work, key_path = workspace
    key = key_path.read_bytes()
    monkeypatch.setattr(producer, "download_source", make_source)
    monkeypatch.setattr(producer, "publish_artifact", Mock(side_effect=OSError("fixture publication failed")))
    with pytest.raises(OSError, match="fixture publication failed"):
        producer.run_producer(REPO, key_path, work)
    assert list(work.iterdir()) == []
    assert key_path.read_bytes() == key


@pytest.fixture
def remote_artifact(tmp_path, workspace):
    source = tmp_path / "fixture-source"
    make_source(source)
    _, key_path = workspace
    encrypted, _ = artifact.encrypt_package(artifact.pack_model(source), key=key_path.read_bytes())
    return encrypted


def test_consumer_loads_only_decrypted_folder_then_cleans_up(workspace, remote_artifact, monkeypatch):
    work, key_path = workspace
    steps = []
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)

    def download(repo_id, revision, destination):
        assert (repo_id, revision) == (REPO, REVISION)
        assert "HF_HUB_OFFLINE" not in os.environ
        destination.write_bytes(remote_artifact)
        steps.append("download")
        return {"repo_id": repo_id, "revision": revision, "filename": "model.cml"}

    def load(model_path, cache):
        assert steps == ["download"]
        assert model_path.is_relative_to(work)
        assert cache.is_relative_to(work) and not cache.exists()
        assert {path.name for path in model_path.iterdir()} == set(artifact.PACKAGE_FILES)
        for name in SOURCE_FILES:
            assert (model_path / name).read_bytes() == f"fixture: {name}".encode()
        steps.append("load")
        return {"device": "cpu", "finite_output": True}

    monkeypatch.setattr(consumer, "download_artifact", download)
    monkeypatch.setattr(consumer, "_load_offline", load)
    result = consumer.run_consumer(REPO, REVISION, key_path, work)
    assert result == {"status": "loaded", "repo_id": REPO, "revision": REVISION,
                      "model": {"device": "cpu", "finite_output": True}}
    assert steps == ["download", "load"]
    assert list(work.iterdir()) == []
    assert "HF_HUB_OFFLINE" not in os.environ


def test_consumer_authentication_failure_never_loads(workspace, remote_artifact, monkeypatch):
    work, key_path = workspace
    corrupted = remote_artifact[:-1] + bytes([remote_artifact[-1] ^ 1])

    def download(repo_id, revision, destination):
        destination.write_bytes(corrupted)

    load = Mock(side_effect=AssertionError("Unauthenticated data reached model loading"))
    monkeypatch.setattr(consumer, "download_artifact", download)
    monkeypatch.setattr(consumer, "_load_offline", load)
    with pytest.raises(ValueError, match="authentication"):
        consumer.run_consumer(REPO, REVISION, key_path, work)
    load.assert_not_called()
    assert list(work.iterdir()) == []


def test_offline_loader_uses_child_only_environment_and_fresh_cache(tmp_path, monkeypatch):
    model_path = tmp_path / "model"
    model_path.mkdir()
    cache = tmp_path / "local-cache"
    token_names = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_TOKEN_PATH")
    for name in token_names:
        monkeypatch.setenv(name, "test-value-never-forward")
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
    expected = {"device": "cpu", "finite_output": True}
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(expected), stderr=""))
    monkeypatch.setattr(consumer.subprocess, "run", run)
    assert consumer._load_offline(model_path, cache) == expected
    arguments, options = run.call_args.args[0], run.call_args.kwargs
    assert arguments[0] == sys.executable
    assert Path(arguments[1]).name == "model_demo.py"
    assert arguments[2:] == ["load", str(model_path)]
    assert options["capture_output"] is True and options["text"] is True
    child = options["env"]
    assert child["HF_HUB_OFFLINE"] == child["TRANSFORMERS_OFFLINE"] == "1"
    assert all(name not in child for name in token_names)
    for name in ("HF_HOME", "HF_HUB_CACHE", "TORCH_HOME"):
        assert Path(child[name]).is_relative_to(cache)
    assert os.environ["HF_HUB_OFFLINE"] == os.environ["TRANSFORMERS_OFFLINE"] == "0"
    assert all(os.environ[name] == "test-value-never-forward" for name in token_names)


@pytest.mark.parametrize("returncode, stdout", [(1, ""), (0, "not JSON")])
def test_offline_loader_propagates_failed_or_invalid_child_result(tmp_path, monkeypatch, returncode, stdout):
    run = Mock(return_value=SimpleNamespace(returncode=returncode, stdout=stdout, stderr="fixture failure"))
    monkeypatch.setattr(consumer.subprocess, "run", run)
    with pytest.raises((ValueError, RuntimeError)):
        consumer._load_offline(tmp_path / "model", tmp_path / "local-cache")


@pytest.mark.parametrize(
    "module, operation, arguments",
    [
        (producer, "run_producer", ["--repo", REPO]),
        (consumer, "run_consumer", ["--repo", REPO, "--revision", REVISION]),
        (hub_artifact, "publish_artifact", ["publish", "--repo", REPO, "--artifact", "fixture.cml"]),
    ],
)
def test_cli_hub_error_with_oserror_inheritance_withholds_remote_message(
    monkeypatch, capsys, module, operation, arguments,
):
    marker = "REMOTE_ERROR_MUST_NOT_EXPOSE_THIS_TOKEN"
    response = httpx.Response(403, request=httpx.Request("GET", "https://example.invalid/model"))
    error = HfHubHTTPError(marker, response=response)
    assert isinstance(error, OSError)  # Broad local-I/O handlers must not intercept this.
    failed = Mock(side_effect=error)
    monkeypatch.setattr(module, operation, failed)
    monkeypatch.setattr(sys, "argv", [module.__name__, *arguments])
    with pytest.raises(SystemExit) as caught:
        module.main()
    failed.assert_called_once()
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err
    assert "request failed" in captured.err
