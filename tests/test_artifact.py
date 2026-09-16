"""Small, offline fixtures exercise authenticated encryption and bounded extraction."""

import io
import json
from pathlib import Path
import stat
import struct
from tempfile import TemporaryDirectory
import warnings
import zipfile

import pytest

import artifact
from model_demo import SOURCE_FILES


@pytest.fixture
def source(tmp_path):
    directory = tmp_path / "source"
    directory.mkdir()
    for filename in SOURCE_FILES:
        (directory / filename).write_bytes(f"fixture: {filename}\n".encode())
    return directory


def make_package(entries=None, *, compression=zipfile.ZIP_STORED):
    """Build a real ZIP; payloads are opaque bytes, not executable model objects."""
    if entries is None:
        entries = [(name, b"fixture", stat.S_IFREG | 0o600) for name in artifact.PACKAGE_FILES]
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate name:")
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content, mode in entries:
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = mode << 16
                entry.compress_type = compression
                archive.writestr(entry, content)
    return buffer.getvalue()


def flip_byte(data, position):
    changed = bytearray(data)
    changed[position] ^= 1
    return bytes(changed)


def test_pack_encrypt_decrypt_extract_roundtrip_uses_allowlist(source, tmp_path):
    (source / "private.key").write_bytes(b"must never enter the package")
    package = artifact.pack_model(source)
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        assert set(archive.namelist()) == set(artifact.PACKAGE_FILES)
        assert len(archive.namelist()) == len(artifact.PACKAGE_FILES)
        assert all(entry.compress_type == zipfile.ZIP_STORED for entry in archive.infolist())
        for filename in SOURCE_FILES:
            assert archive.read(filename) == (source / filename).read_bytes()
        license_path = Path(__file__).resolve().parents[1] / "licenses" / "model-APACHE-2.0.txt"
        assert archive.read("LICENSE") == license_path.read_bytes()

    encrypted, key = artifact.encrypt_package(package)
    assert len(key) == 32
    assert encrypted.startswith(artifact.MAGIC)
    assert len(encrypted) == artifact.HEADER_BYTES + len(package) + artifact.TAG_BYTES
    recovered = artifact.decrypt_package(encrypted, key)
    assert recovered == package
    destination = tmp_path / "recovered"
    artifact.extract_package(recovered, destination)
    assert {path.name for path in destination.iterdir()} == set(artifact.PACKAGE_FILES)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    for filename in SOURCE_FILES:
        assert (destination / filename).read_bytes() == (source / filename).read_bytes()
        assert stat.S_IMODE((destination / filename).stat().st_mode) == 0o600


def test_each_encryption_generates_a_new_key_and_nonce():
    first, first_key = artifact.encrypt_package(make_package())
    second, second_key = artifact.encrypt_package(make_package())
    assert first_key != second_key
    assert first[len(artifact.MAGIC):artifact.HEADER_BYTES] != second[len(artifact.MAGIC):artifact.HEADER_BYTES]
    assert first != second


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda data: b"", id="empty"),
        pytest.param(lambda data: data[:artifact.HEADER_BYTES - 1], id="short-header"),
        pytest.param(lambda data: flip_byte(data, 0), id="wrong-magic"),
        pytest.param(lambda data: flip_byte(data, len(artifact.MAGIC) - 1), id="wrong-version"),
        pytest.param(lambda data: flip_byte(data, len(artifact.MAGIC)), id="changed-nonce"),
        pytest.param(lambda data: flip_byte(data, artifact.HEADER_BYTES), id="changed-ciphertext"),
        pytest.param(lambda data: flip_byte(data, -1), id="changed-tag"),
        pytest.param(lambda data: data[:-1], id="truncated"),
        pytest.param(lambda data: data[:artifact.HEADER_BYTES + artifact.TAG_BYTES - 1], id="short-tag"),
    ],
)
def test_invalid_artifact_is_rejected(mutation):
    encrypted, key = artifact.encrypt_package(make_package())
    with pytest.raises(ValueError):
        artifact.decrypt_package(mutation(encrypted), key)


@pytest.mark.parametrize("key", [b"wrong" * 6 + b"!!", b"x" * 31, b"x" * 33])
def test_wrong_or_invalid_length_aes_key_is_rejected(key):
    encrypted, _ = artifact.encrypt_package(make_package())
    with pytest.raises(ValueError):
        artifact.decrypt_package(encrypted, key)


