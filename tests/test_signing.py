"""Local Ed25519 checks with ephemeral keys; no Consumer or Hub integration."""

import json
import stat
import sys
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
import pytest

import artifact
import signing


@pytest.fixture
def paths(tmp_path):
    private = tmp_path / "keys"
    public = tmp_path / "public"
    private.mkdir(mode=0o700)
    public.mkdir()
    return SimpleNamespace(private=private / "signing.pem", public=public / "verify.pem",
                           artifact=public / "model.cml", signature=public / "model.cml.sig")


@pytest.fixture
def prepared(paths):
    signing.generate_key_pair(paths.private, paths.public)
    encrypted, _ = artifact.encrypt_package(b"fixture")
    paths.artifact.write_bytes(encrypted)
    return paths


@pytest.fixture
def signed(prepared):
    signing.sign_artifact(prepared.artifact, prepared.private, prepared.signature)
    return prepared


def test_key_pair_formats_permissions_and_safe_receipt(paths):
    receipt = signing.generate_key_pair(paths.private, paths.public)
    private_pem = paths.private.read_bytes()
    public_pem = paths.public.read_bytes()
    private = serialization.load_pem_private_key(private_pem, password=None)
    public = serialization.load_pem_public_key(public_pem)
    assert private_pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert public_pem.startswith(b"-----BEGIN PUBLIC KEY-----")
    assert isinstance(private, ed25519.Ed25519PrivateKey)
    assert isinstance(public, ed25519.Ed25519PublicKey)
    assert private.public_key().public_bytes_raw() == public.public_bytes_raw()
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in (paths.private, paths.public))
    assert isinstance(receipt, dict)
    assert "BEGIN PRIVATE KEY" not in json.dumps(receipt)
    assert private.private_bytes_raw().hex() not in json.dumps(receipt)


def test_signature_authenticates_exact_artifact_bytes(signed):
    encrypted = signed.artifact.read_bytes()
    signature = signed.signature.read_bytes()
    assert len(signature) == 64
    assert stat.S_IMODE(signed.signature.stat().st_mode) == 0o600
    public = serialization.load_pem_public_key(signed.public.read_bytes())
    public.verify(signature, encrypted)
    verified = signing.verify_artifact(signed.artifact, signed.signature, signed.public)
    assert isinstance(verified, bytes) and verified == encrypted


def test_aes_key_holder_can_replace_ciphertext_but_cannot_reuse_producer_signature(paths):
    signing.generate_key_pair(paths.private, paths.public)
    original, aes_key = artifact.encrypt_package(b"original model package")
    paths.artifact.write_bytes(original)
    signing.sign_artifact(paths.artifact, paths.private, paths.signature)
    assert signing.verify_artifact(paths.artifact, paths.signature, paths.public) == original

    replacement, _ = artifact.encrypt_package(b"replacement model package", key=aes_key)
    assert original[len(artifact.MAGIC):artifact.HEADER_BYTES] != replacement[len(artifact.MAGIC):artifact.HEADER_BYTES]
    assert artifact.decrypt_package(replacement, aes_key) == b"replacement model package"
    paths.artifact.write_bytes(replacement)
    with pytest.raises(ValueError, match="Signature verification failed"):
        signing.verify_artifact(paths.artifact, paths.signature, paths.public)


@pytest.mark.parametrize("position", [0, len(artifact.MAGIC), artifact.HEADER_BYTES, -1],
                         ids=["header", "nonce", "ciphertext", "tag"])
def test_every_artifact_region_is_protected(signed, position):
    changed = bytearray(signed.artifact.read_bytes())
    changed[position] ^= 1
    signed.artifact.write_bytes(changed)
    with pytest.raises((ValueError, OSError)):
        signing.verify_artifact(signed.artifact, signed.signature, signed.public)


@pytest.mark.parametrize("replacement", ["artifact", "public-key"])
def test_swapped_artifact_or_wrong_trusted_key_is_rejected(signed, replacement):
    if replacement == "artifact":
        signed.artifact.write_bytes(artifact.encrypt_package(b"another fixture")[0])
    else:
        wrong_public = ed25519.Ed25519PrivateKey.generate().public_key()
        signed.public.write_bytes(wrong_public.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        ))
    with pytest.raises((ValueError, OSError)):
        signing.verify_artifact(signed.artifact, signed.signature, signed.public)


@pytest.mark.parametrize("problem", ["missing", "truncated", "invalid"])
def test_missing_or_invalid_signature_fails_closed(signed, problem):
    if problem == "missing":
        signed.signature.unlink()
    elif problem == "truncated":
        signed.signature.write_bytes(signed.signature.read_bytes()[:-1])
    else:
        signature = bytearray(signed.signature.read_bytes())
        signature[0] ^= 1
        signed.signature.write_bytes(signature)
    with pytest.raises((ValueError, OSError)):
        signing.verify_artifact(signed.artifact, signed.signature, signed.public)


