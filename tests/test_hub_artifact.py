"""Offline protocol checks; mocked Hub calls do not prove Hub integration."""

from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from huggingface_hub.errors import HfHubHTTPError
import pytest

from artifact import MAX_ARTIFACT_BYTES, encrypt_package
import hub_artifact


REPO = "test-owner/encrypted-demo"
REVISION = "a" * 40
NEW_REVISION = "b" * 40


@pytest.fixture
def encrypted_file(tmp_path):
    encrypted, key = encrypt_package(b"tiny package")
    path = tmp_path / "model.cml"
    path.write_bytes(encrypted)
    (tmp_path / "private.key").write_bytes(key)
    return path


@pytest.fixture
def signature_file(tmp_path, encrypted_file):
    path = tmp_path / "model.cml.sig"
    path.write_bytes(Ed25519PrivateKey.generate().sign(encrypted_file.read_bytes()))
    return path


@pytest.fixture
def api(monkeypatch):
    client = Mock()
    client.model_info.return_value = SimpleNamespace(
        private=False, sha=REVISION,
        siblings=[SimpleNamespace(rfilename=".gitattributes")],
    )
    client.create_commit.return_value = SimpleNamespace(oid=NEW_REVISION)
    factory = Mock(return_value=client)
    monkeypatch.setattr(hub_artifact, "HfApi", factory)
    return client, factory


def test_publish_uploads_only_explicit_ciphertext_and_returns_commit(encrypted_file, api):
    client, _ = api
    result = hub_artifact.publish_artifact(REPO, encrypted_file)
    client.create_repo.assert_called_once_with(repo_id=REPO, repo_type="model", private=False, exist_ok=True)
    arguments = client.create_commit.call_args.kwargs
    assert arguments["repo_id"] == REPO and arguments["repo_type"] == "model"
    assert arguments["parent_commit"] == REVISION
    assert arguments["revision"] == "main" and arguments["create_pr"] is False
    assert len(arguments["operations"]) == 1
    operation = arguments["operations"][0]
    assert operation.path_in_repo == "model.cml"
    assert operation.path_or_fileobj == encrypted_file.read_bytes()
    assert result == {"repo_id": REPO, "revision": NEW_REVISION, "filename": "model.cml"}


def test_signed_publication_commits_both_exact_files_together(encrypted_file, signature_file, api):
    client, _ = api
    client.model_info.return_value.siblings += [
        SimpleNamespace(rfilename="model.cml"), SimpleNamespace(rfilename="model.cml.sig"),
    ]
    result = hub_artifact.publish_artifact(REPO, encrypted_file, signature_file)
    client.create_commit.assert_called_once()
    arguments = client.create_commit.call_args.kwargs
    assert arguments["parent_commit"] == REVISION
    operations = arguments["operations"]
    assert len(operations) == 2 and all(isinstance(op, hub_artifact.CommitOperationAdd) for op in operations)
    assert {op.path_in_repo: op.path_or_fileobj for op in operations} == {
        "model.cml": encrypted_file.read_bytes(), "model.cml.sig": signature_file.read_bytes(),
    }
    assert result == {"repo_id": REPO, "revision": NEW_REVISION, "filename": "model.cml",
                      "signature_filename": "model.cml.sig"}


@pytest.mark.parametrize("problem", ["missing", "short", "long"])
def test_invalid_signature_prevents_any_publication(encrypted_file, signature_file, api, problem):
    if problem == "missing":
        signature_file.unlink()
    else:
        signature_file.write_bytes(b"s" * (63 if problem == "short" else 65))
    with pytest.raises((ValueError, OSError)):
        hub_artifact.publish_artifact(REPO, encrypted_file, signature_file)
    api[1].assert_not_called()


