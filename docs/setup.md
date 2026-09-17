# Tool setup

[Back to the demo](../README.md#run-the-demo)

Run commands in the same terminal as the README, from the repository root.
`$HOME` is your own home directory. No user-specific paths or machine inventory
are required. The tested route is macOS ARM64 with a Linux ARM64 Docker engine.
Other platforms need their matching binaries; a separate Linux-machine run is
still pending.

## Python and uv

Prerequisites: Python **3.12.14** available as `python3.12`, and uv **0.8.17**;
this combination was tested on macOS ARM64. The interpreter must already be
installed: this uv version cannot automatically download that Python patch.
Install uv from its [official release](https://github.com/astral-sh/uv/releases/tag/0.8.17).
`pyproject.toml` pins direct dependencies; `uv.lock` pins the resolved environment.
Linux selects the PyTorch CPU index. The container demo was tested on Linux ARM64
with CPU-only PyTorch; other platforms have not been executed here.

If your interpreter has a different path, pass that installed Python 3.12.14
executable to `uv sync --frozen --python /path/to/python3.12`.
See [Python downloads](https://www.python.org/downloads/) for Python and the
pinned uv release linked above. Do not assume that `uv python install` from this
older uv release can provide the tested Python patch.

## Docker and Kubernetes tools

Install [Docker Desktop on macOS](https://docs.docker.com/desktop/setup/install/mac-install/)
or [Docker Engine on Linux](https://docs.docker.com/engine/install/), and
[kubectl](https://kubernetes.io/docs/tasks/tools/), before the commands below.
Use a kubectl version compatible with Kubernetes 1.34; 1.34.1 was tested here.

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

### Start Docker

From the repository root, start Docker and check its engine:

```bash
open -a Docker
export DOCKER_CONTEXT=desktop-linux
docker info --format 'os={{.OSType}} arch={{.Architecture}}'
```

Startup is asynchronous: continue only after `docker info` succeeds. These startup
commands are macOS-specific; other machines need their own working Docker context.

On Linux, select the intended local Docker engine instead of setting
`DOCKER_CONTEXT=desktop-linux`; do not run `open -a Docker`. Check `docker context
show` and require `docker info` to succeed. The user running kind must be able to
use that Docker engine. Run the build and kind commands against the same engine.

Return to [Run the demo](../README.md#run-the-demo) once `python3.12`, `uv`,
`docker`, `kubectl`, and `kind` are available. Installing kind does not create
the cluster; the README does that explicitly.