@pytest.mark.parametrize("problem", ["tag", "ciphertext", "truncation", "wrong-key"])
def test_authentication_failure_happens_before_extraction(tmp_path, monkeypatch, problem):
    encrypted, key = artifact.encrypt_package(make_package())
    encrypted_path = tmp_path / "model.enc"
    key_path = tmp_path / "model.key"
    destination = tmp_path / "decrypted"
    if problem == "tag":
        encrypted = flip_byte(encrypted, -1)
    elif problem == "ciphertext":
        encrypted = flip_byte(encrypted, artifact.HEADER_BYTES)
    elif problem == "truncation":
        encrypted = encrypted[:-1]
    else:
        key = flip_byte(key, 0)
    encrypted_path.write_bytes(encrypted)
    key_path.write_bytes(key)
    key_path.chmod(0o600)

    def forbidden(*args, **kwargs):
        pytest.fail("Unauthenticated plaintext reached archive extraction.")

    monkeypatch.setattr(artifact, "extract_package", forbidden)
    with pytest.raises(ValueError):
        artifact.decrypt_model(encrypted_path, key_path, destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "name, mode",
    [
        ("../outside", stat.S_IFREG | 0o600),
        ("/outside", stat.S_IFREG | 0o600),
        ("..\\outside", stat.S_IFREG | 0o600),
        ("nested/config.json", stat.S_IFREG | 0o600),
        ("unexpected.txt", stat.S_IFREG | 0o600),
        ("config.json", stat.S_IFLNK | 0o777),
        ("config.json", stat.S_IFIFO | 0o600),
        ("config.json/", stat.S_IFDIR | 0o700),
    ],
)
def test_unsafe_archive_members_are_rejected_without_output(tmp_path, name, mode):
    entries = [(item, b"fixture", stat.S_IFREG | 0o600) for item in artifact.PACKAGE_FILES[1:]]
    entries.append((name, b"../outside", mode))
    destination = tmp_path / "decrypted"
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError):
        artifact.extract_package(make_package(entries), destination)
    assert not destination.exists()
    assert set(tmp_path.iterdir()) == before


def test_nul_in_archive_filename_is_rejected_without_output(tmp_path):
    entries = [
        ("config.jsonXsuffix" if name == "config.json" else name, b"fixture", stat.S_IFREG | 0o600)
        for name in artifact.PACKAGE_FILES
    ]
    package = make_package(entries)
    # Keep lengths and offsets intact while changing both local and central names.
    assert package.count(b"config.jsonXsuffix") == 2
    package = package.replace(b"config.jsonXsuffix", b"config.json\x00suffix")
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        entry = archive.getinfo("config.json")
        assert entry.orig_filename == "config.json\x00suffix"
    with pytest.raises(ValueError):
        artifact.extract_package(package, tmp_path / "decrypted")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("problem", ["missing", "duplicate", "compressed", "encrypted", "invalid", "truncated", "bad-crc"])
def test_invalid_package_is_rejected_without_output(tmp_path, problem):
    entries = [(name, b"fixture", stat.S_IFREG | 0o600) for name in artifact.PACKAGE_FILES]
    if problem == "missing":
        entries.pop()
    elif problem == "duplicate":
        entries.append(entries[0])
    package = make_package(entries, compression=zipfile.ZIP_DEFLATED if problem == "compressed" else zipfile.ZIP_STORED)
    if problem == "encrypted":
        # zipfile cannot write encrypted archives; mark both header flag fields.
        data = bytearray(package)
        for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            offset = 0
            while (offset := data.find(signature, offset)) != -1:
                flags = struct.unpack_from("<H", data, offset + flag_offset)[0]
                struct.pack_into("<H", data, offset + flag_offset, flags | 1)
                offset += 4
        package = bytes(data)
    elif problem == "invalid":
        package = b"not a ZIP archive"
    elif problem == "truncated":
        package = package[:-10]
    elif problem == "bad-crc":
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            last = archive.infolist()[-1]
            data_offset = last.header_offset + 30 + len(last.filename.encode()) + len(last.extra)
        package = flip_byte(package, data_offset)
    destination = tmp_path / "decrypted"
    with pytest.raises(ValueError):
        artifact.extract_package(package, destination)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_size_limits_are_enforced_before_extraction(tmp_path, monkeypatch):
    package = make_package()
    encrypted, key = artifact.encrypt_package(package)
    monkeypatch.setattr(artifact, "MAX_PACKAGE_BYTES", len(package) - 1)
    monkeypatch.setattr(artifact, "MAX_ARTIFACT_BYTES", len(encrypted) - 1)
    with pytest.raises(ValueError):
        artifact.encrypt_package(package)
    with pytest.raises(ValueError):
        artifact.decrypt_package(encrypted, key)
    with pytest.raises(ValueError):
        artifact.extract_package(package, tmp_path / "decrypted")
    assert list(tmp_path.iterdir()) == []


def test_individual_extracted_file_size_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact, "MAX_FILE_BYTES", 6)
    with pytest.raises(ValueError):
        artifact.extract_package(make_package(), tmp_path / "decrypted")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("problem", ["missing", "symlink", "oversized"])