def test_unsigned_publication_removes_stale_signature_in_same_commit(encrypted_file, api):
    client, _ = api
    client.model_info.return_value.siblings.append(SimpleNamespace(rfilename="model.cml.sig"))
    result = hub_artifact.publish_artifact(REPO, encrypted_file)
    client.create_commit.assert_called_once()
    operations = client.create_commit.call_args.kwargs["operations"]
    assert len(operations) == 2
    assert isinstance(operations[0], hub_artifact.CommitOperationAdd)
    assert operations[0].path_in_repo == "model.cml" and operations[0].path_or_fileobj == encrypted_file.read_bytes()
    assert isinstance(operations[1], hub_artifact.CommitOperationDelete)
    assert operations[1].path_in_repo == "model.cml.sig" and operations[1].is_folder is False
    assert result == {"repo_id": REPO, "revision": NEW_REVISION, "filename": "model.cml"}


@pytest.mark.parametrize("problem", ["private", "unexpected-file", "missing-files", "invalid-revision", "missing-revision"])
def test_publish_rejects_unsafe_existing_repository(encrypted_file, api, problem):
    client, _ = api
    info = client.model_info.return_value
    if problem == "private":
        info.private = True
    elif problem == "unexpected-file":
        info.siblings.append(SimpleNamespace(rfilename="private.key"))
    elif problem == "missing-files":
        info.siblings = None
    else:
        info.sha = "main" if problem == "invalid-revision" else None
    with pytest.raises(ValueError):
        hub_artifact.publish_artifact(REPO, encrypted_file)
    client.create_commit.assert_not_called()


@pytest.mark.parametrize("payload", [b"", b"invalid header" * 4])
def test_invalid_source_is_rejected_before_hub_access(encrypted_file, api, payload):
    encrypted_file.write_bytes(payload)
    with pytest.raises(ValueError):
        hub_artifact.publish_artifact(REPO, encrypted_file)
    api[1].assert_not_called()


def test_parent_commit_conflict_propagates_without_retry(encrypted_file, api):
    client, _ = api
    response = httpx.Response(409, request=httpx.Request("POST", "https://example.invalid/commit/main"))
    conflict = HfHubHTTPError("Parent commit changed", response=response)
    client.create_commit.side_effect = conflict
    with pytest.raises(HfHubHTTPError) as caught:
        hub_artifact.publish_artifact(REPO, encrypted_file)
    assert caught.value is conflict
    client.create_commit.assert_called_once()
    client.model_info.assert_called_once()
    assert client.create_commit.call_args.kwargs["parent_commit"] == REVISION


def mock_download(monkeypatch, source):
    payload = source.read_bytes()
    metadata = Mock(return_value=SimpleNamespace(size=len(payload), commit_hash=REVISION))
    url = Mock(return_value="https://example.invalid/pinned-model.cml")
    cache_paths = []

    def cached_download(**kwargs):
        cache = Path(kwargs["cache_dir"])
        cache.mkdir(parents=True, exist_ok=True)
        cache_paths.append(cache)
        cached = cache / kwargs["filename"]
        cached.write_bytes(source.read_bytes())
        return str(cached)

    download = Mock(side_effect=cached_download)
    monkeypatch.setattr(hub_artifact, "hf_hub_url", url)
    monkeypatch.setattr(hub_artifact, "get_hf_file_metadata", metadata)
    monkeypatch.setattr(hub_artifact, "hf_hub_download", download)
    return metadata, download, url, cache_paths


@pytest.fixture
def download_mocks(monkeypatch, encrypted_file):
    return mock_download(monkeypatch, encrypted_file)


@pytest.fixture
def signature_download_mocks(monkeypatch, signature_file):
    return mock_download(monkeypatch, signature_file)


