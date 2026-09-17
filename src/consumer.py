"""Download, authenticate, decrypt and load a model, then exit."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import httpx

from artifact import MAX_ARTIFACT_BYTES, _read_regular, decrypt_model, decrypt_package, extract_package
from cdh import read_cdh_key, validate_cdh_resource
from hub_artifact import download_artifact, download_signature
from signing import verify_artifact


def _load_offline(model_directory: Path, cache_directory: Path) -> dict:
    """Set offline flags in a fresh interpreter only after the download phase."""
    cache_directory.mkdir(mode=0o700)
    environment = os.environ.copy()
    for name in ("HF_TOKEN", "HF_TOKEN_PATH", "HUGGING_FACE_HUB_TOKEN", "TRANSFORMERS_CACHE"):
        environment.pop(name, None)
    environment.update({
        "HF_HOME": str(cache_directory),
        "HF_HUB_CACHE": str(cache_directory / "hub"),
        "HUGGINGFACE_HUB_CACHE": str(cache_directory / "hub"),
        "HF_XET_CACHE": str(cache_directory / "xet"),
        "HF_MODULES_CACHE": str(cache_directory / "modules"),
        "TORCH_HOME": str(cache_directory / "torch"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    })
    process = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("model_demo.py")), "load", str(model_directory)],
        env=environment, capture_output=True, text=True, timeout=120,
    )
    if process.returncode != 0:
        raise ValueError("Local model loading failed; no fallback is permitted.")
    result = json.loads(process.stdout)
    if result.get("device") != "cpu" or result.get("finite_output") is not True:
        raise ValueError("Local model did not pass the CPU check.")
    return result


def run_consumer(
    repo_id: str, revision: str, key_path: Path | None, work_directory: Path, *,
    require_signature: bool = False, public_key_path: Path | None = None,
    cdh_resource: str | None = None,
) -> dict:
    # Explicit mode and trust configuration must agree; never downgrade silently.
    if require_signature != (public_key_path is not None):
        raise ValueError("Layer 2 requires both --require-signature and --public-key-file.")
    if (key_path is None) == (cdh_resource is None):
        raise ValueError("Select exactly one AES key source: a key file or CDH resource.")
    if cdh_resource is not None:
        validate_cdh_resource(cdh_resource)
    with TemporaryDirectory(dir=work_directory, prefix="consumer-") as temporary:
        working = Path(temporary)
        artifact = working / "model.cml"
        model = working / "model"
        download_artifact(repo_id, revision, artifact)
        if require_signature:
            signature = working / "model.cml.sig"
            download_signature(repo_id, revision, signature)
            verified = verify_artifact(artifact, signature, public_key_path)
        elif cdh_resource is not None:
            verified = _read_regular(artifact, MAX_ARTIFACT_BYTES)
        if require_signature or cdh_resource is not None:
            # Request the key only after signature verification, when required.
            key = (read_cdh_key(cdh_resource) if cdh_resource is not None
                   else _read_regular(Path(key_path), 32, follow_symlinks=True))
            # In signed mode, decrypt the exact buffer verified above, without rereading.
            package = decrypt_package(verified, key)
            extract_package(package, model)
        else:
            decrypt_model(artifact, key_path, model)
        result = _load_offline(model, working / "local-cache")
    report = {"status": "loaded", "repo_id": repo_id, "revision": revision, "model": result}
    if require_signature:
        report["signature_verified"] = True
    if cdh_resource is not None:
        report["key_source"] = "cdh"
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--revision", required=True)
    key_source = parser.add_mutually_exclusive_group()
    key_source.add_argument("--key-file", type=Path, help="Mounted AES key; default: /run/secrets/model/key.")
    key_source.add_argument("--cdh-resource", help="Retrieve AES from guest CDH, e.g. default/key/my-model.")
    parser.add_argument("--work-dir", type=Path, default=Path("/work"))
    parser.add_argument("--require-signature", action="store_true", help="Require Layer 2; no unsigned fallback.")
    parser.add_argument("--public-key-file", type=Path, help="Trusted Ed25519 key from controlled configuration.")
    args = parser.parse_args()
    key_path = args.key_file
    if key_path is None and args.cdh_resource is None:
        key_path = Path("/run/secrets/model/key")
    try:
        result = run_consumer(
            args.repo, args.revision, key_path, args.work_dir,
            require_signature=args.require_signature, public_key_path=args.public_key_file,
            cdh_resource=args.cdh_resource,
        )
    except httpx.HTTPError:
        parser.exit(1, "error: Consumer Hub request failed; no source-model fallback is permitted.\n")
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"error: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
