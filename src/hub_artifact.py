"""Publish encrypted artifacts and optional signatures at immutable Hub revisions."""

import argparse
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import httpx
from huggingface_hub import (
    CommitOperationAdd,
    CommitOperationDelete,
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
from signing import SIGNATURE_BYTES


ARTIFACT_NAME = "model.cml"
SIGNATURE_NAME = "model.cml.sig"


def _check_revision(revision: str) -> None:
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Hub revision must be a full 40-character commit ID.")


def _check_envelope(data: bytes) -> None:
    # This is only a format check. The Consumer must still authenticate with AES-GCM.
    if not HEADER_BYTES + TAG_BYTES < len(data) <= MAX_ARTIFACT_BYTES or not data.startswith(MAGIC):
        raise ValueError("Input is not a supported encrypted artifact.")


def publish_artifact(repo_id: str, artifact_path: Path, signature_path: Path | None = None) -> dict:
    """Commit explicit artifact/signature bytes together, using local HF authentication."""
    validate_repo_id(repo_id)
    data = _read_regular(Path(artifact_path), MAX_ARTIFACT_BYTES)
    _check_envelope(data)
    signature = None
    if signature_path is not None:
        signature = _read_regular(Path(signature_path), SIGNATURE_BYTES)
        if len(signature) != SIGNATURE_BYTES:
            raise ValueError("An Ed25519 signature must contain exactly 64 bytes.")
    api = HfApi()
    api.create_repo(repo_id=repo_id, repo_type="model", private=False, exist_ok=True)
    info = api.model_info(repo_id)
    if info.private is not False:
        raise ValueError("Use the authorized public test repository.")
    if info.siblings is None:
        raise ValueError("Repository file list is unavailable; refusing publication.")
    existing_files = {item.rfilename for item in info.siblings}
    if not existing_files <= {".gitattributes", ARTIFACT_NAME, SIGNATURE_NAME}:
        raise ValueError("Test repository contains unexpected files; refusing publication.")
    _check_revision(info.sha)
    operations = [CommitOperationAdd(path_in_repo=ARTIFACT_NAME, path_or_fileobj=data)]
    if signature is not None:
        operations.append(CommitOperationAdd(path_in_repo=SIGNATURE_NAME, path_or_fileobj=signature))
    elif SIGNATURE_NAME in existing_files:
        # An unsigned publication must not inherit a signature for older bytes.
        operations.append(CommitOperationDelete(path_in_repo=SIGNATURE_NAME, is_folder=False))
    commit = api.create_commit(
        repo_id=repo_id,
        repo_type="model",
        revision="main",
        create_pr=False,
        parent_commit=info.sha,
        operations=operations,
        commit_message="Publish encrypted model artifact" + (" and signature" if signature is not None else ""),
    )
    _check_revision(commit.oid)
    result = {"repo_id": repo_id, "revision": commit.oid, "filename": ARTIFACT_NAME}
    if signature is not None:
        result["signature_filename"] = SIGNATURE_NAME
    return result


def _download_bytes(
    repo_id: str, revision: str, destination: Path, filename: str, minimum: int, maximum: int,
) -> bytes:
    """Download one pinned file anonymously into a fresh, bounded private cache."""
    validate_repo_id(repo_id)
    _check_revision(revision)
    destination = Path(destination)
    _check_new_path(destination)
    metadata = get_hf_file_metadata(
        hf_hub_url(repo_id, filename=filename, revision=revision, repo_type="model"),
        token=False,
    )
    if metadata.commit_hash != revision:
        raise ValueError("Hub metadata does not match the requested revision.")
    if not isinstance(metadata.size, int) or not minimum <= metadata.size <= maximum:
        raise ValueError("Remote file has an invalid or excessive size.")
    with TemporaryDirectory(dir=destination.parent, prefix=".hub-download-") as temporary:
        cache_directory = (Path(temporary) / "cache").resolve()
        cached = hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            revision=revision,
            repo_type="model",
            token=False,
            cache_dir=cache_directory,
        )
        # Hub cache snapshots may be links to blobs; this fresh cache is trusted.
        resolved = Path(cached).resolve()
        if not resolved.is_relative_to(cache_directory):
            raise ValueError("Downloaded file escaped the private temporary cache.")
        data = _read_regular(resolved, maximum)
        if len(data) != metadata.size:
            raise ValueError("Downloaded size does not match Hub metadata.")
        return data


def download_artifact(repo_id: str, revision: str, destination: Path) -> dict:
    """Download and structurally validate ciphertext; authentication happens later."""
    destination = Path(destination)
    data = _download_bytes(
        repo_id, revision, destination, ARTIFACT_NAME, HEADER_BYTES + TAG_BYTES + 1, MAX_ARTIFACT_BYTES,
    )
    _check_envelope(data)
    _write_new_file(destination, data)
    return {
        "repo_id": repo_id,
        "revision": revision,
        "filename": ARTIFACT_NAME,
        "artifact_bytes": len(data),
    }


def download_signature(repo_id: str, revision: str, destination: Path) -> dict:
    """Retrieve exactly 64 signature bytes; callers must use the artifact's revision."""
    destination = Path(destination)
    data = _download_bytes(repo_id, revision, destination, SIGNATURE_NAME, SIGNATURE_BYTES, SIGNATURE_BYTES)
    _write_new_file(destination, data)
    return {
        "repo_id": repo_id, "revision": revision, "filename": SIGNATURE_NAME,
        "signature_bytes": len(data),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--artifact", type=Path, required=True)
    publish.add_argument("--signature", type=Path, help="Optional detached Ed25519 signature for Layer 2.")
    download = commands.add_parser("download")
    download_sig = commands.add_parser("download-signature")
    for command in (download, download_sig):
        command.add_argument("--revision", required=True)
        command.add_argument("--destination", type=Path, required=True)
    for command in (publish, download, download_sig):
        command.add_argument("--repo", required=True)
    args = parser.parse_args()
    try:
        if args.action == "publish":
            result = publish_artifact(args.repo, args.artifact, signature_path=args.signature)
        elif args.action == "download":
            result = download_artifact(args.repo, args.revision, args.destination)
        else:
            result = download_signature(args.repo, args.revision, args.destination)
    except httpx.HTTPError as error:
        response = getattr(error, "response", None)
        status = response.status_code if response is not None else "network failure"
        parser.exit(1, f"error: Hugging Face request failed ({status}). Check connectivity and repository permissions.\n")
    except (ValueError, OSError) as error:
        parser.exit(1, f"error: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