def test_download_pins_anonymous_requests_and_removes_temporary_cache(tmp_path, encrypted_file, download_mocks):
    metadata, download, url, cache_paths = download_mocks
    destination = tmp_path / "downloaded.cml"
    result = hub_artifact.download_artifact(REPO, REVISION, destination)
    assert destination.read_bytes() == encrypted_file.read_bytes()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert result == {"repo_id": REPO, "revision": REVISION, "filename": "model.cml", "artifact_bytes": destination.stat().st_size}
    url.assert_called_once_with(REPO, filename="model.cml", revision=REVISION, repo_type="model")
    metadata.assert_called_once_with(url.return_value, token=False)
    arguments = download.call_args.kwargs
    assert {name: arguments[name] for name in ("repo_id", "filename", "revision", "repo_type", "token")} == {
        "repo_id": REPO, "filename": "model.cml", "revision": REVISION, "repo_type": "model", "token": False,
    }
    assert cache_paths and all(not path.exists() for path in cache_paths)


@pytest.mark.parametrize("parent", ["relative", "symlink"])
def test_download_accepts_relative_or_symlink_parent(tmp_path, encrypted_file, download_mocks, monkeypatch, parent):
    monkeypatch.chdir(tmp_path)
    destination = Path("downloaded.cml")
    if parent == "symlink":
        real_parent = tmp_path / "real-output"
        real_parent.mkdir()
        linked_parent = tmp_path / "linked-output"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        destination = linked_parent / "downloaded.cml"
    hub_artifact.download_artifact(REPO, REVISION, destination)
    assert destination.read_bytes() == encrypted_file.read_bytes()
    cache = Path(download_mocks[1].call_args.kwargs["cache_dir"])
    assert cache.is_absolute() and cache == cache.resolve()
    assert not cache.exists()


@pytest.mark.parametrize("escape", ["direct", "symlink"])
def test_download_rejects_file_outside_temporary_cache(tmp_path, encrypted_file, download_mocks, escape):
    _, download, _, caches = download_mocks
    original = encrypted_file.read_bytes()

    def escaped_download(**kwargs):
        cache = Path(kwargs["cache_dir"])
        cache.mkdir(parents=True)
        caches.append(cache)
        if escape == "direct":
            return str(encrypted_file)
        linked = cache / "model.cml"
        linked.symlink_to(encrypted_file)
        return str(linked)

    download.side_effect = escaped_download
    destination = tmp_path / "downloaded.cml"
    with pytest.raises(ValueError, match="escaped"):
        hub_artifact.download_artifact(REPO, REVISION, destination)
    assert not destination.exists()
    assert encrypted_file.read_bytes() == original
    assert caches and all(not cache.exists() for cache in caches)


@pytest.mark.parametrize("revision", ["main", "abc123", "g" * 40])
def test_mutable_or_invalid_revision_fails_before_network(tmp_path, download_mocks, revision):
    with pytest.raises(ValueError):
        hub_artifact.download_artifact(REPO, revision, tmp_path / "downloaded.cml")
    for mocked in download_mocks[:3]:
        mocked.assert_not_called()


@pytest.mark.parametrize("problem", ["oversized", "empty", "wrong-commit"])
def test_invalid_metadata_fails_before_download(tmp_path, download_mocks, problem):
    metadata, download, _, _ = download_mocks
    if problem == "wrong-commit":
        metadata.return_value.commit_hash = NEW_REVISION
    else:
        metadata.return_value.size = MAX_ARTIFACT_BYTES + 1 if problem == "oversized" else 0
    destination = tmp_path / "downloaded.cml"
    with pytest.raises(ValueError):
        hub_artifact.download_artifact(REPO, REVISION, destination)
    download.assert_not_called()
    assert not destination.exists()


@pytest.mark.parametrize("problem", ["bad-header", "size-mismatch"])
def test_downloaded_invalid_bytes_leave_no_output(tmp_path, encrypted_file, download_mocks, problem):
    metadata, download, _, _ = download_mocks
    encrypted_file.write_bytes(b"invalid header" * 4 if problem == "bad-header" else encrypt_package(b"tiny package")[0])
    metadata.return_value.size = encrypted_file.stat().st_size + (problem == "size-mismatch")
    destination = tmp_path / "downloaded.cml"
    with pytest.raises(ValueError):
        hub_artifact.download_artifact(REPO, REVISION, destination)
    assert not destination.exists()


