"""Publish one encrypted file and retrieve it from an immutable Hub revision."""

import argparse
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import httpx
from huggingface_hub import (
    CommitOperationAdd,
    HfApi,
    get_hf_file_metadata,
    hf_hub_download,
    hf_hub_url,
)
from huggingface_hub.utils import validate_repo_id

from artifact import (
    HEADER_BYTES,
    MAGIC,
    MAX_ARTIFACT_BYTES,
    TAG_BYTES,
    _check_new_path,
    _read_regular,
    _write_new_file,
)


ARTIFACT_NAME = "model.cml"


def _check_revision(revision: str) -> None:
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Hub revision must be a full 40-character commit ID.")


def _check_envelope(data: bytes) -> None:
    # This is only a format check. The Consumer must still authenticate with AES-GCM.
    if not HEADER_BYTES + TAG_BYTES < len(data) <= MAX_ARTIFACT_BYTES or not data.startswith(MAGIC):
        raise ValueError("Input is not a supported encrypted artifact.")


def publish_artifact(repo_id: str, artifact_path: Path) -> dict:
    """Use local HF authentication and an explicit, single-file commit."""
    validate_repo_id(repo_id)
    data = _read_regular(Path(artifact_path), MAX_ARTIFACT_BYTES)
    _check_envelope(data)
    api = HfApi()
    api.create_repo(repo_id=repo_id, repo_type="model", private=False, exist_ok=True)
    info = api.model_info(repo_id)
    if info.private is not False:
        raise ValueError("Use the authorized public test repository.")
    if info.siblings is None:
        raise ValueError("Repository file list is unavailable; refusing publication.")
    if any(item.rfilename not in {".gitattributes", ARTIFACT_NAME} for item in info.siblings):
        raise ValueError("Test repository contains unexpected files; refusing publication.")
    _check_revision(info.sha)
    commit = api.create_commit(
        repo_id=repo_id,
        repo_type="model",
        revision="main",
        create_pr=False,
        parent_commit=info.sha,
        operations=[CommitOperationAdd(path_in_repo=ARTIFACT_NAME, path_or_fileobj=data)],
        commit_message="Publish encrypted model artifact",
    )
    _check_revision(commit.oid)
    return {"repo_id": repo_id, "revision": commit.oid, "filename": ARTIFACT_NAME}


def download_artifact(repo_id: str, revision: str, destination: Path) -> dict:
    """Download anonymously into a fresh cache, enforcing the expected size."""
    validate_repo_id(repo_id)
    _check_revision(revision)
    destination = Path(destination)
    _check_new_path(destination)
    metadata = get_hf_file_metadata(
        hf_hub_url(repo_id, filename=ARTIFACT_NAME, revision=revision, repo_type="model"),
        token=False,
    )
    if metadata.commit_hash != revision:
        raise ValueError("Hub metadata does not match the requested revision.")
    if metadata.size is None or not HEADER_BYTES + TAG_BYTES < metadata.size <= MAX_ARTIFACT_BYTES:
        raise ValueError("Remote artifact has an invalid or excessive size.")
    with TemporaryDirectory(dir=destination.parent, prefix=".hub-download-") as temporary:
        cache_directory = (Path(temporary) / "cache").resolve()
        cached = hf_hub_download(
            repo_id=repo_id,
            filename=ARTIFACT_NAME,
            revision=revision,
            repo_type="model",
            token=False,
            cache_dir=cache_directory,
        )
        # Hub cache snapshots may be links to blobs; this fresh cache is trusted.
        resolved = Path(cached).resolve()
        if not resolved.is_relative_to(cache_directory):
            raise ValueError("Downloaded file escaped the private temporary cache.")
        data = _read_regular(resolved, MAX_ARTIFACT_BYTES)
        if len(data) != metadata.size:
            raise ValueError("Downloaded size does not match Hub metadata.")
        _check_envelope(data)
        _write_new_file(destination, data)
    return {
        "repo_id": repo_id,
        "revision": revision,
        "filename": ARTIFACT_NAME,
        "artifact_bytes": len(data),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--artifact", type=Path, required=True)
    download = commands.add_parser("download")
    download.add_argument("--revision", required=True)
    download.add_argument("--destination", type=Path, required=True)
    for command in (publish, download):
        command.add_argument("--repo", required=True)
    args = parser.parse_args()
    try:
        if args.action == "publish":
            result = publish_artifact(args.repo, args.artifact)
        else:
            result = download_artifact(args.repo, args.revision, args.destination)
    except (ValueError, OSError) as error:
        parser.exit(1, f"error: {error}\n")
    except httpx.HTTPError as error:
        response = getattr(error, "response", None)
        status = response.status_code if response is not None else "network failure"
        parser.exit(1, f"error: Hugging Face request failed ({status}). Check connectivity and repository permissions.\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
