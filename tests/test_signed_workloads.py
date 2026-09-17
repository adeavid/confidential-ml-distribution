"""Real cryptography and offline workload checks; Hub and CPU load are fixtures."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import artifact
import consumer
from model_demo import SOURCE_FILES
import producer
import signing


REPO = "test-owner/encrypted-demo"
REVISION = "b" * 40


@pytest.fixture
def signed_model(tmp_path):
    private, public, work = (tmp_path / name for name in ("private", "public", "work"))
    for directory in (private, public, work):
        directory.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.mkdir()
    for name in SOURCE_FILES:
        (source / name).write_bytes(f"fixture: {name}".encode())
    encrypted, key = artifact.encrypt_package(artifact.pack_model(source))
    key_path = private / "model.key"
    key_path.write_bytes(key)
    private_key, public_key = private / "signing.pem", public / "verify.pem"
    signing.generate_key_pair(private_key, public_key)
    ciphertext, signature = public / "model.cml", public / "model.cml.sig"
    ciphertext.write_bytes(encrypted)
    signing.sign_artifact(ciphertext, private_key, signature)
    return SimpleNamespace(work=work, source=source, key=key, key_path=key_path,
                           private_key=private_key, public_key=public_key,
                           encrypted=encrypted, signature=signature.read_bytes())


def mock_downloads(monkeypatch, model, *, problem=None, steps=None):
    def download(repo, revision, destination):
        assert (repo, revision) == (REPO, REVISION)
        data = model.encrypted
        if problem == "tampered-artifact":
            data = data[:-1] + bytes([data[-1] ^ 1])
        destination.write_bytes(data)
        if steps is not None:
            steps.append("download-artifact")

    def download_signature(repo, revision, destination):
        assert (repo, revision) == (REPO, REVISION)
        if problem == "missing-signature":
            raise FileNotFoundError("Signature absent at pinned revision.")
        data = model.signature
        if problem == "invalid-signature":
            data = bytes([data[0] ^ 1]) + data[1:]
        destination.write_bytes(data)
        if steps is not None:
            steps.append("download-signature")

    monkeypatch.setattr(consumer, "download_artifact", download)
    monkeypatch.setattr(consumer, "download_signature", download_signature)


@pytest.mark.parametrize("replace_after_verify", [False, True])
def test_signed_consumer_orders_checks_and_decrypts_verified_buffer(signed_model, monkeypatch, replace_after_verify):
    model, steps = signed_model, []
    mock_downloads(monkeypatch, model, steps=steps)
    real_decrypt, real_read = consumer.decrypt_package, consumer._read_regular

    def verify(path, signature, public_key):
        assert public_key == model.public_key
        verified = signing.verify_artifact(path, signature, public_key)
        steps.append("verify")
        if replace_after_verify:
            path.write_bytes(b"replaced after successful signature verification")
        return verified

    def read_key(*args, **kwargs):
        assert steps[-1] == "verify"
        steps.append("read-aes")
        return real_read(*args, **kwargs)

    def decrypt(data, key):
        assert data == model.encrypted
        assert steps[-1] == "read-aes"
        steps.append("decrypt")
        return real_decrypt(data, key)

    def load(directory, cache):
        assert steps[-1] == "decrypt"
        assert {p.name for p in directory.iterdir()} == set(artifact.PACKAGE_FILES)
        steps.append("load")
        return {"device": "cpu", "finite_output": True}

    monkeypatch.setattr(consumer, "verify_artifact", verify)
    monkeypatch.setattr(consumer, "_read_regular", read_key)
    monkeypatch.setattr(consumer, "decrypt_package", decrypt)
    monkeypatch.setattr(consumer, "_load_offline", load)
    monkeypatch.setattr(consumer, "decrypt_model", Mock(side_effect=AssertionError("Unsigned fallback")))
    report = consumer.run_consumer(REPO, REVISION, model.key_path, model.work,
                                   require_signature=True, public_key_path=model.public_key)
    assert report["signature_verified"] is True
    assert steps == ["download-artifact", "download-signature", "verify", "read-aes", "decrypt", "load"]
    assert list(model.work.iterdir()) == []


@pytest.mark.parametrize("problem", ["missing-signature", "invalid-signature", "wrong-public-key", "tampered-artifact"])
def test_signature_failure_never_reads_aes_decrypts_extracts_or_loads(signed_model, monkeypatch, problem):
    model = signed_model
    mock_downloads(monkeypatch, model, problem=problem)
    if problem == "wrong-public-key":
        wrong_private = model.private_key.with_name("wrong.pem")
        wrong_public = model.public_key.with_name("wrong.pem")
        signing.generate_key_pair(wrong_private, wrong_public)
        model.public_key = wrong_public
    guards = []
    for name in ("_read_regular", "decrypt_package", "decrypt_model", "extract_package", "_load_offline"):
        guard = Mock(side_effect=AssertionError(f"Signature failure reached {name}"))
        guards.append(guard)
        monkeypatch.setattr(consumer, name, guard)
    with pytest.raises((ValueError, OSError)):
        consumer.run_consumer(REPO, REVISION, model.key_path, model.work,
                              require_signature=True, public_key_path=model.public_key)
    for guard in guards:
        guard.assert_not_called()
    assert list(model.work.iterdir()) == []


def test_valid_signature_does_not_bypass_gcm(signed_model, monkeypatch):
    model = signed_model
    mock_downloads(monkeypatch, model)
    model.key_path.write_bytes(bytes([model.key[0] ^ 1]) + model.key[1:])
    verified = Mock(wraps=consumer.verify_artifact)
    load = Mock(side_effect=AssertionError("Wrong AES reached model loading"))
    monkeypatch.setattr(consumer, "verify_artifact", verified)
    monkeypatch.setattr(consumer, "_load_offline", load)
    with pytest.raises(ValueError, match="Artifact authentication failed"):
        consumer.run_consumer(REPO, REVISION, model.key_path, model.work,
                              require_signature=True, public_key_path=model.public_key)
    verified.assert_called_once()
    load.assert_not_called()


@pytest.mark.parametrize("required,public", [(True, None), (False, "public.pem")])
def test_incomplete_signature_mode_fails_before_network(signed_model, monkeypatch, required, public):
    download = Mock()
    monkeypatch.setattr(consumer, "download_artifact", download)
    with pytest.raises(ValueError, match="requires both"):
        consumer.run_consumer(REPO, REVISION, signed_model.key_path, signed_model.work,
                              require_signature=required, public_key_path=public)
    download.assert_not_called()


def test_signed_producer_passes_only_ciphertext_and_signature_to_publication(signed_model, monkeypatch):
    model = signed_model

    def download(destination):
        destination.mkdir()
        for name in SOURCE_FILES:
            (destination / name).write_bytes((model.source / name).read_bytes())

    def publish(repo, artifact_path, signature_path):
        assert repo == REPO
        assert {p.name for p in artifact_path.parent.iterdir()} == {"model.cml", "model.cml.sig"}
        verified = signing.verify_artifact(artifact_path, signature_path, model.public_key)
        assert artifact.decrypt_package(verified, model.key) == artifact.pack_model(model.source)
        return {"repo_id": repo, "revision": REVISION, "filename": "model.cml", "signature_filename": "model.cml.sig"}

    monkeypatch.setattr(producer, "download_source", download)
    monkeypatch.setattr(producer, "publish_artifact", publish)
    report = producer.run_producer(REPO, model.key_path, model.work, signing_key_path=model.private_key)
    assert report["signature_filename"] == "model.cml.sig"
    assert list(model.work.iterdir()) == []


def test_signing_failure_never_publishes_unsigned(signed_model, monkeypatch):
    model = signed_model
    monkeypatch.setattr(producer, "download_source", lambda destination: None)
    monkeypatch.setattr(producer, "pack_model", lambda source: b"fixture")
    monkeypatch.setattr(producer, "sign_artifact", Mock(side_effect=ValueError("Invalid signing key")))
    publish = Mock()
    monkeypatch.setattr(producer, "publish_artifact", publish)
    with pytest.raises(ValueError, match="Invalid signing key"):
        producer.run_producer(REPO, model.key_path, model.work, signing_key_path=model.private_key)
    publish.assert_not_called()
    assert list(model.work.iterdir()) == []
