# Confidential ML Model Distribution PoC

A reproducible proof of concept for encrypted and authenticated ML model distribution.

**Current status: Layers 1 and 2 implemented and verified on local kind/Linux ARM64.**
Producer published a real encrypted artifact; a fresh Consumer Pod downloaded
that revision, decrypted it through a mounted Secret, and loaded it on CPU.
The signed run also passed; wrong AES and verification keys failed closed.
Layer 2 verifies Ed25519 signatures before decryption. See the
[Kubernetes demo](#kubernetes-demo) and [observed results](#verification).

## Scope and acceptance

The assessment PDF is the source of requirements. Only Layer 1 is mandatory;
Layers 2 and 3 are optional and independent. Validate Layer 1 before adding Layer 2.
Evaluate Layer 3 afterwards only if the environment and time allow.

| Requirement | Implementation / plan | Acceptance evidence | Status |
| --- | --- | --- | --- |
| L1: select and encrypt a small open model | Pinned BERT; bounded ZIP and AES-256-GCM | Real round trip and CPU load; negative tests | Passed |
| L1: publish encrypted artifact on HF Hub | Producer commits one file to the public test repository | Full commit and anonymous retrieval | Passed in Kubernetes |
| L1: deliver key through a Kubernetes Secret | Bootstrap provisions Secret; Consumer mounts it read-only | Fresh Pod reads key without Kubernetes API credentials | Passed |
| L1: download, decrypt, load in Kubernetes | Consumer Job uses pinned revision and decrypted local directory | Fresh Job completes; wrong-key Job fails before loading | Passed |
| L2: sign and verify before decryption | Ed25519 over complete artifact; controlled public-key ConfigMap | Offline call-order checks; real signed run and wrong-public-key rejection | Passed in Kubernetes |
| Delivery: public Git repo, Dockerfiles, manifests, README | Pinned dependencies, container builds and bootstrap | Isolated checkout checks and recorded Hub/Kubernetes runs | Included; second-machine repetition pending |

AES-GCM, Ed25519, kind, and Jobs are project choices. A CPU forward pass is extra
evidence of an operational model; loading itself is the formal requirement.

## Architecture

Producer packages and encrypts the model, then publishes ciphertext to Hugging Face.
Consumer downloads that revision, decrypts with its mounted AES key, and loads
locally. Layer 2 adds a signature to the publication and requires verification
with a separately provisioned public key before reading AES or decrypting.

`scripts/run_layer1.py` runs on the operator's machine with explicit Kubernetes
credentials. It provisions the key before Producer, reads Producer's reported
artifact commit, and supplies that commit to Consumer through `demo-settings`.
Neither workload calls the Kubernetes API.

- **Pod:** Kubernetes' smallest deployable unit, grouping containers with their
  network and storage. Our Consumer Pod runs Python and mounts the key file.
- **Job:** creates a Pod for a task that finishes. Loading and checking a model
  does not require a continuously running service.
- **Secret:** holds the AES key separately from the image and public artifact.
  Base64 is encoding, not encryption; cluster access and storage protection matter.

Layer 1 separates artifact access from key access. Someone holding only the
ciphertext cannot recover its contents, assuming sound encryption and key handling.
The original model is public: this PoC protects our packaged copy, not that original.
We trust Producer, the bootstrap operator, Consumer configuration, and host/cluster
administrators. A privileged administrator may read the key or decrypted model.

## Local model demo

Prerequisites: Python **3.12.14** available as `python3.12`, and uv **0.8.17**;
this combination was tested on macOS ARM64. The interpreter must already be
installed: this uv version cannot automatically download that Python patch.
Install uv from its [official release](https://github.com/astral-sh/uv/releases/tag/0.8.17).
`pyproject.toml` pins direct dependencies; `uv.lock` pins the resolved environment.
Linux selects the PyTorch CPU index. The container demo was tested on Linux ARM64
with CPU-only PyTorch; other platforms have not been executed here.

Source: [Google BERT Tiny](https://huggingface.co/google/bert_uncased_L-2_H-128_A-2/tree/30b0a37ccaaa32f332884b96992754e246e48c5f),
revision `30b0a37ccaaa32f332884b96992754e246e48c5f`, public and requiring no token.
`src/model_demo.py` downloads only `config.json`, `model.safetensors` (17.7 MB),
`vocab.txt`, and the original `README.md`. The model card declares Apache-2.0.
Packaging preserves that card and includes `licenses/model-APACHE-2.0.txt` as
`LICENSE`, copied from the [official Apache text](https://www.apache.org/licenses/LICENSE-2.0.txt).

Clone the repository into any parent directory you choose. `$HOME` in the commands
means your own home directory; no personal absolute paths are required.

```bash
git clone https://github.com/adeavid/confidential-ml-distribution.git
cd confidential-ml-distribution
uv sync --frozen --python python3.12
uv run --frozen python src/model_demo.py download runtime/source-model
```

The destination must not exist. Download uses the full source revision and verifies
the weights' pinned SHA-256. It copies regular files from a temporary cache into
the final folder, then removes that cache. Repeat `load` without downloading again:

```bash
uv run --frozen python src/model_demo.py load runtime/source-model
```

The loader requires local configuration, vocabulary, and safetensors files.
`AutoTokenizer` explicitly lowercases input. `AutoModelForPreTraining` loads the
complete BERT checkpoint, including both pretraining heads; a base `AutoModel`
would discard those heads. Missing, unexpected, or mismatched weights fail loading.
Both loaders use `local_files_only=True` and `trust_remote_code=False`; weights use
`use_safetensors=True` and `weights_only=True`. No source-model download fallback exists.
A finite CPU forward pass proves operational loading, not prediction quality.

### Tests

```bash
uv run --frozen pytest -q
MODEL_DEMO_TEST_MODEL=runtime/source-model uv run --frozen pytest -q
```

`src/` contains application code. `tests/` uses small local fixtures for fast checks
without external services. The second command additionally tests the downloaded
real checkpoint and its encrypted round trip in separate processes with empty caches and a Python socket
guard. The guard detects attempted Python socket networking; it does not enforce
OS-level isolation or cover native networking. Fixture tests do not demonstrate
Hugging Face or Kubernetes integration.

## Local encrypted round trip

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

### Format and decisions

```text
Public header / AAD (17 bytes) = CMLD (4) + version 1 (1) + nonce (12)
Artifact = header + ciphertext + GCM tag (16 bytes)
```

`artifact.py` uses `cryptography==50.0.1`'s high-level `AESGCM`. The local CLI creates
a fresh 256-bit key per encryption; the Kubernetes bootstrap creates one fresh key
per run and Producer reads it from the Secret. Every encryption generates a random
12-byte nonce. The entire header is AAD
(authenticated associated data). The library appends and checks the full tag.
Never reuse a nonce with the same key. Wrong keys, changed nonces/ciphertext/tags,
and truncation fail before ZIP parsing, extraction, or model loading; the tag
cannot identify which of these caused authentication to fail.

AES-GCM combines confidentiality and integrity in one supported API. AES-CBC
with a separate MAC would require more composition and padding logic;
ChaCha20-Poly1305 is also a valid alternative. AES-GCM does not authenticate a
unique producer against other holders of the AES key; Layer 2 adds that.

The encrypted payload is a **stored ZIP with no compression**, containing exactly
the four source files and `LICENSE`. Names, duplicates, file types, sizes, CRC,
and compression flags are checked; extraction writes fixed flat names without
`extractall`. Links and other entry types are rejected. Only a fully extracted
package becomes the destination; directories use `0700` and files `0600`.

Limits: 32 MiB per file, 64 MiB for the complete ZIP, and 64 MiB + 33 bytes for
the artifact. Full-buffer encryption is practical for this roughly 18 MB model;
several copies coexist, so these limits are **not** a total RAM bound. Large
models would require a separately designed streaming format. The header, artifact
size, and any externally published filenames/revisions are public; model files
and their internal names are encrypted. Python buffers and local plaintext are
not securely erased. Host administrators remain trusted.

API reference: [cryptography 50.0.1 AESGCM](https://cryptography.io/en/50.0.1/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM).

## Local signature commands

These standalone commands exercise signing without a cluster. The signed
Kubernetes demo below provisions its own keys and calls the same signing code;
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

**Project choices:** [Ed25519 in cryptography 50.0.1](https://cryptography.io/en/50.0.1/hazmat/primitives/asymmetric/ed25519/)
provides a fixed 64-byte signature and a simple API; this PoC has no requirement
to interoperate with RSA-based systems. We sign the exact complete artifact bytes,
including the public header, nonce, ciphertext and GCM tag, without a custom
prehash or serialization. Keys use standard PEM: PKCS8 for private keys and
SubjectPublicKeyInfo for public keys. Only Ed25519 keys are accepted.

The signing private key is separate from the AES key and HF token. Key generation
creates files exclusively with mode `0600`; private keys must stay outside the
project and public output directories. The private PEM is **not password-encrypted**:
use a private parent directory and a trusted host. Do not upload that directory.
Mounted key-file symlinks are supported, while artifact/signature inputs must be
regular files without symlinks. Reads are bounded, including 4 KiB per PEM file.

The operator supplies the trusted public-key file independently. Its secrecy is
unnecessary, but preventing unauthorized replacement is essential. Accepting an
attacker's replacement public key would allow their signatures to pass. A valid
signature identifies possession of the corresponding private key; it does not
guarantee model safety, freshness or confidentiality after an AES-key leak.

A legitimately signed old release still has a valid signature. Pinning the expected
commit controls the selected version and makes the run reproducible; it is not a
complete revocation or anti-rollback system.

`verify_artifact()` returns the exact verified ciphertext buffer. Consumer
decrypts that buffer instead of reading the file again, so a replaced file
cannot bypass the earlier check. AES-GCM and package validation
remain mandatory after signature verification, with no fallback to unsigned
Layer 1 when a Layer 2 signature is absent or invalid.

## Hub publication and retrieval

`src/hub_artifact.py` also exposes standalone `publish`, `download` and `download-signature` commands
for local diagnostics; use `--help` for their arguments. The Kubernetes demo below
runs this same code. `publish --signature FILE` adds a detached signature in the
same commit as the encrypted artifact. The explicit upload list contains only
`model.cml` and, in Layer 2, `model.cml.sig`; unexpected repository contents are
rejected. A parent-commit check prevents silently
publishing over another writer's change; conflicts are not automatically retried.

Publishing Layer 1 after Layer 2 removes any stale signature in that new commit.
Earlier pinned revisions remain unchanged. The model, keys and tokens are never
uploaded as plaintext. Anonymous retrieval pins the full artifact commit,
uses a fresh temporary cache, and checks metadata size, downloaded size and header.
Layer 2 retrieves both files from that exact commit and verifies the signature
before AES-GCM authentication/decryption. Hub metadata is trusted over HTTPS;
SDK mocks are not evidence of integration. Source-model and artifact commits identify
files in different repositories. Always retain the key matching the artifact revision.

## Local development cluster

Docker Desktop supplies Linux on macOS; kind creates a container acting as our
single Kubernetes node. `kubectl` uses the endpoint and credentials in kubeconfig
to communicate with it. One local node avoids cloud cost and credentials.
Docker Desktop and `kubectl` must already be installed and available on `PATH`.

Tested tooling: Docker Engine **29.1.3**, kind **v0.33.0**, Kubernetes **v1.34.11**,
and kubectl **v1.34.1**. kind and the node image digest in `k8s/kind.yaml` are pinned.

### Install kind (macOS ARM64)

Keep project tools outside the repository:

```bash
export MODEL_DEMO_BIN="$HOME/.local/share/confidential-ml-distribution/bin"
mkdir -p "$MODEL_DEMO_BIN"
curl --fail --location --proto '=https' --tlsv1.2 \
  https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-darwin-arm64 \
  --output "$MODEL_DEMO_BIN/kind.download"
echo "0c8c7dbe5e23594a198b786c4bc13dacc101fa6196b0cb0b23a1ca44e61f4b4f  $MODEL_DEMO_BIN/kind.download" \
  | shasum -a 256 -c - && \
  chmod 755 "$MODEL_DEMO_BIN/kind.download" && \
  mv "$MODEL_DEMO_BIN/kind.download" "$MODEL_DEMO_BIN/kind"
export PATH="$MODEL_DEMO_BIN:$PATH"
kind version
```

Other platforms need their matching binary and checksum from the
[v0.33.0 release](https://github.com/kubernetes-sigs/kind/releases/tag/v0.33.0).

### Start and check the cluster

From the repository root, start Docker and check its engine:

```bash
open -a Docker
export DOCKER_CONTEXT=desktop-linux
docker info --format 'os={{.OSType}} arch={{.Architecture}}'
```

Startup is asynchronous: continue only after `docker info` succeeds. These startup
commands are macOS-specific; other machines need their own working Docker context.
The separate kubeconfig below avoids changing the default Kubernetes context.

```bash
umask 077
export MODEL_DEMO_KUBECONFIG="$HOME/.config/confidential-ml-distribution/kubeconfig"
mkdir -p "$(dirname "$MODEL_DEMO_KUBECONFIG")"
KIND_EXPERIMENTAL_PROVIDER=docker kind create cluster \
  --name model-demo --config k8s/kind.yaml \
  --kubeconfig "$MODEL_DEMO_KUBECONFIG" --wait 180s
kubectl --kubeconfig "$MODEL_DEMO_KUBECONFIG" --context kind-model-demo \
  wait --for=condition=Ready node --all --timeout=120s
kubectl --kubeconfig "$MODEL_DEMO_KUBECONFIG" --context kind-model-demo \
  wait --for=condition=Ready pod --all --all-namespaces --timeout=120s
kubectl --kubeconfig "$MODEL_DEMO_KUBECONFIG" --context kind-model-demo get nodes
kubectl --kubeconfig "$MODEL_DEMO_KUBECONFIG" --context kind-model-demo \
  get pods --all-namespaces
```

## Kubernetes demo

From a fresh checkout, install the Python prerequisites above and run
`uv sync --frozen --python python3.12`. Create the local cluster using the preceding
section. Producer downloads the source; Consumer downloads the encrypted artifact
and, in Layer 2, its signature.
The standalone local demos are optional diagnostics, not prerequisites for this flow.

### Build and load the images

```bash
docker build -f Dockerfile.producer -t model-producer:layer1 .
docker build -f Dockerfile.consumer -t model-consumer:layer1 .
KIND_EXPERIMENTAL_PROVIDER=docker kind load docker-image \
  model-producer:layer1 model-consumer:layer1 --name model-demo
```

The Dockerfiles pin the Python/uv base images by digest and install `uv.lock`
without development dependencies. Builds use the local platform; it must match
the kind node. Jobs use `imagePullPolicy: Never`, so no container registry or image
push is required. Rebuild and reload both tags after changing application code.

### Authenticate and run

Use a public test repository owned by the logged-in user. This performs a real Hub
publication. Authentication happens on the operator's machine, not in an image:

```bash
umask 077
uv run --frozen hf auth login --format human --no-add-to-git-credential
export MODEL_DEMO_HF_REPO="<your-user>/confidential-ml-artifacts"
uv run --frozen python scripts/run_layer1.py \
  --repo "$MODEL_DEMO_HF_REPO" --kubeconfig "$MODEL_DEMO_KUBECONFIG" \
  --context kind-model-demo --verify-wrong-key
```

Bootstrap creates a fresh `model-demo-l1-*` namespace, saves its AES key under
`$HOME/.config/confidential-ml-distribution/runs/<namespace>/model.key` with mode `0600`, and creates
immutable Secrets from the templates. Secret values travel through `kubectl`
stdin; they are not written to YAML files or passed in command arguments. Do not
apply the empty Secret templates directly: immutable Secrets must be populated
before creation. ConfigMap placeholders are also filled by bootstrap.

Only Producer mounts the HF credential. Its temporary Kubernetes Secret is removed
before Consumer starts; deletion does not revoke a token already read. The local
HF login remains. The key Secret is read-only in both Jobs. A new run gets a new
key; preserve each run's key together
with its recorded artifact revision. A new key cannot decrypt an older publication.

The namespace enforces Kubernetes' restricted Pod Security policy. Both containers
run as UID/GID `10001`, with `fsGroup: 10001` making the `0440` Secret files readable.
Their root filesystems are read-only, capabilities are dropped, privilege escalation
is disabled, and no ServiceAccount token is mounted. No workload RBAC is required.
CPU/memory limits and a five-minute deadline bound each Job; failed Jobs are not
retried. Jobs do not guarantee exactly-once execution; bootstrap rejects ambiguous
results with multiple Pods rather than selecting an arbitrary success log.

`/work` is a memory-backed `emptyDir` capped at 256 MiB, with fresh caches. Consumer
first downloads anonymously, authenticates/decrypts, then starts a separate local-load
process with offline flags and an empty cache. Offline mode is not enabled before
the Hub download. The tmpfs does not protect against the host administrator or
guarantee secure memory erasure.

### Layer 2: signed distribution

Use the same Dockerfiles and local cluster, with distinct image tags. Authenticate
as above and set `MODEL_DEMO_HF_REPO` and `MODEL_DEMO_KUBECONFIG` before running.
Layer 2 can run directly after this setup; a prior Layer 1 run is not required:

```bash
docker build -f Dockerfile.producer -t model-producer:layer2 .
docker build -f Dockerfile.consumer -t model-consumer:layer2 .
KIND_EXPERIMENTAL_PROVIDER=docker kind load docker-image \
  model-producer:layer2 model-consumer:layer2 --name model-demo
uv run --frozen python scripts/run_layer1.py \
  --layer 2 --repo "$MODEL_DEMO_HF_REPO" --kubeconfig "$MODEL_DEMO_KUBECONFIG" \
  --context kind-model-demo --verify-wrong-key --verify-wrong-public-key
```

The script name is retained for compatibility; `--layer 1` (the default) remains
independent. Layer 2 uses the two `*-job-layer2.yaml` manifests in a fresh
`model-demo-l2-*` namespace. We keep explicit manifests for this small PoC so that
the different mounts and flags are visible without a template engine.

Bootstrap creates a new Ed25519 pair for this demo run. The private PEM backup is
`$HOME/.config/confidential-ml-distribution/runs/<namespace>/producer-signing.pem`; only Producer
mounts its temporary immutable `signing-key` Secret. Consumer instead mounts
`public.pem` from the operator-provisioned immutable
[`producer-verification-key` ConfigMap](https://v1-34.docs.kubernetes.io/docs/concepts/configuration/configmap/).
This public key is never downloaded from the Hub. Its local copy is saved with
the run records. Per-run signing keys keep the demonstration self-contained;
a production publisher would manage a stable signing identity and rotation policy.

Producer signs after encryption and publishes both files together. Bootstrap
passes the reported full commit to Consumer and requires a signed-publication
receipt. Consumer requires both `--require-signature` and `--public-key-file`;
partial configuration is an error, not a downgrade. It downloads both files,
verifies the signature, then reads AES, decrypts, extracts and loads locally.
Success includes `"signature_verified": true`. The temporary signing and HF token
Secrets are removed after Producer; their deletion does not revoke copied keys.

`--verify-wrong-public-key` creates an additional Consumer Job with a different
operator-provisioned public key. It must fail at signature verification.
`--verify-wrong-key` retains the correct signature/public key but changes AES;
it must fail at GCM authentication. Neither expected failure may report a loaded
model. Missing or modified signatures and the exact call order are also exercised
by offline tests that fail if AES is read or decryption is called after a bad signature.

### Inspect and clean up a run

Copy the non-secret `namespace` from bootstrap output. Reports and workload logs
are saved under the ignored `runtime/<namespace>/` directory. Successful output
records the artifact revision, distinct Pod UIDs, image IDs, exit codes and CPU result.
With `--verify-wrong-key`, `consumer-wrong-key` must fail at authentication without
a loaded-model report; that expected failure does not make bootstrap fail.

```bash
export MODEL_DEMO_RUN="<namespace-from-bootstrap>"
kubectl --kubeconfig "$MODEL_DEMO_KUBECONFIG" --context kind-model-demo \
  get jobs,pods -n "$MODEL_DEMO_RUN"
cat "runtime/$MODEL_DEMO_RUN/result.json"
kubectl --kubeconfig "$MODEL_DEMO_KUBECONFIG" --context kind-model-demo \
  delete namespace "$MODEL_DEMO_RUN" --wait=true
```

Namespace deletion removes Jobs, Secrets and ephemeral volumes, retaining the public
Hub revision, local records and key backup. On handled errors or Ctrl+C, bootstrap
attempts to stop Producer before removing its temporary credential. Inspect
interrupted runs and the Hub before retrying: publication may have succeeded before
its receipt was returned. Preserve the key; delete the namespace for cluster cleanup.
Never reuse a namespace or key path.

### Local loading with Docker networking disabled

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

## Verification

Latest integration runs on 2026-09-17 used a newly created `model-demo` kind
cluster and the neutral names documented above. Previously tested application
images were retagged and imported; application code and Dockerfiles were unchanged.

| Run | Published Hub revision | Observed result |
|---|---|---|
| Layer 1: `model-demo-l1-4307c63d` | [`50fc2f58f73427e2da8f99e1adebd05738e5f0b3`](https://huggingface.co/adeavid/confidential-ml-artifacts/tree/50fc2f58f73427e2da8f99e1adebd05738e5f0b3) | Producer and new Consumer exited 0; wrong AES key exited 1 without loading. |
| Layer 2: `model-demo-l2-4a883a84` | [`83612572c07e98c4167b37d2897e4bf1863ac976`](https://huggingface.co/adeavid/confidential-ml-artifacts/tree/83612572c07e98c4167b37d2897e4bf1863ac976) | Producer and new Consumer exited 0 with signature verified; wrong AES and public keys exited 1 without loading. |

Both successful Consumers reported CPU and finite `[1, 9, 30522]` output.
Anonymous Hub inspection confirmed only `.gitattributes` and `model.cml`, plus
`model.cml.sig` for Layer 2. This was a fresh cluster on the existing machine,
not an independent second-machine installation.

Additional checks and earlier milestone evidence:

- kind checksum matched; cluster creation and readiness checks exited 0. One node
  and all nine system Pods were Ready; API bound to loopback; kubeconfig mode 0600.
- Anonymous source download passed: four files, 17,975,651 bytes in total.
- Separate local load passed: `BertForPreTraining`, CPU, 4,433,468 parameters,
  nine input tokens, output shape `[1, 9, 30522]`, finite output, no loading errors.
- Full suite after the neutral-name cleanup, run from the working tree with
  `MODEL_DEMO_TEST_MODEL` pointing to the previously downloaded real checkpoint:
  **186 passed in 54.35 seconds**.
  Fixtures cover authentication failures, ZIP safety, size limits, permissions,
  atomic signed Hub publication, bootstrap safeguards and Ed25519 signatures.
  Bad or missing signatures stop before AES access; decryption uses the exact
  verified bytes even if the downloaded file changes afterwards. The two opt-in
  real-model tests used new processes with empty caches and zero Python socket attempts.
- Both Dockerfiles previously built successfully from an isolated export of the
  application checkpoint using the existing Docker
  layer cache. All ten workload/resource templates passed API server dry-run
  validation. This confirms packaging and manifest validity on the existing
  environment; it does not replace the real runs below or a second-machine test.
- Real CLI decryption recovered byte-identical source files and license. Wrong key,
  altered ciphertext/tag and truncation each exited 1 without a recovered directory.
- Both images built for Linux ARM64, run as UID `10001`, and use PyTorch without
  CUDA. The real model also loaded from the Consumer image with `--network none`,
  a read-only root filesystem and fresh memory-backed working storage.
- The earlier Layer 1 bootstrap completed: Producer completed with exit 0,
  publishing [revision `7f4108411a72e022a4df5492203755fc821df712`](https://huggingface.co/adeavid/confidential-ml-artifacts/tree/7f4108411a72e022a4df5492203755fc821df712); a new Consumer Pod
  retrieved that revision and completed with exit 0, CPU and finite `[1, 9, 30522]`
  output from 4,433,468 parameters. The wrong-key Job failed authentication with
  exit 1 and no loaded-model report. The immutable AES Secret contained 32 bytes;
  UID/GID/fsGroup and read-only mounts matched the manifests. Neither Pod mounted
  a ServiceAccount token; Consumer had no HF credential and the temporary publisher
  Secret was confirmed absent afterwards. That public Hub revision contained only
  `.gitattributes` and the 17,987,550-byte `model.cml`.
- Local signing tests: **28 passed**. They include altered header/nonce/ciphertext/tag,
  absent or invalid signatures, wrong public keys, invalid key formats, output
  safety and a replacement artifact that passes GCM with the same AES key but
  fails the original Producer signature.
- Real local CLI signing of the existing 17,987,550-byte artifact succeeded;
  its detached signature was 64 bytes and verification exited 0. Wrong public key,
  missing/modified signature, modified ciphertext and truncation each exited 1.
  Ephemeral signing private keys were removed after this check. This run performed
  no publication, Kubernetes deployment, decryption or model loading.
- The earlier signed bootstrap passed on 2026-09-17:
  Producer published [revision `f8f92a7548ffb19e1f52d4df7902c2498b63cde3`](https://huggingface.co/adeavid/confidential-ml-artifacts/tree/f8f92a7548ffb19e1f52d4df7902c2498b63cde3)
  with `model.cml` (17,987,550 bytes) and `model.cml.sig` (64 bytes) in one commit.
  Anonymous inspection found only these files and `.gitattributes`.
  A new Consumer Pod completed with exit 0, `signature_verified: true`, CPU,
  4,433,468 parameters and finite `[1, 9, 30522]` output.
  The wrong-public-key Job exited 1 at signature verification; the wrong-AES Job
  exited 1 at GCM authentication. Neither reported a loaded model.
  Deployed mounts matched the manifests: only Producer received the private
  signing key and HF credential; Consumer received AES and the controlled public
  ConfigMap. The public key matched the operator's copy, the ConfigMap was
  immutable, and the temporary signing/token Secrets were absent after Producer.
  Reports and checked logs are retained locally under the ignored run directory;
  the public README records only non-secret outcomes.

Missing/modified signatures were tested offline, not by modifying the public Hub.
Attestation remains unimplemented and untested.
The full setup has not yet been repeated on a second clean machine.

## Troubleshooting and cleanup

- **Existing model directory:** run `load` against it; use a new path for a fresh
  download. An incomplete folder is an error, not a trigger to fetch missing files.
- **Authentication failed:** verify the expected artifact/key pair and transfer;
  abort on mismatch. Do not bypass authentication or download the original model.
- **Encryption output already exists:** use fresh artifact and key names; overwriting
  either independently could lose the key needed for an existing artifact.
- **Hub 401/403:** check the logged-in account and repository write permission.
- **Unauthenticated download warning:** expected for our public download; no token is needed.
- **Hub commit conflict:** inspect the changed remote repository before retrying;
  never silently publish on top of a revision you have not checked.
- **Docker socket unavailable:** wait for `docker info` to succeed before kind.
- **Cluster already exists:** inspect it with the explicit kubeconfig/context.
- **Image download fails:** inspect the network error; do not substitute the pin.
- **ErrImageNeverPull:** build and load the exact local image tags into this kind
  cluster before running bootstrap; do not change the policy to pull an unknown image.
- **Node not Ready:** inspect system Pods and Docker resources. Readiness alone
  does not demonstrate the model pipeline.

To remove this project's local cluster and all its in-cluster resources:

```bash
DOCKER_CONTEXT=desktop-linux KIND_EXPERIMENTAL_PROVIDER=docker kind delete cluster \
  --name model-demo --kubeconfig "$HOME/.config/confidential-ml-distribution/kubeconfig"
```

## Credentials and publication

- Keep AES keys, signing private keys, Hub tokens, and kubeconfigs outside the repo;
  never embed them in arguments, logs, images, or committed YAML.
- `.gitignore` excludes generated `runtime/` and `artifacts/` paths defensively;
  it does not protect tracked files, Docker builds, or Hub uploads. `.dockerignore`
  permits source, the model license and dependencies only; use explicit Docker `COPY` paths.
- The AES key decrypts the artifact; the HF token authorizes Hub operations;
  the optional signing private key identifies Producer. They are distinct.
- [Public Git repository](https://github.com/adeavid/confidential-ml-distribution)
  and `adeavid/confidential-ml-artifacts` on Hugging Face are authorized destinations.
  Only the encrypted artifact and, for Layer 2, its signature are uploaded to the Hub. AES keys exist in the
  operator's protected backup directory and the run's Kubernetes Secret, not in Git or Hub.

## Limitations and future work

Layer 3 is deferred after a read-only feasibility check on 2026-09-17. The current
Linux ARM64 kind node has no `/dev/kvm` device or KVM module; no CoCo RuntimeClass
is installed. The missing virtualization prerequisite blocks the required
Kata/QEMU flow in this setup. See the [Kata prerequisites](https://github.com/kata-containers/kata-containers/blob/main/docs/quick-start-guide.md#try-it-out).
No CoCo/KBS components were installed, and no attestation flow was tested.

Revisiting Layer 3 requires a compatible Linux node with usable KVM, including
nested virtualization when applicable, and a tested, pinned software stack.
The assessment reference uses operator v0.10.0 and Trustee v0.10.1; the
[current installation guide](https://github.com/confidential-containers/charts/blob/main/QUICKSTART.md)
uses Helm and deprecates the operator. We have not silently substituted that
installation for the required operator.

Sample attestation does not prove hardware-backed isolation from the host.
Production key rotation/revocation, strict attestation policy, and real
confidential hardware are future extensions.

References: [Secrets](https://kubernetes.io/docs/concepts/configuration/secret/), [Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/),
[kind](https://kind.sigs.k8s.io/docs/user/quick-start/), [CoCo development reference](https://confidentialcontainers.org/blog/2024/12/03/confidential-containers-without-confidential-hardware/).
