# Optional local checks

[Back to the demo](../README.md#run-the-demo)

These are diagnostics, not prerequisites for the Kubernetes Jobs. Run them from
the repository root after the README dependency setup. All output directories,
artifact paths and private-key paths must be new; use fresh names when repeating.

## Download and load the source

Source: [Google BERT Tiny](https://huggingface.co/google/bert_uncased_L-2_H-128_A-2/tree/30b0a37ccaaa32f332884b96992754e246e48c5f),
revision `30b0a37ccaaa32f332884b96992754e246e48c5f`, public and requiring no token.
`src/model_demo.py` downloads only `config.json`, `model.safetensors` (17.7 MB),
`vocab.txt`, and the original `README.md`. The model card declares Apache-2.0.
Packaging preserves that card and includes `licenses/model-APACHE-2.0.txt` as
`LICENSE`, copied from the [official Apache text](https://www.apache.org/licenses/LICENSE-2.0.txt).

After the README dependency setup, download to a new local directory:

```bash
uv run --frozen python src/model_demo.py download runtime/source-model
uv run --frozen python src/model_demo.py load runtime/source-model
```

The loader requires local configuration, vocabulary, and safetensors files.
`AutoTokenizer` explicitly lowercases input. `AutoModelForPreTraining` loads the
complete BERT checkpoint, including both pretraining heads; a base `AutoModel`
would discard those heads. Missing, unexpected, or mismatched weights fail loading.
Both loaders use `local_files_only=True` and `trust_remote_code=False`; weights use
`use_safetensors=True` and `weights_only=True`. No source-model download fallback exists.
A finite CPU forward pass proves operational loading, not prediction quality.

## Encrypted round trip

After the source download, run from the repository root. Commands pass a key
**file path**, never key bytes. All output paths must be new; use new names for
another encryption. This local CLI generates a new key for each encryption.

```bash
export MODEL_DEMO_KEY_DIR="$HOME/.config/confidential-ml-distribution/keys"
install -d -m 700 "$MODEL_DEMO_KEY_DIR" artifacts runtime
uv run --frozen python src/artifact.py encrypt \
  --source runtime/source-model --artifact artifacts/model.cml \
  --key-file "$MODEL_DEMO_KEY_DIR/model-v1.key"
uv run --frozen python src/artifact.py decrypt \
  --artifact artifacts/model.cml --key-file "$MODEL_DEMO_KEY_DIR/model-v1.key" \
  --destination runtime/decrypted-model && \
uv run --frozen python src/model_demo.py load runtime/decrypted-model
```

The key is a raw 32-byte file created exclusively with mode `0600`, outside the
project, source, and artifact directories. Decryption accepts a regular key file
via a symlink, as used by Kubernetes Secret volumes. This standalone CLI does not
provision a Secret. Outputs are operator-controlled local paths; do not use shared
writable parent directories. Interrupted two-file creation is not a crash-safe
transaction: inspect leftovers and use fresh paths after an interruption.

## Signature commands

These standalone commands exercise signing without a cluster. The [signed
Kubernetes demo](../README.md#5-run-layer-2) provisions its own keys and calls the same signing code;
these local commands are optional diagnostics, not setup prerequisites.

After creating `artifacts/model.cml` above, run from the repository root:

```bash
export MODEL_DEMO_SIGNING_DIR="$HOME/.config/confidential-ml-distribution/signing"
install -d -m 700 "$MODEL_DEMO_SIGNING_DIR" runtime/signing
uv run --frozen python src/signing.py keygen \
  --private-key "$MODEL_DEMO_SIGNING_DIR/producer-v1.pem" \
  --public-key runtime/signing/producer-v1.public.pem
uv run --frozen python src/signing.py sign \
  --artifact artifacts/model.cml \
  --private-key "$MODEL_DEMO_SIGNING_DIR/producer-v1.pem" \
  --signature artifacts/model.cml.sig
uv run --frozen python src/signing.py verify \
  --artifact artifacts/model.cml \
  --signature artifacts/model.cml.sig \
  --public-key runtime/signing/producer-v1.public.pem
uv run --frozen pytest -q tests/test_signing.py
```

Output paths must be new; use fresh names for another run. Success prints only
JSON metadata. Failure exits 1. Verification neither decrypts nor loads a model.

## Load with Docker networking disabled

Build the Consumer image using the README before this check.

After the standalone decrypt step, this checks the real model using the Consumer
image, an empty memory-backed working directory, and `--network none`:
Use `model-consumer:layer2` instead of `model-consumer:layer1` below if you
built only the Layer 2 tags.

```bash
COPYFILE_DISABLE=1 tar -C runtime/decrypted-model -cf - . | \
docker run --rm -i --network none --read-only \
  --tmpfs /work:rw,nosuid,nodev,noexec,uid=10001,gid=10001,mode=700,size=256m \
  -e TMPDIR=/work -e HF_HOME=/work/cache -e HF_HUB_OFFLINE=1 \
  -e TRANSFORMERS_OFFLINE=1 -e HF_HUB_DISABLE_PROGRESS_BARS=1 \
  --entrypoint sh model-consumer:layer1 \
  -c 'mkdir /work/model && tar -xf - -C /work/model && python /app/src/model_demo.py load /work/model'
```

`COPYFILE_DISABLE=1` avoids macOS archive metadata. This load-only check complements
the Python socket guard and does not require network access to the source model.

## Standalone Hub commands

The Kubernetes Producer and Consumer use the same `src/hub_artifact.py` code.
For manual diagnostics, run `uv run --frozen python src/hub_artifact.py --help`.
`publish --signature FILE` publishes artifact and signature together. Publication
requires your own authorized test repository and local HF login; it is not an
offline test. Never publish keys, tokens, or a decrypted model directory.
