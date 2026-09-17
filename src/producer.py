"""Run the model Producer without Kubernetes API access."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from artifact import _read_regular, _write_new_file, encrypt_package, pack_model
from hub_artifact import publish_artifact
from model_demo import SOURCE_REVISION, download_source
from signing import sign_artifact


def run_producer(
    repo_id: str, key_path: Path, work_directory: Path, *, signing_key_path: Path | None = None,
) -> dict:
    key = _read_regular(Path(key_path), 32, follow_symlinks=True)
    if len(key) != 32:
        raise ValueError("AES-256 key must contain exactly 32 bytes.")
    with TemporaryDirectory(dir=work_directory, prefix="producer-") as temporary:
        working = Path(temporary)
        source = working / "source"
        download_source(source)
        encrypted, _ = encrypt_package(pack_model(source), key=key)
        public = working / "public"
        public.mkdir(mode=0o700)
        artifact = public / "model.cml"
        _write_new_file(artifact, encrypted)
        if signing_key_path is not None:
            signature = public / "model.cml.sig"
            sign_artifact(artifact, signing_key_path, signature)
            publication = publish_artifact(repo_id, artifact, signature)
        else:
            publication = publish_artifact(repo_id, artifact)
    return {
        "status": "published",
        **publication,
        "artifact_bytes": len(encrypted),
        "source_revision": SOURCE_REVISION,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--key-file", type=Path, default=Path("/run/secrets/model/key"))
    parser.add_argument("--work-dir", type=Path, default=Path("/work"))
    parser.add_argument("--signing-key-file", type=Path, help="Enable Layer 2 with a mounted Ed25519 private key.")
    args = parser.parse_args()
    try:
        result = run_producer(args.repo, args.key_file, args.work_dir, signing_key_path=args.signing_key_file)
    except httpx.HTTPError:
        parser.exit(1, "error: Producer Hub request failed; check connectivity and write permissions.\n")
    except (ValueError, OSError) as error:
        parser.exit(1, f"error: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