def test_unsafe_source_file_is_rejected(source, tmp_path, monkeypatch, problem):
    weights = source / "model.safetensors"
    if problem == "missing":
        weights.unlink()
    elif problem == "symlink":
        target = tmp_path / "weights"
        weights.rename(target)
        weights.symlink_to(target)
    else:
        monkeypatch.setattr(artifact, "MAX_FILE_BYTES", 32)
        weights.write_bytes(b"x" * 33)
    with pytest.raises((ValueError, OSError)):
        artifact.pack_model(source)


@pytest.fixture
def output_paths(tmp_path):
    public = tmp_path / "public"
    private = tmp_path / "private"
    public.mkdir()
    private.mkdir(mode=0o700)
    return public / "model.enc", private / "model.key"


def test_file_workflow_roundtrip_and_key_permissions(source, output_paths, tmp_path):
    encrypted_path, key_path = output_paths
    result = artifact.encrypt_model(source, encrypted_path, key_path)
    assert encrypted_path.is_file()
    assert len(key_path.read_bytes()) == 32
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert key_path.read_bytes().hex() not in json.dumps(result)
    destination = tmp_path / "decrypted"
    artifact.decrypt_model(encrypted_path, key_path, destination)
    for filename in SOURCE_FILES:
        assert (destination / filename).read_bytes() == (source / filename).read_bytes()


def test_read_only_secret_key_symlink_supports_decryption(source, output_paths, tmp_path):
    encrypted_path, key_path = output_paths
    artifact.encrypt_model(source, encrypted_path, key_path)
    key_path.chmod(0o440)
    mounted_key = tmp_path / "mounted.key"
    mounted_key.symlink_to(key_path)
    destination = tmp_path / "decrypted"
    artifact.decrypt_model(encrypted_path, mounted_key, destination)
    for filename in SOURCE_FILES:
        assert (destination / filename).read_bytes() == (source / filename).read_bytes()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o440


@pytest.mark.parametrize("existing", ["artifact", "key"])
def test_encryption_never_overwrites_or_leaves_partial_outputs(source, output_paths, existing):
    encrypted_path, key_path = output_paths
    occupied = encrypted_path if existing == "artifact" else key_path
    counterpart = key_path if existing == "artifact" else encrypted_path
    occupied.write_bytes(b"keep this existing file")
    with pytest.raises((ValueError, OSError)):
        artifact.encrypt_model(source, encrypted_path, key_path)
    assert occupied.read_bytes() == b"keep this existing file"
    assert not counterpart.exists()


def test_failed_artifact_write_removes_only_new_key(source, output_paths, monkeypatch):
    encrypted_path, key_path = output_paths
    real_write = artifact._write_new_file

    def fail_artifact_write(path, data):
        if path == encrypted_path:
            raise OSError("Simulated artifact write failure")
        real_write(path, data)

    monkeypatch.setattr(artifact, "_write_new_file", fail_artifact_write)
    with pytest.raises(OSError, match="Simulated artifact write failure"):
        artifact.encrypt_model(source, encrypted_path, key_path)
    assert not key_path.exists()
    assert not encrypted_path.exists()


def test_key_cannot_share_artifact_directory(source, tmp_path):
    encrypted_path, key_path = tmp_path / "model.enc", tmp_path / "model.key"
    with pytest.raises(ValueError):
        artifact.encrypt_model(source, encrypted_path, key_path)
    assert not encrypted_path.exists()
    assert not key_path.exists()


def test_key_cannot_be_written_inside_repository(source, output_paths):
    repository = Path(__file__).resolve().parents[1]
    encrypted_path, _ = output_paths
    with TemporaryDirectory(dir=repository, prefix=".test-key-location-") as directory:
        key_path = Path(directory) / "model.key"
        with pytest.raises(ValueError):
            artifact.encrypt_model(source, encrypted_path, key_path)
        assert not key_path.exists()
        assert not encrypted_path.exists()


def test_missing_output_parent_is_not_created(source, output_paths):
    encrypted_path, key_path = output_paths
    encrypted_path = encrypted_path.parent / "missing" / encrypted_path.name
    with pytest.raises((ValueError, OSError)):
        artifact.encrypt_model(source, encrypted_path, key_path)
    assert not encrypted_path.parent.exists()
    assert not key_path.exists()


def test_extraction_does_not_replace_existing_destination(tmp_path):
    destination = tmp_path / "decrypted"
    destination.mkdir()
    sentinel = destination / "existing.txt"
    sentinel.write_bytes(b"keep")
    with pytest.raises((ValueError, OSError)):
        artifact.extract_package(make_package(), destination)
    assert list(destination.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"keep"
