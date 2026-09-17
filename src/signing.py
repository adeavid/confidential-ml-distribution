"""Local Ed25519 signatures over complete encrypted CMLD artifacts."""

import argparse
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from artifact import (
    HEADER_BYTES,
    MAGIC,
    MAX_ARTIFACT_BYTES,
    PROJECT_ROOT,
    TAG_BYTES,
    _check_new_path,
    _read_regular,
    _write_new_file,
)


MAX_PEM_BYTES = 4096
SIGNATURE_BYTES = 64


def _read_artifact(path: Path) -> bytes:
    data = _read_regular(path, MAX_ARTIFACT_BYTES)
    # Structural check only; a signature does not replace GCM or package validation.
    if len(data) <= HEADER_BYTES + TAG_BYTES or not data.startswith(MAGIC):
        raise ValueError("Input is not a supported encrypted artifact.")
    return data


def _private_key(path: Path) -> Ed25519PrivateKey:
    data = _read_regular(path, MAX_PEM_BYTES, follow_symlinks=True)
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise ValueError("Expected an unencrypted Ed25519 private key in PEM format.") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Signing requires an Ed25519 private key.")
    return key


def _public_key(path: Path) -> Ed25519PublicKey:
    data = _read_regular(path, MAX_PEM_BYTES, follow_symlinks=True)
    try:
        key = serialization.load_pem_public_key(data)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise ValueError("Expected an Ed25519 public key in PEM format.") from None
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Verification requires an Ed25519 public key.")
    return key


def generate_key_pair(private_key_path: Path, public_key_path: Path) -> dict:
    """Create a PEM pair exclusively; keep the private file out of public outputs."""
    private_key_path, public_key_path = Path(private_key_path), Path(public_key_path)
    _check_new_path(private_key_path)
    _check_new_path(public_key_path)
    private_location = private_key_path.resolve()
    if (private_location.is_relative_to(PROJECT_ROOT)
            or private_location.is_relative_to(public_key_path.parent.resolve())):
        raise ValueError("Keep the signing private key outside the project and public output directory.")
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _write_new_file(private_key_path, private_pem)
    try:
        _write_new_file(public_key_path, public_pem)
    except BaseException:
        private_key_path.unlink()
        raise
    return {"status": "keys-created", "algorithm": "Ed25519"}


def sign_artifact(artifact_path: Path, private_key_path: Path, signature_path: Path) -> dict:
    """Sign the exact envelope bytes, including header, nonce, ciphertext and tag."""
    artifact_path, private_key_path, signature_path = map(
        Path, (artifact_path, private_key_path, signature_path),
    )
    _check_new_path(signature_path)
    private_location = private_key_path.resolve()
    if (private_location.is_relative_to(PROJECT_ROOT)
            or private_location.is_relative_to(signature_path.parent.resolve())):
        raise ValueError("Keep the signing private key outside the project and signature output directory.")
    data = _read_artifact(artifact_path)
    signature = _private_key(private_key_path).sign(data)
    _write_new_file(signature_path, signature)
    return {
        "status": "signed", "algorithm": "Ed25519",
        "artifact_bytes": len(data), "signature_bytes": len(signature),
    }


def verify_artifact(artifact_path: Path, signature_path: Path, public_key_path: Path) -> bytes:
    """Return the verified ciphertext; the caller supplies a trusted public-key path.

    A later decryption step must consume these returned bytes, not reread the file.
    This prevents checking one buffer and then decrypting a replaced file.
    """
    data = _read_artifact(Path(artifact_path))
    signature = _read_regular(Path(signature_path), SIGNATURE_BYTES)
    if len(signature) != SIGNATURE_BYTES:
        raise ValueError("An Ed25519 signature must contain exactly 64 bytes.")
    try:
        _public_key(Path(public_key_path)).verify(signature, data)
    except InvalidSignature:
        raise ValueError("Signature verification failed: wrong public key or modified artifact/signature.") from None
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    keygen = commands.add_parser("keygen")
    keygen.add_argument("--private-key", type=Path, required=True)
    keygen.add_argument("--public-key", type=Path, required=True)
    sign = commands.add_parser("sign")
    sign.add_argument("--private-key", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--public-key", type=Path, required=True)
    for command in (sign, verify):
        command.add_argument("--artifact", type=Path, required=True)
        command.add_argument("--signature", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "keygen":
            result = generate_key_pair(args.private_key, args.public_key)
        elif args.action == "sign":
            result = sign_artifact(args.artifact, args.private_key, args.signature)
        else:
            data = verify_artifact(args.artifact, args.signature, args.public_key)
            result = {"status": "verified", "algorithm": "Ed25519", "artifact_bytes": len(data)}
    except (ValueError, OSError) as error:
        parser.exit(1, f"error: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
