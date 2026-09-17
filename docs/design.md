# Design and trust boundaries

[Back to the demo](../README.md#run-the-demo)

## Requirements and project choices

The assessment requires encrypting a small open model, publishing the encrypted
artifact on Hugging Face, and loading it in Kubernetes using a mounted Secret.
Layer 2 adds an asymmetric signature verified before decryption. The choices
below implement those requirements; the CPU forward pass is additional evidence.

| Choice | Alternative | Reason for this PoC |
|---|---|---|
| BERT Tiny, about 18 MB | A larger language model | Fast CPU loading makes security checks easy to repeat. |
| Full source commit and weights SHA-256 | Following `main` | Select known files and detect unexpected weights. |
| Safetensors and built-in Transformers code | Pickled weights or custom remote code | Reduce executable deserialization and remote-code exposure. |
| AES-256-GCM, high-level API | CBC plus a separate MAC | Confidentiality and integrity without composing padding and authentication. |
| Stored ZIP, bounded in-memory encryption | Compressed or streamed formats | Keep the format and extraction rules small and testable. |
| Ed25519 | RSA | A simple signing API and fixed 64-byte signatures; no RSA interoperability requirement. |
| Jobs with explicit manifests | A Deployment or Helm templates | The task ends after loading; a continuous service and templating are unnecessary here. |
| Separate bootstrap and mounted key files | Workloads fetching Secrets through the API | Python workloads need no Kubernetes API credentials or Secret-read role. |

## Artifact format

```text
Public header / AAD (17 bytes) = CMLD (4) + version 1 (1) + nonce (12)
model.cml = header + ciphertext + GCM tag (16 bytes)
model.cml.sig = Ed25519 signature over every byte of model.cml (Layer 2)
```

`src/artifact.py` uses `cryptography==50.0.1`'s high-level `AESGCM`. The local
encryption CLI creates a fresh 256-bit key. In Kubernetes, bootstrap creates a
fresh key per run and Producer reads it from its mounted Secret. Producer creates
a random 12-byte nonce for each encryption. Never reuse a nonce with the same key.
The public header is authenticated associated data (AAD); it need not be secret.
The library appends and checks the complete tag.

Wrong keys, changed nonces/ciphertext/tags and truncation fail before package
extraction or loading. An authentication failure does not identify its cause.
The encrypted payload is a ZIP **without compression**, containing `config.json`,
`model.safetensors`, `vocab.txt`, the model card `README.md`, and `LICENSE`.
Extraction validates fixed flat names, duplicates, file types, sizes, CRC and
flags. Links and compressed entries are rejected; `extractall` is not used.
Only a fully extracted package becomes the destination.

Limits are 32 MiB per file, 64 MiB per ZIP, and 64 MiB + 33 bytes per artifact.
Several buffers coexist, so these limits are not a total RAM bound. This is
reasonable for the selected model; large models need a separately designed
streaming format. The header, total size and external filenames/revisions remain
public; the model files and internal filenames are encrypted.

## Key delivery and signatures

| Material | Provisioning | Workloads receiving it |
|---|---|---|
| AES model key | Immutable `model-key` Secret | Producer and Consumer |
| Ed25519 private key | Temporary immutable `signing-key` Secret | Producer only |
| Ed25519 public key | Immutable `producer-verification-key` ConfigMap | Consumer only |
| Hugging Face token | Temporary immutable `hf-publisher-token` Secret | Producer only |
| Kubernetes API credentials | Operator's separate kubeconfig | Bootstrap only |

All key, token and public-key mounts are read-only; `/work` is writable.
AES is already available when Consumer starts;
verifying first is a code-order guarantee, not an attestation-based key release.
Bootstrap saves private keys under
`$HOME/.config/confidential-ml-distribution/runs/<namespace>/` with mode `0600`.
Signing PEM files are not password-encrypted; the parent directory and operator
host must be trusted. Temporary signing/token Secrets are removed after Producer.
Deleting them cannot revoke credentials a process has already copied.

The operator controls the verification public key independently of the Hub.
Accepting a public key from the same mutable location as the artifact would let
an attacker replace both. Ed25519 signs the exact complete artifact, with no
custom prehash or ambiguous serialization. `verify_artifact()` returns the
verified byte buffer; Consumer decrypts that buffer without rereading the file.
Missing/invalid signatures or partial Layer 2 configuration abort without
downgrading to Layer 1. GCM and package validation remain mandatory afterwards.

GCM proves integrity relative to the shared AES key. Another AES-key holder can
make a different ciphertext with a valid tag. They cannot make the Producer's
signature without its private signing key. A signature proves possession of that
private key, not model safety, freshness, or confidentiality after an AES leak.
An older valid release still verifies. Pinning the expected commit selects the
version reproducibly; it is not a complete revocation or anti-rollback system.

## Publication and local loading

Producer uses an explicit upload list: `model.cml` and optionally `model.cml.sig`.
They are published in one Hub commit. Unexpected repository files are rejected;
a parent-commit check detects concurrent writers instead of retrying silently.
Publishing Layer 1 after Layer 2 removes the stale signature in the new commit.
Earlier pinned revisions remain available unless removed from the Hub.

Source and artifact revisions identify different repositories. Consumer downloads
the artifact and, in Layer 2, its signature anonymously from the same full artifact commit, with a
fresh temporary cache and bounded size/revision checks. Only afterwards does a
new Python subprocess load the extracted folder with its own empty cache and
offline flags. `local_files_only=True`, `trust_remote_code=False`,
`use_safetensors=True` and `weights_only=True` constrain loading. Missing files or
incomplete weights fail explicitly; there is no original-model download fallback.

The loader uses `AutoModelForPreTraining` to include both BERT pretraining heads;
a base `AutoModel` would discard them. It rejects missing, unexpected or mismatched
weights, then checks a CPU forward pass for finite output. This proves operational
loading, not useful predictions. Python socket guards in tests detect Python
network attempts; a separate `docker --network none` check validates loading with
container networking disabled. These are different forms of evidence.

## Kubernetes and threat model

A Pod groups a container with its network and volumes. A Job creates a Pod for
work that terminates; our Python programs report a result and exit. Jobs have
zero configured retries and a deadline, but Kubernetes does not promise
exactly-once execution. Bootstrap rejects ambiguous multiple-Pod results and
requires a fresh namespace. It handles key provisioning separately, so neither
workload needs a ServiceAccount token or permissions to query the Secrets API.

UID/GID/fsGroup `10001` allow non-root processes to read `0440` key mounts.
Containers have a read-only root filesystem, no privilege escalation, dropped
capabilities, resource limits, and a memory-backed `emptyDir` for temporary files.
This does not guarantee secure memory erasure or protection from a compromised
host. A malicious process that already has a key mount can read it.

The original model is public: this PoC demonstrates protected distribution of
our packaged copy, not secrecy of the original. We trust Producer, bootstrap,
Consumer configuration and host/cluster administrators. A privileged operator
may read keys or plaintext or replace code/configuration. Kubernetes Secrets use
base64 encoding, which is not encryption; API access controls and storage
protection remain important. Hub tampering fails GCM and, in Layer 2, signature
checks, but availability and a complete anti-rollback mechanism are out of scope.

References: [AESGCM 50.0.1](https://cryptography.io/en/50.0.1/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM),
[Ed25519 50.0.1](https://cryptography.io/en/50.0.1/hazmat/primitives/asymmetric/ed25519/),
[Kubernetes Secrets](https://kubernetes.io/docs/concepts/configuration/secret/).
