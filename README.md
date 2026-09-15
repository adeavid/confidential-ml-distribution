# Confidential ML Model Distribution PoC

An incremental implementation of a technical assessment. The objective is a
small, reproducible model distribution pipeline whose decisions can be explained
and whose failure cases can be demonstrated.

**Current status:** milestone 1 infrastructure checks passed: one local node is
Ready and all nine system Pods are Running. The Producer and Consumer are not
implemented yet. No assessment layer is verified.
See [verification](#verification) for executed checks and pending work.

## Scope and acceptance

The assessment PDF is the source of requirements. Only Layer 1 is mandatory;
Layers 2 and 3 are optional and independent. This project will complete and
validate Layer 1 before adding Layer 2. Layer 3 will only be evaluated afterwards,
subject to environment compatibility and time for a real test.

| Requirement | Planned implementation | Acceptance evidence | Status |
| --- | --- | --- | --- |
| L1: select and encrypt a small open HF model | Pin its source commit; package required files; encrypt with AES-256-GCM | Local round trip; reject wrong key, tampering, truncation, and unsafe archive entries | Pending |
| L1: publish the encrypted artifact on HF Hub | Upload an explicit file list to an authorized test repository | Record the resulting full commit ID; inspect uploaded file names | Pending |
| L1: deliver the key as a Kubernetes Secret | A documented bootstrap step provisions the Secret; Consumer mounts it read-only | Fresh Pod reads the key file without Kubernetes API credentials | Pending |
| L1: download, decrypt, and load in Kubernetes | Consumer Job downloads the pinned revision and loads only the decrypted directory | Fresh Job completes a CPU forward pass; missing local files fail without fallback | Pending |
| L2: sign and verify before decryption | Ed25519 signature over the complete encrypted artifact; controlled public key | Invalid/missing signature or wrong public key aborts before decryption | Pending |
| Delivery: public Git repo, two Dockerfiles, manifests, README | Add each artifact with its corresponding milestone | Repeat the documented demo from a clean environment | Pending |

AES-GCM, Ed25519, a single-node kind cluster, and Jobs are project choices, not
algorithms or tools prescribed by the assessment. The CPU forward pass is extra
evidence of a successful load; model loading itself is the formal requirement.

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

The diagram describes the target design, not an already executed pipeline.
The bootstrap handoff of the generated key will be implemented explicitly with
the Producer; the model process itself does not need permission to create
Secrets through the Kubernetes API.

- **Pod:** Kubernetes' smallest deployable unit. It groups one or more containers
  with their network and storage configuration. Our Consumer Pod will have one
  container running Python and a read-only volume exposing the key as a file.
- **Job:** creates a Pod for a task that finishes. The Consumer loads and checks
  a model, then exits; a continuously running service is unnecessary here.
- **Secret:** holds the AES key separately from the image and public artifact.
  Base64 is an encoding, not encryption. Cluster access control and storage
  protection still matter.

Layer 1 separates access to the stored artifact from access to the decryption
key. Someone who only obtains the encrypted artifact cannot recover its contents
without the key, assuming the encryption and key handling are sound.

The original model is public. This PoC demonstrates secure distribution of our
packaged copy; it cannot make the public original secret. Layer 1 trusts the
Producer, bootstrap operator, Consumer configuration, and host/cluster
administrators. A privileged administrator may read the key or decrypted model.
Containers and a local cluster do not remove that trust.

## Local development cluster

The documented setup uses Docker Desktop on macOS ARM64. One kind node keeps
the infrastructure small. A managed cloud cluster would add
cost and credentials without being necessary for the base assessment.

On macOS, Docker Desktop supplies a Linux environment. kind creates a Docker
container that acts as our Kubernetes node. `kubectl` talks to that cluster using
the endpoint and credentials stored in its kubeconfig file.

Tested tooling (kind and the node image are explicitly pinned):

- Docker Engine 29.1.3.
- kind v0.33.0.
- Kubernetes v1.34.11, pinned by image digest in `k8s/kind.yaml`.
- kubectl v1.34.1.

The node version keeps the existing kubectl on the same Kubernetes minor version.
Python, model, and application dependency versions will be selected and tested
in later milestones. Application dependencies are not installed yet.

### Install the pinned kind binary (macOS ARM64)

Use a project-specific tools directory outside the Git repository:

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

For another supported OS/architecture, use the matching v0.33.0 release binary
and checksum from the [official release](https://github.com/kubernetes-sigs/kind/releases/tag/v0.33.0).
Do not use the macOS ARM64 binary or its checksum on another platform.

### Start and check the cluster

Run from the repository root. Keep cluster access credentials outside the repo.
The explicit kubeconfig avoids changing the user's default Kubernetes context.

```bash
open -a Docker
export DOCKER_CONTEXT=desktop-linux
docker info --format 'os={{.OSType}} arch={{.Architecture}}'
```

Docker Desktop starts asynchronously. Continue only after `docker info` succeeds;
if it reports an unavailable socket, let Docker finish starting and repeat it.

```bash
umask 077
export MODEL_DEMO_KUBECONFIG="$HOME/.config/confidential-ml-distribution/kubeconfig"
mkdir -p "$(dirname "$MODEL_DEMO_KUBECONFIG")"
KIND_EXPERIMENTAL_PROVIDER=docker kind create cluster \
  --name model-demo \
  --config k8s/kind.yaml \
  --kubeconfig "$MODEL_DEMO_KUBECONFIG" \
  --wait 180s

kubectl --kubeconfig "$MODEL_DEMO_KUBECONFIG" --context kind-model-demo \
  wait --for=condition=Ready node --all --timeout=120s
kubectl --kubeconfig "$MODEL_DEMO_KUBECONFIG" --context kind-model-demo \
  wait --for=condition=Ready pod --all --all-namespaces --timeout=120s
kubectl --kubeconfig "$MODEL_DEMO_KUBECONFIG" --context kind-model-demo get nodes
kubectl --kubeconfig "$MODEL_DEMO_KUBECONFIG" --context kind-model-demo \
  get pods --all-namespaces
```

`open -a Docker` and `desktop-linux` are specific to this macOS setup. Other
machines need a running supported container runtime and its own context.

Future application commands will also select a dedicated `model-demo` namespace.
No application workloads or namespace are created in this milestone.

### Verification

Observed results from the commands above:

- kind installation: the binary SHA-256 matched the official release checksum.
- Cluster creation and readiness checks: exit 0; one node Ready and all nine
  system Pods Ready and Running.
- The API port was bound to loopback only; the separate kubeconfig had mode 0600.

These checks verify the local infrastructure only. Model loading, encryption,
HF publication, application Jobs, signing, and attestation have not been tested.
The full setup has not yet been repeated on a second clean machine.

### Troubleshooting and cleanup

- **Docker socket unavailable:** start Docker Desktop and wait until `docker
  info` succeeds before creating the cluster.
- **Cluster already exists:** inspect it using the explicit kubeconfig and
  context; do not create or replace unrelated clusters.
- **Image download fails:** inspect the reported network/registry error. The
  pinned image must be available; do not silently substitute an untested tag.
- **Node is not Ready:** inspect system Pods and Docker's available resources.
  A Ready node is infrastructure evidence, not proof of the model pipeline.

When the local cluster is no longer needed, the following removes only this
project's cluster and all of its in-cluster resources:

```bash
DOCKER_CONTEXT=desktop-linux KIND_EXPERIMENTAL_PROVIDER=docker kind delete cluster \
  --name model-demo --kubeconfig "$HOME/.config/confidential-ml-distribution/kubeconfig"
```

## Credentials and publication

- Keep real AES keys, signing private keys, Hub tokens, and kubeconfig files
  outside this repository. Never put them in command arguments, logs, images,
  or committed YAML.
- `.gitignore` provides defensive exclusions. It does not protect already
  tracked files, Docker builds, or Hub uploads.
- `.dockerignore` denies files by default and permits only planned source and
  dependency declarations. Future Dockerfiles will use explicit `COPY` paths.
- `artifacts/` and `runtime/` are reserved ignored paths for generated public
  outputs and local working files. Neither directory exists yet.
- The AES key decrypts the artifact. The HF token authorizes Hub operations.
  The optional signing private key identifies the Producer. They are distinct.
- Public Git and Hub destinations must be identified and authorized before any
  publication. No model, key, or artifact has been published.

## Next milestones

1. Load a real small model locally from a fixed source commit.
2. Add authenticated encryption and fast negative tests.
3. Publish and retrieve only the intended encrypted files on HF Hub.
4. Run Producer and Consumer in Kubernetes, including Secret provisioning.
5. Reproduce and explain Layer 1, then add Layer 2.

Layer 3 is deferred. This macOS/kind setup has not been validated for Kata/CoCo.
Development sample attestation does not prove hardware-backed isolation from
the host. Production rotation/revocation, strict attestation policy, and real
confidential hardware remain possible extensions.

## References

- [Kubernetes Secrets](https://kubernetes.io/docs/concepts/configuration/secret/)
- [Kubernetes Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/)
- [kind quick start](https://kind.sigs.k8s.io/docs/user/quick-start/)
- [CoCo development-mode reference](https://confidentialcontainers.org/blog/2024/12/03/confidential-containers-without-confidential-hardware/)
