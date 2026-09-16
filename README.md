# Confidential ML Model Distribution PoC

A reproducible technical assessment implementation with explainable decisions and demonstrated failures.

**Current status:** encryption, real Hub publication, anonymous retrieval, and
local CPU loading have passed. Local Kubernetes infrastructure is ready, but
Producer/Consumer Jobs are not implemented. **Layer 1 is incomplete.** See [verification](#verification).

## Scope and acceptance

The assessment PDF is the source of requirements. Only Layer 1 is mandatory;
Layers 2 and 3 are optional and independent. Validate Layer 1 before adding Layer 2.
Evaluate Layer 3 afterwards only if the environment and time allow.

| Requirement | Implementation / plan | Acceptance evidence | Status |
| --- | --- | --- | --- |
| L1: select and encrypt a small open model | Pinned BERT; bounded ZIP and AES-256-GCM | Real local round trip and CPU load; negative tests | Local round trip passed |
| L1: publish encrypted artifact on HF Hub | Single-file commit in authorized public test repository | Full commit and anonymous download of identical ciphertext | Passed using local processes |
| L1: deliver key through a Kubernetes Secret | Bootstrap provisions Secret; Consumer mounts it read-only | Fresh Pod reads key without Kubernetes API credentials | Pending |
| L1: download, decrypt, load in Kubernetes | Consumer Job uses pinned revision and decrypted local directory | Fresh Job completes; missing files fail without fallback | Pending |
| L2: sign and verify before decryption | Ed25519 over complete artifact; trusted public key | Missing/invalid signature or wrong key aborts before decryption | Pending |
| Delivery: public Git repo, Dockerfiles, manifests, README | Add artifacts with each milestone | Repeat documented demo from a clean environment | In progress |

AES-GCM, Ed25519, kind, and Jobs are project choices. A CPU forward pass is extra
evidence of an operational model; loading itself is the formal requirement.

## Planned architecture

```text
Public source model (fixed commit)
              |
              v
         Producer Job ---- encrypted artifact ----> HF test repository
              |                                        |
         new AES key                              fixed commit
              |                                        |
      bootstrap provisioning                           v
              +----> Kubernetes Secret ---------> Consumer Job
                            read-only file         decrypt -> local load
```

This is the target pipeline. The bootstrap handoff of the generated key will be
implemented with Producer; the model process need not create Secrets via the API.

- **Pod:** Kubernetes' smallest deployable unit, grouping containers with their
  network and storage. Our Consumer Pod will run Python and mount the key file.
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
Linux selects the PyTorch CPU index; **Linux application execution is not tested yet**.

Source: [Google BERT Tiny](https://huggingface.co/google/bert_uncased_L-2_H-128_A-2/tree/30b0a37ccaaa32f332884b96992754e246e48c5f),
revision `30b0a37ccaaa32f332884b96992754e246e48c5f`, public and requiring no token.
`src/model_demo.py` downloads only `config.json`, `model.safetensors` (17.7 MB),
`vocab.txt`, and the original `README.md`. The model card declares Apache-2.0.
Packaging preserves that card and includes `licenses/model-APACHE-2.0.txt` as
`LICENSE`, copied from the [official Apache text](https://www.apache.org/licenses/LICENSE-2.0.txt).

Run from the repository root:

```bash
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
another encryption. Each encryption generates a new key.

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
via a symlink to accommodate future Kubernetes Secret volumes. This does not yet
provision a Secret. Outputs are operator-controlled local paths; do not use shared
writable parent directories. Interrupted two-file creation is not a crash-safe
transaction: inspect leftovers and use fresh paths after an interruption.

### Format and decisions

```text
Public header / AAD (17 bytes) = CMLD (4) + version 1 (1) + nonce (12)
Artifact = header + ciphertext + GCM tag (16 bytes)
```

`artifact.py` uses `cryptography==50.0.1`'s high-level `AESGCM`: a fresh 256-bit
key and a random 12-byte nonce for every encryption. The entire header is AAD
(authenticated associated data). The library appends and checks the full tag.
Never reuse a nonce with the same key. Wrong keys, changed nonces/ciphertext/tags,
and truncation fail before ZIP parsing, extraction, or model loading; the tag
cannot identify which of these caused authentication to fail.

AES-GCM combines confidentiality and integrity in one supported API. AES-CBC
with a separate MAC would require more composition and padding logic;
ChaCha20-Poly1305 is also a valid alternative. AES-GCM does not authenticate a
unique producer against other holders of the AES key; Layer 2 will add that.

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

## Hub round trip with local processes

Use your own authorized test repository. From an interactive terminal, authenticate
with the browser option; do not paste tokens into commands, source code or chat:

```bash
umask 077
uv run --frozen hf auth login --format human --no-add-to-git-credential
```

After the local encryption step, publish its ciphertext and copy the full
`revision` from the JSON result into `MODEL_DEMO_HF_REVISION`:

```bash
export MODEL_DEMO_HF_REPO="<your-user>/confidential-ml-artifacts"
uv run --frozen python src/hub_artifact.py publish \
  --repo "$MODEL_DEMO_HF_REPO" --artifact artifacts/model.cml
export MODEL_DEMO_HF_REVISION="<full-commit-returned-by-publish>"
uv run --frozen python src/hub_artifact.py download \
  --repo "$MODEL_DEMO_HF_REPO" --revision "$MODEL_DEMO_HF_REVISION" \
  --destination runtime/hub-model.cml
uv run --frozen python src/artifact.py decrypt \
  --artifact runtime/hub-model.cml --key-file "$MODEL_DEMO_KEY_DIR/model-v1.key" \
  --destination runtime/hub-decrypted-model && \
uv run --frozen python src/model_demo.py load runtime/hub-decrypted-model
```

Keep the matching local key from that publication. Creating another key cannot
decrypt an existing artifact. The source-model commit and the encrypted-artifact
commit belong to different repositories and identify different files.
Both download and decryption destinations must be new; change both paths to repeat the demo.

Publication creates a public model repository if needed, refuses unexpected
existing files, and commits **only `model.cml`**, from bytes already read after
checking their header and size. `.gitattributes` is created by Hugging Face.
A parent-commit check aborts if another writer changes the branch before our
commit; publication conflicts are not automatically retried.
The publisher never receives the AES key. Retrieval uses `token=False`, a full
commit ID and a fresh temporary cache. It checks Hub metadata size before download,
then actual size and format before creating a new local file. This trusts Hub's
HTTPS metadata service; AES-GCM separately authenticates the downloaded content.
SDK mocks test these decisions locally and are not evidence of Hub integration.

## Local development cluster

Docker Desktop supplies Linux on macOS; kind creates a container acting as our
single Kubernetes node. `kubectl` uses the endpoint and credentials in kubeconfig
to communicate with it. One local node avoids cloud cost and credentials.

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

## Verification

- kind checksum matched; cluster creation and readiness checks exited 0. One node
  and all nine system Pods were Ready; API bound to loopback; kubeconfig mode 0600.
- Anonymous source download passed: four files, 17,975,651 bytes in total.
- Separate local load passed: `BertForPreTraining`, CPU, 4,433,468 parameters,
  nine input tokens, output shape `[1, 9, 30522]`, finite output, no loading errors.
- Local artifact suite: **48 passed** using tiny fixtures, without network or
  PyTorch. Covers authentication failures, unsafe ZIP entries, limits and permissions.
- Hub protocol suite: **22 passed**, using mocked SDK calls to check file selection,
  revision pinning, limits, cache paths and publication conflicts.
- With `MODEL_DEMO_TEST_MODEL=runtime/hub-decrypted-model`: **95 passed**, including the
  retrieved checkpoint and its encrypted round trip in new processes with empty caches
  and zero observed Python socket attempts. The two real-model tests are opt-in.
- Real CLI encryption produced a 17,987,550-byte artifact. Decrypted source files
  and the bundled license were byte-identical; the recovered model loaded on CPU.
  Wrong key, changed ciphertext, changed tag and truncation each exited 1 with an
  authentication error and no recovered directory. The AES key was 32 bytes, mode 0600.
- Real public Hub artifact: [adeavid/confidential-ml-artifacts](https://huggingface.co/adeavid/confidential-ml-artifacts/tree/ceec126ff0ea4cb81e381eaa4270ac5078a1a414),
  revision `ceec126ff0ea4cb81e381eaa4270ac5078a1a414`. The revision contains only
  `model.cml` and the Hub-generated `.gitattributes`; the AES key was not uploaded.
  Anonymous retrieval returned byte-identical ciphertext; decryption recovered
  the original files, and the recovered model completed the CPU forward pass.
  Artifact SHA-256: `ede783b080c362145a38ca8f3940f02158c25122459039ea352bac9919112226`.

Application Jobs, Secret provisioning, signing, and attestation remain untested.
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
  Only the encrypted model artifact was uploaded to the Hub; keys remain local.

## Next milestones

Add Dockerfiles, then run both Jobs with Secret provisioning and reproduce
Layer 1 before adding Layer 2. Layer 3 is deferred: this macOS/kind setup has not
been validated for Kata/CoCo. Sample attestation does not prove hardware-backed
isolation from the host. Production key rotation/revocation, strict attestation
policy, and real confidential hardware are future extensions.

References: [Secrets](https://kubernetes.io/docs/concepts/configuration/secret/), [Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/),
[kind](https://kind.sigs.k8s.io/docs/user/quick-start/), [CoCo development reference](https://confidentialcontainers.org/blog/2024/12/03/confidential-containers-without-confidential-hardware/).
