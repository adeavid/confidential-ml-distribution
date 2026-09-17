# Confidential ML Model Distribution PoC

Distribute an encrypted model through Hugging Face and load it in Kubernetes.
Layer 1 delivers AES through a Secret. Layer 2 verifies the Producer's signature
before decryption. The original model is public; this demonstrates the delivery
mechanism, not secrecy of the original weights.

**Layers 1 and 2 verified on local kind / Linux ARM64. Layer 3 is not implemented.**
Only Layer 1 is required by the assessment; the others are optional and independent.

[Run the demo](#run-the-demo) · [Observed results](#observed-results) ·
[Design and security](docs/design.md) · [Optional local checks](docs/local-checks.md)

## Architecture

![Layers 1 and 2: public artifacts, Kubernetes Jobs, and controlled key mounts](docs/architecture-layer1-layer2.png)

Bootstrap runs on the operator's machine: it generates keys, provisions Secrets
and ConfigMaps, starts Producer, and passes the published commit to Consumer.
Producer and Consumer are separate Jobs: each creates a Pod, runs Python and exits.
Neither workload uses the Kubernetes API. Consumer receives AES and, in Layer 2,
a controlled public key. Only Producer receives the Hub token and, in Layer 2,
the signing private key.

Layer 2 verifies the exact bytes it later decrypts. Its AES file is already mounted
at Pod startup: verification controls program order, not key release. The loader
then uses the extracted folder, an empty cache, and offline settings in a child
process. Host and cluster administrators remain trusted.

## Prerequisites

The commands below assume a POSIX shell and a local Docker engine. The complete
route was tested on macOS ARM64 with Docker Desktop; Linux/AMD64 repetition is
pending. Use the same terminal throughout so exported variables remain available.

| Tool / access | Tested requirement |
|---|---|
| Python and uv | Python **3.12.14** installed as `python3.12`; uv **0.8.17** |
| Container tooling | Working Docker Engine **29.1.3**; kind **v0.33.0**; kubectl **v1.34.1** |
| Kubernetes | Local cluster created below; node **v1.34.11**, pinned by digest |
| Other commands | Git; curl and shasum for the macOS kind installation |
| Hugging Face | Personal account and a token allowed to create/write your public test model repository |
| Network | HTTPS access to Hugging Face, package indexes and container registries |

Follow [tool setup](docs/setup.md) if anything is missing. Python installation is
an explicit prerequisite: the pinned older uv cannot download this Python patch
itself. No cloud subscription, container registry push or GPU is required.

## Run the demo

### 1. Clone and install dependencies

Run all subsequent commands from this repository's root. `$HOME` means your own
home directory; no personal absolute paths are embedded in the commands.

```bash
git clone https://github.com/adeavid/confidential-ml-distribution.git
cd confidential-ml-distribution
python3.12 --version
uv --version
uv sync --frozen --python python3.12
```

Direct dependency versions and the resolved environment are pinned in
`pyproject.toml` and `uv.lock`. Linux selects CPU-only PyTorch.
The Kubernetes demo downloads its own model; no standalone local demo is required.

### 2. Create the local cluster

Start Docker first. On the tested macOS setup use `open -a Docker` and
`export DOCKER_CONTEXT=desktop-linux`; wait until `docker info` succeeds.
On Linux, use your intended local Docker context instead. See [setup](docs/setup.md).

```bash
docker info --format 'os={{.OSType}} arch={{.Architecture}}'
kind version
kubectl version --client
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
```

The API binds to loopback. The separate kubeconfig avoids replacing your default
context. If this cluster already exists, inspect it instead of recreating it.

### 3. Build and load the images

```bash
docker build -f Dockerfile.producer -t model-producer:layer1 .
docker build -f Dockerfile.consumer -t model-consumer:layer1 .
KIND_EXPERIMENTAL_PROVIDER=docker kind load docker-image \
  model-producer:layer1 model-consumer:layer1 --name model-demo
```

Both Dockerfiles pin their Python/uv base images by digest. Build for the same
platform as the kind node. Jobs use `imagePullPolicy: Never`, so images must be
loaded into kind. Rebuild and reload after application changes.

### 4. Authenticate and run Layer 1

This performs a **real publication** to your own public Hugging Face model repo.
Replace `<your-user>` below with your personal account, not an organization.
Producer creates the repo if needed. Use a dedicated test repo: no README/model
card or unrelated files; only `.gitattributes`, `model.cml` and `model.cml.sig`
are permitted. Enter your token only into the local login prompt.

```bash
uv run --frozen hf auth login --format human --no-add-to-git-credential
export MODEL_DEMO_HF_REPO="<your-user>/confidential-ml-artifacts"
uv run --frozen python scripts/run_layer1.py \
  --repo "$MODEL_DEMO_HF_REPO" --kubeconfig "$MODEL_DEMO_KUBECONFIG" \
  --context kind-model-demo --verify-wrong-key
```

Expected: Producer and Consumer exit **0**; Consumer reports `status: loaded`,
`device: cpu`, `finite_output: true`, and the published full revision.
The additional `consumer-wrong-key` Job must exit **1** without loading.
That expected negative result does not make the bootstrap command fail.

Bootstrap creates a fresh namespace and provisions immutable Secrets from the
checked-in templates. Do not apply the empty Secret templates directly. Keys and
tokens are supplied through stdin to kubectl, not embedded in versioned YAML,
images, process arguments or logs. AES backups are stored outside the repo under
`$HOME/.config/confidential-ml-distribution/runs/<namespace>/model.key` (`0600`).

### 5. Run Layer 2

Use the same setup, account and variables. Layer 2 can also run directly after
setup; a completed Layer 1 run is not a prerequisite. Separate tags make each
manifest's intended mode visible.

```bash
docker build -f Dockerfile.producer -t model-producer:layer2 .
docker build -f Dockerfile.consumer -t model-consumer:layer2 .
KIND_EXPERIMENTAL_PROVIDER=docker kind load docker-image \
  model-producer:layer2 model-consumer:layer2 --name model-demo
uv run --frozen python scripts/run_layer1.py \
  --layer 2 --repo "$MODEL_DEMO_HF_REPO" --kubeconfig "$MODEL_DEMO_KUBECONFIG" \
  --context kind-model-demo --verify-wrong-key --verify-wrong-public-key
```

Expected: the successful Consumer additionally reports `signature_verified: true`.
The wrong-public-key Job fails at signature verification; the wrong-AES Job fails
at GCM authentication. Both exit **1** without a loaded-model report. Artifact and
signature are published together and retrieved from the same full commit.

Bootstrap creates a fresh Ed25519 pair and supplies the public key through a
controlled ConfigMap, independently of the Hub. The private backup is
`$HOME/.config/confidential-ml-distribution/runs/<namespace>/producer-signing.pem`.
Temporary signing/token Secrets are removed after Producer; deleting a Secret
does not revoke an already copied credential. Layer 2 never falls back to Layer 1.

### 6. Inspect and clean up

Copy `namespace` from the bootstrap output. Each run has a different namespace.
Local reports include revisions, Pod UIDs, image IDs, exit codes and CPU results.

```bash
export MODEL_DEMO_RUN="<namespace-from-bootstrap>"
kubectl --kubeconfig "$MODEL_DEMO_KUBECONFIG" --context kind-model-demo \
  get jobs,pods -n "$MODEL_DEMO_RUN"
cat "runtime/$MODEL_DEMO_RUN/result.json"
```

After inspection, remove that run's Jobs, Secrets and temporary volumes:

```bash
kubectl --kubeconfig "$MODEL_DEMO_KUBECONFIG" --context kind-model-demo \
  delete namespace "$MODEL_DEMO_RUN" --wait=true
```

The public Hub revision, local report and protected key backup remain. Preserve
matching keys if you need to consume that revision again. For full cluster cleanup:

```bash
KIND_EXPERIMENTAL_PROVIDER=docker kind delete cluster \
  --name model-demo --kubeconfig "$MODEL_DEMO_KUBECONFIG"
```

## Tests and observed results

Fast tests use small fixtures and ephemeral keys, with no external services:

```bash
uv run --frozen pytest -q
```

Two real-model tests are opt-in. To include them, download the pinned source once:

```bash
uv run --frozen python src/model_demo.py download runtime/source-model
MODEL_DEMO_TEST_MODEL=runtime/source-model uv run --frozen pytest -q
```

Use a new destination for downloads; reuse the existing complete folder for tests.
The opt-in tests load in new processes with empty caches and a Python socket guard.
For a separate load-only check with container networking disabled, see
[local checks](docs/local-checks.md#load-with-docker-networking-disabled).

### Observed results

On **2026-09-17**, the full suite with both real-model tests passed:
**186 passed in 54.35 s**. This is an observed duration, not a runtime guarantee.
A new local kind cluster then completed these real Hub/Kubernetes runs:

| Layer | Namespace | Published revision | Result |
|---|---|---|---|
| 1 | `model-demo-l1-4307c63d` | [`50fc2f58f73427e2da8f99e1adebd05738e5f0b3`](https://huggingface.co/adeavid/confidential-ml-artifacts/tree/50fc2f58f73427e2da8f99e1adebd05738e5f0b3) | Producer/Consumer exit 0; wrong AES exits 1. |
| 2 | `model-demo-l2-4a883a84` | [`83612572c07e98c4167b37d2897e4bf1863ac976`](https://huggingface.co/adeavid/confidential-ml-artifacts/tree/83612572c07e98c4167b37d2897e4bf1863ac976) | Signature verified and model loaded; wrong AES/public key exit 1. |

Both successful Consumers produced finite `[1, 9, 30522]` output on CPU from
4,433,468 parameters. Anonymous Hub inspection found only the intended files.
Application images had previously built from an isolated checkout using Docker's
layer cache; the new-cluster runs reused those images with neutral tags.

| Acceptance property | Evidence |
|---|---|
| Secret delivery and fresh Kubernetes Consumer load | Real runs above; read-only mounts and non-root UID/GID checked. |
| Wrong AES, changed ciphertext/tag, truncation | Local fixtures and real artifact CLI checks; wrong AES also tested in Kubernetes. |
| Bad/missing signature or wrong public key stops before AES | Offline call-order tests; wrong public key also tested in Kubernetes. |
| Local files are sufficient; missing files cannot trigger fallback | Opt-in real-model tests with empty caches; separate Docker `--network none` load passed. |
| Unsafe archive names, links, types and sizes rejected | Local package fixtures; no unsafe extraction paths accepted. |
| Container packaging and manifest validity | Both Dockerfiles built; ten resource/workload templates passed API server dry-run. |

Missing/modified signatures were tested offline, not by corrupting public Hub
contents. Fixture tests are not integration evidence. No fresh installation on
an independent second machine has been completed.

## Troubleshooting

| Symptom | Check / action |
|---|---|
| Docker unavailable | Start the intended engine; `docker info` must succeed before kind. |
| `ErrImageNeverPull` | Build and load the exact tag into this kind cluster. |
| Hub 401/403 or rejected repository | Check personal-account ownership, create/write permissions, and allowed repo files. |
| Authentication or signature failure | Stop; inspect the expected artifact revision and matching keys. Never bypass checks. |
| Existing output directory or namespace | Use a fresh run; never reuse private-key paths. |
| Interrupted publication / commit conflict | Inspect the Hub and retain the key before retrying; publication may already have succeeded. |
| Wrong cluster / empty kubeconfig argument | Re-export the README variables in this terminal and use explicit `kind-model-demo`. |
| Missing local model files | Treat as a failure; there is no original-model download fallback. |

## Decisions and limitations

The selected source is [Google BERT Tiny at a fixed commit](https://huggingface.co/google/bert_uncased_L-2_H-128_A-2/tree/30b0a37ccaaa32f332884b96992754e246e48c5f),
with safetensors weights and an Apache-2.0 model card/license preserved in the
package. Its size makes CPU verification practical. Full-buffer AES-GCM, Ed25519,
Jobs, extraction bounds and alternatives are explained in [design](docs/design.md).

Kubernetes Secret base64 is not encryption. Privileged host/cluster administrators
can access keys or plaintext. A valid signature does not prove model safety or
freshness, and a pinned revision is not a complete anti-rollback system.
Memory-backed temporary storage does not guarantee secure memory erasure.

**Layer 3:** not implemented or verified. The current local node lacks `/dev/kvm`,
so it cannot run the required Kata/QEMU flow as configured. A separate Linux
x86_64 VM also lacked `/dev/kvm` and exposed no `vmx`/`svm` flags during a read-only
check. No workloads were changed there. Another node must first demonstrate
usable KVM and, if needed, nested virtualization. The assessment
reference uses operator v0.10.0 and Trustee v0.10.1; current CoCo documentation
uses Helm and deprecates the operator. A compatible stack retaining the required
operator must be pinned and tested before implementation. Sample attestation
would demonstrate the protocol, not hardware-backed protection from the host.

Production rotation/revocation, strict attestation policies and real confidential
hardware remain future work. References: [Kata prerequisites](https://github.com/kata-containers/kata-containers/blob/main/docs/quick-start-guide.md#try-it-out),
[assessment CoCo reference](https://confidentialcontainers.org/blog/2024/12/03/confidential-containers-without-confidential-hardware/),
[current CoCo installation](https://github.com/confidential-containers/charts/blob/main/QUICKSTART.md).