@pytest.mark.parametrize("key_type", ["private", "public"])
@pytest.mark.parametrize("problem", ["malformed", "wrong-algorithm"])
def test_key_files_must_be_valid_ed25519_pem(prepared, key_type, problem):
    if key_type == "public":
        signing.sign_artifact(prepared.artifact, prepared.private, prepared.signature)
    value = b"this is not a PEM key"
    if problem == "wrong-algorithm":
        wrong = x25519.X25519PrivateKey.generate()
        value = (wrong.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()) if key_type == "private" else
                 wrong.public_key().public_bytes(serialization.Encoding.PEM,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo))
    getattr(prepared, key_type).write_bytes(value)
    with pytest.raises((ValueError, OSError)):
        if key_type == "private":
            signing.sign_artifact(prepared.artifact, prepared.private, prepared.signature)
        else:
            signing.verify_artifact(prepared.artifact, prepared.signature, prepared.public)
    if key_type == "private":
        assert not prepared.signature.exists()


@pytest.mark.parametrize("existing", ["private", "public", "signature"])
def test_outputs_are_never_overwritten(paths, existing):
    occupied = getattr(paths, existing)
    if existing == "signature":
        signing.generate_key_pair(paths.private, paths.public)
        paths.artifact.write_bytes(artifact.encrypt_package(b"fixture")[0])
    occupied.write_bytes(b"preserve this file")
    with pytest.raises((ValueError, OSError)):
        if existing == "signature":
            signing.sign_artifact(paths.artifact, paths.private, paths.signature)
        else:
            signing.generate_key_pair(paths.private, paths.public)
    assert occupied.read_bytes() == b"preserve this file"
    if existing != "signature":
        assert not getattr(paths, "public" if existing == "private" else "private").exists()


@pytest.mark.parametrize("placement", ["project", "public-descendant"])
def test_private_key_cannot_be_written_into_publishable_locations(paths, tmp_path, monkeypatch, placement):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(signing, "PROJECT_ROOT", project)
    directory = project if placement == "project" else paths.public.parent / "nested"
    directory.mkdir(exist_ok=True)
    private_path = directory / "private.pem"
    with pytest.raises(ValueError):
        signing.generate_key_pair(private_path, paths.public)
    assert not private_path.exists() and not paths.public.exists()


@pytest.mark.parametrize("location", ["project", "signature-output"])
def test_signing_rejects_private_key_in_publishable_location(prepared, monkeypatch, location):
    signature_path = prepared.signature
    if location == "project":
        monkeypatch.setattr(signing, "PROJECT_ROOT", prepared.private.parent)
    else:
        signature_path = prepared.private.parent / "model.cml.sig"
    with pytest.raises(ValueError):
        signing.sign_artifact(prepared.artifact, prepared.private, signature_path)
    assert not signature_path.exists()


def test_keygen_requires_existing_parent_directories(paths):
    private = paths.private.parent / "missing" / "private.pem"
    with pytest.raises((ValueError, OSError)):
        signing.generate_key_pair(private, paths.public)
    assert not private.parent.exists() and not paths.public.exists()


@pytest.mark.parametrize("problem", ["empty", "bad-header", "oversized"])
def test_signing_rejects_invalid_envelope_before_creating_signature(prepared, monkeypatch, problem):
    if problem == "empty":
        prepared.artifact.write_bytes(b"")
    elif problem == "bad-header":
        prepared.artifact.write_bytes(b"not an artifact" * 4)
    else:
        monkeypatch.setattr(signing, "MAX_ARTIFACT_BYTES", prepared.artifact.stat().st_size - 1)
    with pytest.raises((ValueError, OSError)):
        signing.sign_artifact(prepared.artifact, prepared.private, prepared.signature)
    assert not prepared.signature.exists()


def test_verify_cli_success_receipt_and_failure_are_safe(signed, monkeypatch, capsys):
    arguments = ["signing", "verify", "--artifact", str(signed.artifact),
                 "--public-key", str(signed.public), "--signature", str(signed.signature)]
    monkeypatch.setattr(sys, "argv", arguments)
    signing.main()
    success = capsys.readouterr()
    assert isinstance(json.loads(success.out), dict)
    signed.signature.write_bytes(b"\x00" * 64)
    with pytest.raises(SystemExit) as caught:
        signing.main()
    failure = capsys.readouterr()
    assert caught.value.code == 1 and "error:" in failure.err
    combined = success.out + success.err + failure.out + failure.err
    assert "Traceback" not in combined and "BEGIN PRIVATE KEY" not in combined
    private = serialization.load_pem_private_key(signed.private.read_bytes(), password=None)
    assert private.private_bytes_raw().hex() not in combined
