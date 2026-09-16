"""Download one pinned BERT checkpoint, then load it from local files only."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory


SOURCE_REPO = "google/bert_uncased_L-2_H-128_A-2"
SOURCE_REVISION = "30b0a37ccaaa32f332884b96992754e246e48c5f"
REQUIRED_FILES = ("config.json", "model.safetensors", "vocab.txt")
SOURCE_FILES = (*REQUIRED_FILES, "README.md")
WEIGHTS_SHA256 = "7fb69ad9f6866d8983183c930e33828f326470bf6ad8bbb2ad4ed957a92e9414"
DEMO_TEXT = "The capital of France is [MASK]."


def download_source(destination: Path) -> dict:
    """Use an explicit file list; expose the destination only after success."""
    from huggingface_hub import hf_hub_download

    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Download destination must not already exist.")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(dir=destination.parent, prefix=".source-") as temporary:
        staging = Path(temporary) / "model"
        staging.mkdir()
        for filename in SOURCE_FILES:
            cached_file = hf_hub_download(
                repo_id=SOURCE_REPO,
                revision=SOURCE_REVISION,
                filename=filename,
                cache_dir=Path(temporary) / "cache",
                token=False,
            )
            shutil.copyfile(cached_file, staging / filename)

        with (staging / "model.safetensors").open("rb") as weights:
            if hashlib.file_digest(weights, "sha256").hexdigest() != WEIGHTS_SHA256:
                raise ValueError("Source weights do not match the pinned SHA-256.")
        total_bytes = sum((staging / name).stat().st_size for name in SOURCE_FILES)
        staging.rename(destination)

    return {
        "source_repo": SOURCE_REPO,
        "source_revision": SOURCE_REVISION,
        "files": list(SOURCE_FILES),
        "total_bytes": total_bytes,
    }


def run_local(directory: Path) -> dict:
    """Validate a concrete folder, load safe weights, and run one CPU forward pass."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Model directory must be an existing local directory, not a symlink.")
    directory = directory.resolve()
    for filename in REQUIRED_FILES:
        path = directory / filename
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing or unsafe required model file: {filename}")

    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("model_type") != "bert" or config.get("auto_map"):
        raise ValueError("This PoC supports built-in BERT models without custom code only.")

    # Imports follow preflight so incomplete input fails before model loading.
    import torch
    from safetensors import SafetensorError
    from transformers import AutoModelForPreTraining, AutoTokenizer

    torch.set_num_threads(1)
    tokenizer = AutoTokenizer.from_pretrained(
        directory,
        do_lower_case=True,
        local_files_only=True,
        trust_remote_code=False,
        token=False,
    )
    try:
        model, loading_info = AutoModelForPreTraining.from_pretrained(
            directory,
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
            weights_only=True,
            output_loading_info=True,
            token=False,
        )
    except SafetensorError as error:
        raise ValueError("Invalid safetensors weights.") from error
    # Do not mistake randomly initialized or incompatible weights for a full load.
    for field in ("missing_keys", "unexpected_keys", "mismatched_keys", "error_msgs"):
        if loading_info.get(field):
            raise ValueError(f"Checkpoint did not load completely: {field}")

    model.to("cpu").eval()
    inputs = tokenizer(DEMO_TEXT, return_tensors="pt", truncation=True, max_length=32)
    with torch.inference_mode():
        output = model(**inputs)
    if not torch.isfinite(output.prediction_logits).all().item():
        raise ValueError("Model produced non-finite output.")

    return {
        "model_class": type(model).__name__,
        "device": str(next(model.parameters()).device),
        "parameter_count": sum(p.numel() for p in model.parameters()),
        "input_tokens": inputs["input_ids"].shape[1],
        "output_shape": list(output.prediction_logits.shape),
        "finite_output": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("download", "load"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        result = download_source(args.directory) if args.action == "download" else run_local(args.directory)
    except (ValueError, OSError, RuntimeError) as error:
        parser.exit(1, f"error: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
