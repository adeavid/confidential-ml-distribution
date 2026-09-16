"""Offline local-loading checks; an opt-in check uses the real downloaded model."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest

import model_demo


@pytest.fixture(scope="session")
def checkpoint(tmp_path_factory):
    """Create real, tiny safetensors weights without contacting Hugging Face."""
    import torch
    from transformers import BertConfig, BertForPreTraining

    directory = tmp_path_factory.mktemp("tiny-checkpoint")
    vocabulary = [
        "[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]",
        "the", "capital", "of", "france", "is", ".", "paris",
    ]
    torch.manual_seed(7)
    torch.set_num_threads(1)
    config = BertConfig(
        vocab_size=len(vocabulary), hidden_size=16, num_hidden_layers=1,
        num_attention_heads=2, intermediate_size=32, max_position_embeddings=64,
        pad_token_id=0,
    )
    BertForPreTraining(config).save_pretrained(directory)
    (directory / "vocab.txt").write_text("\n".join(vocabulary) + "\n")
    return directory


@pytest.fixture
def local_model(tmp_path, checkpoint):
    return Path(shutil.copytree(checkpoint, tmp_path / "model"))


@pytest.fixture
def forbid_model_loading(monkeypatch):
    """Make preflight tests fail if they reach either Transformers loader."""
    from transformers import AutoModelForPreTraining, AutoTokenizer

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid local input reached a Transformers loader.")

    monkeypatch.setattr(AutoTokenizer, "from_pretrained", forbidden)
    monkeypatch.setattr(AutoModelForPreTraining, "from_pretrained", forbidden)


@pytest.mark.parametrize("filename", model_demo.REQUIRED_FILES)
@pytest.mark.parametrize("problem", ["missing", "empty", "symlink"])
def test_unsafe_required_file_fails_before_loading(
    local_model, tmp_path, filename, problem, forbid_model_loading
):
    path = local_model / filename
    if problem == "missing":
        path.unlink()
    elif problem == "empty":
        path.write_bytes(b"")
    else:
        target = tmp_path / filename
        path.rename(target)
        path.symlink_to(target)
    with pytest.raises(ValueError, match=re.escape(filename)):
        model_demo.run_local(local_model)


def test_pickle_weights_are_not_a_fallback(local_model, forbid_model_loading):
    (local_model / "model.safetensors").unlink()
    (local_model / "pytorch_model.bin").write_bytes(b"not safe weights")
    with pytest.raises(ValueError, match="model.safetensors"):
        model_demo.run_local(local_model)


@pytest.mark.parametrize("problem", ["missing", "symlink"])
def test_invalid_directory_fails_before_loading(
    tmp_path, local_model, problem, forbid_model_loading
):
    directory = tmp_path / "invalid-directory"
    if problem == "symlink":
        directory.symlink_to(local_model, target_is_directory=True)
    with pytest.raises(ValueError, match="existing local directory"):
        model_demo.run_local(directory)


@pytest.mark.parametrize(
    "configuration",
    [
        "{", "[]",
        '{"model_type": "gpt2"}',
        '{"model_type": "bert", "auto_map": {"AutoModel": "evil.Model"}}',
    ],
)
def test_invalid_or_custom_configuration_is_rejected(
    local_model, configuration, forbid_model_loading
):
    (local_model / "config.json").write_text(configuration)
    with pytest.raises(ValueError):
        model_demo.run_local(local_model)


@pytest.mark.parametrize("problem", ["corrupt", "incomplete"])
def test_bad_checkpoint_fails_before_forward(local_model, monkeypatch, problem):
    from safetensors.torch import load_file, save_file
    from transformers import BertForPreTraining

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid weights reached the forward pass.")

    monkeypatch.setattr(BertForPreTraining, "forward", forbidden)
    weights_path = local_model / "model.safetensors"
    if problem == "corrupt":
        weights_path.write_bytes(b"truncated")
        message = "Invalid safetensors weights"
    else:
        weights = load_file(weights_path)
        del weights["bert.encoder.layer.0.attention.self.query.weight"]
        save_file(weights, weights_path, metadata={"format": "pt"})
        message = "Checkpoint did not load completely: missing_keys"
    with pytest.raises(ValueError, match=message):
        model_demo.run_local(local_model)


def test_transformers_loaders_receive_only_local_safe_options(local_model):
    from transformers import AutoModelForPreTraining, AutoTokenizer

    with (
        patch.object(AutoTokenizer, "from_pretrained", wraps=AutoTokenizer.from_pretrained) as tokenizer,
        patch.object(AutoModelForPreTraining, "from_pretrained", wraps=AutoModelForPreTraining.from_pretrained) as model,
    ):
        model_demo.run_local(local_model)
    for loader in (tokenizer, model):
        assert loader.call_count == 1
        assert loader.call_args.args == (local_model.resolve(),)
        kwargs = loader.call_args.kwargs
        assert kwargs["local_files_only"] is True
        assert kwargs["trust_remote_code"] is False
        assert kwargs["token"] is False
    assert model.call_args.kwargs["use_safetensors"] is True
    assert model.call_args.kwargs["weights_only"] is True


@pytest.fixture
def fake_hub(tmp_path, checkpoint, monkeypatch):
    """A local file provider for download-control tests, not integration evidence."""
    import huggingface_hub

    source = Path(shutil.copytree(checkpoint, tmp_path / "hub-fixture"))
    (source / "README.md").write_text("Test checkpoint; no network involved.\n")
    digest = hashlib.sha256((source / "model.safetensors").read_bytes()).hexdigest()
    monkeypatch.setattr(model_demo, "WEIGHTS_SHA256", digest)
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        return str(source / kwargs["filename"])

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    return source, calls


def test_download_uses_explicit_pinned_anonymous_requests(tmp_path, fake_hub):
    source, calls = fake_hub
    destination = tmp_path / "downloads" / "model"
    result = model_demo.download_source(destination)
    assert re.fullmatch(r"[0-9a-f]{40}", model_demo.SOURCE_REVISION)
    assert [call["filename"] for call in calls] == list(model_demo.SOURCE_FILES)
    for call in calls:
        assert call["repo_id"] == model_demo.SOURCE_REPO
        assert call["revision"] == model_demo.SOURCE_REVISION
        assert call["token"] is False
        assert not Path(call["cache_dir"]).exists()
    assert sorted(path.name for path in destination.iterdir()) == sorted(model_demo.SOURCE_FILES)
    for filename in model_demo.SOURCE_FILES:
        assert (destination / filename).read_bytes() == (source / filename).read_bytes()
        assert not (destination / filename).is_symlink()
    assert result["source_revision"] == model_demo.SOURCE_REVISION
    assert result["files"] == list(model_demo.SOURCE_FILES)
    assert result["total_bytes"] == sum((destination / name).stat().st_size for name in model_demo.SOURCE_FILES)


@pytest.mark.parametrize("problem", ["interrupted", "digest"])
def test_failed_download_leaves_no_partial_destination(tmp_path, fake_hub, monkeypatch, problem):
    import huggingface_hub

    download = huggingface_hub.hf_hub_download
    _, attempts = fake_hub

    def interrupted(**kwargs):
        if len(attempts) == 1:
            raise OSError("Simulated download interruption")
        return download(**kwargs)

    if problem == "interrupted":
        monkeypatch.setattr(huggingface_hub, "hf_hub_download", interrupted)
        error, message = OSError, "Simulated download interruption"
    else:
        monkeypatch.setattr(model_demo, "WEIGHTS_SHA256", "0" * 64)
        error, message = ValueError, "pinned SHA-256"
    destination = tmp_path / "downloads" / "model"
    with pytest.raises(error, match=message):
        model_demo.download_source(destination)
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []
    if problem == "interrupted":
        assert len(attempts) == 1


# This guard observes Python socket APIs; it is not operating-system isolation.
# It is installed before importing model_demo, torch, or Transformers.
SOCKET_GUARDED_LOAD = r'''
import json
from pathlib import Path
import socket
import sys

attempts = []
def block(name):
    def denied(*args, **kwargs):
        attempts.append(name)
        raise RuntimeError("Python socket access blocked: " + name)
    return denied

for name in ("create_connection", "getaddrinfo"):
    setattr(socket, name, block(name))
for name in ("connect", "connect_ex", "sendto", "sendmsg"):
    if hasattr(socket.socket, name):
        setattr(socket.socket, name, block(name))

# Prove that the guard is active, then exclude this deliberate probe from counts.
try:
    socket.create_connection(("example.invalid", 443))
except RuntimeError as error:
    assert "Python socket access blocked" in str(error)
else:
    raise AssertionError("Socket guard did not block the probe.")
assert attempts == ["create_connection"]
attempts.clear()

cache = Path(sys.argv[3])
assert cache.is_dir() and not list(cache.iterdir()), "HF cache must start empty"
sys.path.insert(0, sys.argv[2])
from model_demo import run_local
result = run_local(Path(sys.argv[1]))
assert attempts == [], "Local load attempted Python socket networking: " + repr(attempts)
print(json.dumps({"result": result, "python_socket_attempts": attempts}))
'''


def socket_guarded_load(model_directory, tmp_path):
    """Run in a new interpreter and new caches, checking Python socket attempts."""
    hf_home = tmp_path / "empty-hf-home"
    hf_home.mkdir()
    environment = os.environ.copy()
    environment.update({
        "HF_HOME": str(hf_home),
        "HF_HUB_CACHE": str(hf_home / "hub"),
        "HUGGINGFACE_HUB_CACHE": str(hf_home / "hub"),
        "HF_XET_CACHE": str(hf_home / "xet"),
        "HF_MODULES_CACHE": str(hf_home / "modules"),
        "TORCH_HOME": str(tmp_path / "empty-torch-home"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "TRANSFORMERS_CACHE"):
        environment.pop(name, None)
    process = subprocess.run(
        [sys.executable, "-c", SOCKET_GUARDED_LOAD, str(model_directory),
         str(Path(model_demo.__file__).parent), str(hf_home)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout.strip().splitlines()[-1])
    assert result["python_socket_attempts"] == []
    assert result["result"]["device"] == "cpu"
    assert result["result"]["finite_output"] is True
    return result["result"]


def test_tiny_checkpoint_loads_with_empty_cache_and_python_socket_guard(local_model, tmp_path):
    result = socket_guarded_load(local_model, tmp_path)
    assert result["output_shape"][-1] == 12


@pytest.mark.skipif(not os.environ.get("MODEL_DEMO_TEST_MODEL"), reason="Real model path not supplied")
def test_real_model_loads_with_empty_cache_and_python_socket_guard(tmp_path):
    directory = Path(os.environ["MODEL_DEMO_TEST_MODEL"]).expanduser().resolve()
    result = socket_guarded_load(directory, tmp_path)
    assert result["model_class"] == "BertForPreTraining"
    assert result["output_shape"] == [1, 9, 30522]
