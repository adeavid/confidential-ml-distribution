"""Bounded model packaging and AES-256-GCM encryption for the local PoC."""

import argparse
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import zipfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from model_demo import SOURCE_FILES


MAGIC = b"CMLD\x01"  # Four-byte identifier and one-byte format version.
NONCE_BYTES = 12
HEADER_BYTES = len(MAGIC) + NONCE_BYTES
TAG_BYTES = 16
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_BYTES = MAX_PACKAGE_BYTES + HEADER_BYTES + TAG_BYTES
MAX_FILE_BYTES = 32 * 1024 * 1024
PACKAGE_FILES = (*SOURCE_FILES, "LICENSE")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LICENSE_PATH = PROJECT_ROOT / "licenses" / "model-APACHE-2.0.txt"


def _read_regular(path: Path, limit: int, *, follow_symlinks: bool = False) -> bytes:
    """Bound the read itself; nonblocking open avoids waiting on a FIFO."""
    flags = os.O_RDONLY | os.O_NONBLOCK
    if not follow_symlinks:
        flags |= os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Input must be a regular file.")
        data = stream.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError("Input is empty or exceeds its size limit.")
    return data


def _check_new_path(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError("Output must not already exist.")
    if not path.parent.is_dir():
        raise ValueError("Output parent directory must already exist.")


def _write_new_file(path: Path, data: bytes) -> None:
    """Create exclusively with mode 0600; never remove a pre-existing file."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink()
        raise


def pack_model(source: Path) -> bytes:
    """Package only the source allowlist and the vendored Apache license."""
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Source must be a local directory, not a symlink.")
    output = BytesIO()
    total = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
        for name in PACKAGE_FILES:
            path = LICENSE_PATH if name == "LICENSE" else source / name
            data = _read_regular(path, MAX_FILE_BYTES)
            total += len(data)
            if total > MAX_PACKAGE_BYTES:
                raise ValueError("Package exceeds its size limit.")
            entry = zipfile.ZipInfo(name)  # Fixed timestamp, no source path metadata.
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(entry, data)
    package = output.getvalue()
    if len(package) > MAX_PACKAGE_BYTES:
        raise ValueError("Package exceeds its size limit.")
    return package


def encrypt_package(package: bytes, *, key: bytes | None = None) -> tuple[bytes, bytes]:
    """Use a fresh nonce, with a new local key or a key provisioned by bootstrap."""
    if not package or len(package) > MAX_PACKAGE_BYTES:
        raise ValueError("Package is empty or exceeds its size limit.")
    if key is None:
        key = AESGCM.generate_key(bit_length=256)
    if len(key) != 32:
        raise ValueError("AES-256 key must contain exactly 32 bytes.")
    nonce = os.urandom(NONCE_BYTES)
    header = MAGIC + nonce
    # The public header is authenticated as associated data (AAD).
    ciphertext_and_tag = AESGCM(key).encrypt(nonce, package, header)
    return header + ciphertext_and_tag, key


def decrypt_package(artifact: bytes, key: bytes) -> bytes:
    """Release plaintext only after GCM authentication succeeds."""
    if len(key) != 32:
        raise ValueError("AES-256 key must contain exactly 32 bytes.")
    if not HEADER_BYTES + TAG_BYTES < len(artifact) <= MAX_ARTIFACT_BYTES:
        raise ValueError("Artifact is truncated or exceeds its size limit.")
    if not artifact.startswith(MAGIC):
        raise ValueError("Unsupported artifact format or version.")
    header = artifact[:HEADER_BYTES]
    nonce = header[len(MAGIC):]
    try:
        return AESGCM(key).decrypt(nonce, artifact[HEADER_BYTES:], header)
    except InvalidTag:
        # The tag cannot distinguish a wrong key from tampering or truncation.
        raise ValueError("Artifact authentication failed: wrong key or modified artifact.") from None


def extract_package(package: bytes, destination: Path) -> None:
    """Validate a flat stored ZIP and write into a private staging directory."""
    destination = Path(destination)
    _check_new_path(destination)
    if not package or len(package) > MAX_PACKAGE_BYTES:
        raise ValueError("Package is empty or exceeds its size limit.")
    try:
        with zipfile.ZipFile(BytesIO(package), "r") as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(PACKAGE_FILES) or set(names) != set(PACKAGE_FILES):
                raise ValueError("Package must contain exactly the expected files, without duplicates.")
            total = 0
            for entry in entries:
                # Exact names also reject traversal, absolute paths and directories.
                if entry.orig_filename != entry.filename or not stat.S_ISREG(entry.external_attr >> 16):
                    raise ValueError("Package contains an unsafe name or non-regular file.")
                if entry.compress_type != zipfile.ZIP_STORED or entry.flag_bits & 1:
                    raise ValueError("Package entries must be stored without compression or ZIP encryption.")
                if not 0 < entry.file_size <= MAX_FILE_BYTES or entry.compress_size != entry.file_size:
                    raise ValueError("Package member exceeds its size limit or has invalid sizes.")
                total += entry.file_size
            if total > MAX_PACKAGE_BYTES:
                raise ValueError("Package exceeds its size limit.")

            # Never apply paths, permissions or timestamps from the archive.
            with TemporaryDirectory(dir=destination.parent, prefix=".decrypt-") as temporary:
                staging = Path(temporary) / "model"
                staging.mkdir(mode=0o700)
                for entry in entries:
                    with archive.open(entry) as stream:
                        data = stream.read(MAX_FILE_BYTES + 1)
                    if len(data) != entry.file_size or len(data) > MAX_FILE_BYTES:
                        raise ValueError("Package member has an invalid size.")
                    _write_new_file(staging / entry.filename, data)
                _check_new_path(destination)
                staging.rename(destination)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, NotImplementedError, RuntimeError) as error:
        raise ValueError("Invalid model package.") from error


def encrypt_model(source: Path, artifact_path: Path, key_path: Path) -> dict:
    """Create local outputs only; publishing and Secret provisioning are separate."""
    source, artifact_path, key_path = Path(source), Path(artifact_path), Path(key_path)
    _check_new_path(artifact_path)
    _check_new_path(key_path)
    resolved_key = key_path.resolve()
    if (resolved_key.is_relative_to(PROJECT_ROOT)
            or resolved_key.is_relative_to(artifact_path.parent.resolve())
            or resolved_key.is_relative_to(source.resolve())):
        raise ValueError("Keep the key outside the project, source and artifact directories.")
    package = pack_model(source)
    artifact, key = encrypt_package(package)
    _write_new_file(key_path, key)
    try:
        _write_new_file(artifact_path, artifact)
    except BaseException:
        key_path.unlink()
        raise
    return {
        "format": "CMLD/1",
        "artifact_bytes": len(artifact),
        "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "key_written": True,
    }


def decrypt_model(artifact_path: Path, key_path: Path, destination: Path) -> dict:
    """Authenticate before parsing or extracting; mounted Secret links are allowed."""
    artifact = _read_regular(Path(artifact_path), MAX_ARTIFACT_BYTES)
    key = _read_regular(Path(key_path), 32, follow_symlinks=True)
    package = decrypt_package(artifact, key)
    extract_package(package, Path(destination))
    return {"format": "CMLD/1", "files": list(PACKAGE_FILES), "authenticated": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    encrypt = commands.add_parser("encrypt")
    encrypt.add_argument("--source", type=Path, required=True)
    decrypt = commands.add_parser("decrypt")
    decrypt.add_argument("--destination", type=Path, required=True)
    for command in (encrypt, decrypt):
        command.add_argument("--artifact", type=Path, required=True)
        command.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "encrypt":
            result = encrypt_model(args.source, args.artifact, args.key_file)
        else:
            result = decrypt_model(args.artifact, args.key_file, args.destination)
    except (ValueError, OSError) as error:
        parser.exit(1, f"error: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
