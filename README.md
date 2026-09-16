# Confidential ML Model Distribution PoC

A reproducible technical assessment implementation with explainable decisions and demonstrated failures.

**Current status:** local Kubernetes infrastructure and a real CPU model load
have passed. Encryption, Hub publication, and Producer/Consumer Jobs are not
implemented. **Layer 1 is incomplete.** See [verification](#verification).

## Scope and acceptance

The assessment PDF is the source of requirements. Only Layer 1 is mandatory;
Layers 2 and 3 are optional and independent. Validate Layer 1 before adding Layer 2.
Evaluate Layer 3 afterwards only if the environment and time allow.

| Requirement | Implementation / plan | Acceptance evidence | Status |
| --- | --- | --- | --- |
| L1: select and encrypt a small open model | Pinned BERT; planned AES-256-GCM package | Real local load; encryption round trip and malformed-input failures | Local load passed; encryption pending |
| L1: publish encrypted artifact on HF Hub | Explicit file list; authorized test repository | Resulting full commit ID and uploaded file names | Pending |
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
`vocab.txt`, and the original `README.md`. The model card declares Apache-2.0;
preserve its attribution and include the license text before redistributing a package.

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
real checkpoint in a separate process with an empty cache and a Python socket
guard. The guard detects attempted Python socket networking; it does not enforce
OS-level isolation or cover native networking. Fixture tests do not demonstrate
Hugging Face or Kubernetes integration.

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
- Offline fixture suite: **23 passed, 1 skipped** (the real model test is opt-in).
- With `MODEL_DEMO_TEST_MODEL=runtime/source-model`: **24 passed**, including the
  real checkpoint in a new process with an empty cache and zero observed Python
  socket attempts. Missing/unsafe files and corrupt/incomplete weights failed.

Encryption, HF upload, application Jobs, signing, and attestation remain untested.
The full setup has not yet been repeated on a second clean machine.

## Troubleshooting and cleanup

- **Existing model directory:** run `load` against it; use a new path for a fresh
  download. An incomplete folder is an error, not a trigger to fetch missing files.
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
  permits source and dependency declarations only; use explicit Docker `COPY` paths.
- The AES key decrypts the artifact; the HF token authorizes Hub operations;
  the optional signing private key identifies Producer. They are distinct.
- [Public Git repository](https://github.com/adeavid/confidential-ml-distribution)
  is authorized. An HF test destination still needs authorization; no model, key,
  or encrypted artifact has been published.

## Next milestones

Add authenticated encryption, then publish and retrieve
the intended encrypted files. Run both Jobs with Secret provisioning and reproduce
Layer 1 before adding Layer 2. Layer 3 is deferred: this macOS/kind setup has not
been validated for Kata/CoCo. Sample attestation does not prove hardware-backed
isolation from the host. Production key rotation/revocation, strict attestation
policy, and real confidential hardware are future extensions.

References: [Secrets](https://kubernetes.io/docs/concepts/configuration/secret/), [Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/),
[kind](https://kind.sigs.k8s.io/docs/user/quick-start/), [CoCo development reference](https://confidentialcontainers.org/blog/2024/12/03/confidential-containers-without-confidential-hardware/).