def test_signature_download_is_pinned_anonymous_and_exact(tmp_path, signature_file, signature_download_mocks):
    metadata, download, url, caches = signature_download_mocks
    destination = tmp_path / "downloaded.sig"
    result = hub_artifact.download_signature(REPO, REVISION, destination)
    assert destination.read_bytes() == signature_file.read_bytes()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert result == {"repo_id": REPO, "revision": REVISION, "filename": "model.cml.sig", "signature_bytes": 64}
    url.assert_called_once_with(REPO, filename="model.cml.sig", revision=REVISION, repo_type="model")
    metadata.assert_called_once_with(url.return_value, token=False)
    download.assert_called_once()
    args = download.call_args.kwargs
    assert args["filename"] == "model.cml.sig" and args["revision"] == REVISION and args["token"] is False
    assert caches and all(not cache.exists() for cache in caches)


@pytest.mark.parametrize("problem", ["short", "long", "wrong-commit", "mutable-revision"])
def test_invalid_signature_request_or_metadata_prevents_download(tmp_path, signature_download_mocks, problem):
    metadata, download, url, _ = signature_download_mocks
    revision = REVISION
    if problem == "mutable-revision":
        revision = "main"
    elif problem == "wrong-commit":
        metadata.return_value.commit_hash = NEW_REVISION
    else:
        metadata.return_value.size = 63 if problem == "short" else 65
    destination = tmp_path / "downloaded.sig"
    with pytest.raises(ValueError):
        hub_artifact.download_signature(REPO, revision, destination)
    download.assert_not_called()
    if problem == "mutable-revision":
        metadata.assert_not_called()
        url.assert_not_called()
    assert not destination.exists()


@pytest.mark.parametrize("actual_size", [63, 65])
def test_signature_download_rejects_bytes_not_matching_metadata(tmp_path, signature_file, signature_download_mocks, actual_size):
    signature_file.write_bytes(b"s" * actual_size)
    destination = tmp_path / "downloaded.sig"
    with pytest.raises(ValueError):
        hub_artifact.download_signature(REPO, REVISION, destination)
    assert not destination.exists()
    assert all(not cache.exists() for cache in signature_download_mocks[3])


@pytest.mark.parametrize("phase", ["metadata", "download"])
def test_missing_remote_signature_propagates_without_fallback(tmp_path, signature_download_mocks, phase):
    metadata, download, _, _ = signature_download_mocks
    response = httpx.Response(404, request=httpx.Request("GET", "https://example.invalid/model.cml.sig"))
    missing = HfHubHTTPError("Signature does not exist", response=response)
    (metadata if phase == "metadata" else download).side_effect = missing
    destination = tmp_path / "downloaded.sig"
    with pytest.raises(HfHubHTTPError) as caught:
        hub_artifact.download_signature(REPO, REVISION, destination)
    assert caught.value is missing
    assert not destination.exists()
    assert list(tmp_path.glob(".hub-download-*")) == []
    if phase == "metadata":
        download.assert_not_called()
    else:
        download.assert_called_once()


@pytest.mark.parametrize("action", ["publish", "download-signature"])
def test_signature_cli_dispatches_explicit_paths(monkeypatch, capsys, action):
    operation = Mock(return_value={"revision": REVISION})
    arguments = ["hub_artifact", action, "--repo", REPO]
    if action == "publish":
        monkeypatch.setattr(hub_artifact, "publish_artifact", operation)
        arguments += ["--artifact", "model.cml", "--signature", "model.cml.sig"]
    else:
        monkeypatch.setattr(hub_artifact, "download_signature", operation)
        arguments += ["--revision", REVISION, "--destination", "downloaded.sig"]
    monkeypatch.setattr(sys, "argv", arguments)
    hub_artifact.main()
    if action == "publish":
        operation.assert_called_once_with(REPO, Path("model.cml"), signature_path=Path("model.cml.sig"))
    else:
        operation.assert_called_once_with(REPO, REVISION, Path("downloaded.sig"))
    assert REVISION in capsys.readouterr().out
