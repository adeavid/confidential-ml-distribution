#!/usr/bin/env bash
# Isolated, disposable GCE Ubuntu 22.04 x86_64 lab only.
# Historical compatibility baseline; Kubernetes 1.30 is end-of-life.
# Installs Kubernetes only. No CoCo, Trustee, cloud resources, or model keys.
# Usage ON THE DISPOSABLE VM: sudo bash bootstrap-kubernetes.sh INTERNAL_IP EXPECTED_PROJECT EXPECTED_VM
# Requires the repository checkout and k8s/layer3/flannel-pinned.yaml. Never run on the Mac.
# Keep GCE ingress restricted: binding an internal IP does not defeat external-IP NAT.
# Ubuntu prerequisite packages use the distribution's signed APT repositories;
# those OS package patch versions are not locked by this application lab script.
set -euo pipefail
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
trap 'printf "ERROR: bootstrap stopped at line %s; inspect the disposable VM before retrying.\n" "$LINENO" >&2' ERR

[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || die 'Linux x86_64 is required.'
[[ $EUID -eq 0 ]] || die 'Run as root on the disposable Linux VM.'
[[ $# -eq 3 ]] || die 'Usage: bootstrap-kubernetes.sh INTERNAL_IP EXPECTED_PROJECT EXPECTED_VM'
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 22.04 ]] || die 'Only Ubuntu 22.04 is prepared.'
[[ $(ps -p 1 -o comm=) == systemd ]] || die 'Run directly on a systemd host, not in a container.'

# Refuse an unrelated host. The caller must identify the disposable GCE VM.
# These metadata endpoints contain identity only, never access tokens.
python3 -I - "$2" "$3" <<'PY'
import sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
expected = {'project/project-id': sys.argv[1], 'instance/name': sys.argv[2]}
for path, value in expected.items():
    request = urllib.request.Request(
        'http://169.254.169.254/computeMetadata/v1/' + path,
        headers={'Metadata-Flavor': 'Google'})
    with opener.open(request, timeout=3) as response:
        assert response.read(256).decode() == value, 'Wrong disposable GCE lab host'
print('Disposable GCE lab identity matched')
PY

NODE_INTERNAL_IP=$1
NODE_NAME=model-demo-l3
REPOSITORY_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
FLANNEL_MANIFEST="$REPOSITORY_ROOT/k8s/layer3/flannel-pinned.yaml"
WORK_DIR=/var/lib/model-demo-kubernetes-bootstrap
FLANNEL_SHA256=220ebd9d8dfe49bd00c848eb2f71f718ef525e58cb344d8cad5100cc99b36f2b

for path in /etc/kubernetes /var/lib/etcd /etc/containerd/config.toml "$WORK_DIR"; do
  [[ ! -e $path ]] || die "Existing state found at $path; use a fresh disposable VM."
done
for binary in containerd kubelet kubeadm kubectl runc; do
  ! command -v "$binary" >/dev/null 2>&1 || die "Existing $binary installation; use a fresh VM."
done
[[ -f $FLANNEL_MANIFEST ]] || die 'Missing k8s/layer3/flannel-pinned.yaml.'
[[ $FLANNEL_SHA256 =~ ^[a-f0-9]{64}$ ]] || die 'A required manifest hash is missing.'
printf '%s  %s\n' "$FLANNEL_SHA256" "$FLANNEL_MANIFEST" | sha256sum --check --status

# Require an assigned RFC1918 address and non-overlapping local interface routes.
# Inspect the full VPC/peering route ranges separately before running the lab.
python3 -I - "$NODE_INTERNAL_IP" <<'PY'
import ipaddress, json, subprocess, sys
address = ipaddress.IPv4Address(sys.argv[1])
private = [ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
assert any(address in network for network in private), 'An RFC1918 internal IPv4 is required'
interfaces = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show']))
assigned = [entry for interface in interfaces for entry in interface.get('addr_info', [])]
assert any(entry['local'] == str(address) for entry in assigned), 'IP is not assigned to this VM'
lab_networks = [ipaddress.ip_network(n) for n in ('10.244.0.0/16', '10.96.0.0/12')]
for entry in assigned:
    network = ipaddress.ip_network(f"{entry['local']}/{entry['prefixlen']}", strict=False)
    assert not any(network.overlaps(lab) for lab in lab_networks), 'Lab CIDR overlaps a host interface'
print('Internal IP and local CIDR checks passed')
PY

modprobe kvm_intel
[[ -c /dev/kvm ]] || die '/dev/kvm is unavailable.'
python3 -I - <<'PY'
import fcntl, os
fd = os.open('/dev/kvm', os.O_RDWR)
assert fcntl.ioctl(fd, 0xAE00, 0) == 12, 'Unsupported KVM API'
vmfd = fcntl.ioctl(fd, 0xAE01, 0)
os.close(vmfd)
os.close(fd)
print('KVM_CREATE_VM passed; this is not a Kata boot test')
PY

install -d -m 700 "$WORK_DIR" "$WORK_DIR/downloads" "$WORK_DIR/patches"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl conntrack socat iptables ethtool ebtables

# SHA-256 pins below were checked against the versioned official release files.
# No missing hash, failed download, or mismatch is allowed to fall back to latest.
fetch() {
  local name=$1 url=$2 checksum=$3
  [[ $checksum =~ ^[a-f0-9]{64}$ ]] || die "Missing SHA-256 for $name"
  curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
    --connect-timeout 20 --max-time 600 --retry 3 "$url" \
    --output "$WORK_DIR/downloads/$name"
  printf '%s  %s\n' "$checksum" "$WORK_DIR/downloads/$name" | sha256sum --check --status
  printf 'Verified %s\n' "$name"
}
fetch containerd.tar.gz https://github.com/containerd/containerd/releases/download/v1.7.22/containerd-1.7.22-linux-amd64.tar.gz \
  f8b2d935d1f86003f4e0c1af3b9f0d2820bacabe6dc9f562785b74af24c5e468
fetch runc https://github.com/opencontainers/runc/releases/download/v1.1.14/runc.amd64 \
  a83c0804ebc16826829e7925626c4793da89a9b225bbcc468f2b338ea9f8e8a8
fetch cni.tar.gz https://github.com/containernetworking/plugins/releases/download/v1.5.1/cni-plugins-linux-amd64-v1.5.1.tgz \
  77baa2f669980a82255ffa2f2717de823992480271ee778aa51a9c60ae89ff9b
fetch crictl.tar.gz https://github.com/kubernetes-sigs/cri-tools/releases/download/v1.30.1/crictl-v1.30.1-linux-amd64.tar.gz \
  71873cdeeeb6c9ee0f79c27b45db38066da81f0c30dcda909b4eedc3aff63f59
fetch kubeadm https://dl.k8s.io/release/v1.30.1/bin/linux/amd64/kubeadm \
  651faa3bbbfb368ed00460e4d11732614310b690b767c51810a7b638cc0961a2
fetch kubelet https://dl.k8s.io/release/v1.30.1/bin/linux/amd64/kubelet \
  87bd6e5de9c0769c605da5fedb77a35c8b764e3bda1632447883c935dcf219d3
fetch kubectl https://dl.k8s.io/release/v1.30.1/bin/linux/amd64/kubectl \
  5b86f0b06e1a5ba6f8f00e2b01e8ed39407729c4990aeda961f83a586f975e8a
fetch containerd.service https://raw.githubusercontent.com/containerd/containerd/v1.7.22/containerd.service \
  4576c080ab8a3eec318b4ea9017aba47b929b6de148fa442d78b8b3d21e5479b
fetch kubelet.service https://raw.githubusercontent.com/kubernetes/release/v0.16.9/cmd/krel/templates/latest/kubelet/kubelet.service \
  ccc966cee3d366b3e9ca0fe3f603b09218a0d5ed01e54f9cae953208f411141f
fetch 10-kubeadm.conf https://raw.githubusercontent.com/kubernetes/release/v0.16.9/cmd/krel/templates/latest/kubeadm/10-kubeadm.conf \
  46458e5b4adf851099e591acb3248931fc5f4073976fedb11c49d6bfad5c41a8

swapoff --all
cp /etc/fstab "$WORK_DIR/fstab.before"
sed -i -E '/^[[:space:]]*#/! s@^([^#]+[[:space:]]swap[[:space:]].*)$@# model-demo lab disabled swap: \1@' /etc/fstab
cat >/etc/modules-load.d/model-demo-kubernetes.conf <<'EOF'
overlay
br_netfilter
vhost_vsock
vhost_net
EOF
for module in overlay br_netfilter vhost_vsock vhost_net; do modprobe "$module"; done
cat >/etc/sysctl.d/99-model-demo-kubernetes.conf <<'EOF'
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
EOF
sysctl --load=/etc/sysctl.d/99-model-demo-kubernetes.conf

tar -xzf "$WORK_DIR/downloads/containerd.tar.gz" -C /usr/local
install -m 755 "$WORK_DIR/downloads/runc" /usr/local/sbin/runc
install -d -m 755 /opt/cni/bin /etc/containerd /etc/systemd/system/kubelet.service.d
tar -xzf "$WORK_DIR/downloads/cni.tar.gz" -C /opt/cni/bin
tar -xzf "$WORK_DIR/downloads/crictl.tar.gz" -C /usr/local/bin
for binary in kubeadm kubelet kubectl; do install -m 755 "$WORK_DIR/downloads/$binary" "/usr/local/bin/$binary"; done
install -m 644 "$WORK_DIR/downloads/containerd.service" /etc/systemd/system/containerd.service
sed 's#/usr/bin/kubelet#/usr/local/bin/kubelet#g' "$WORK_DIR/downloads/kubelet.service" >/etc/systemd/system/kubelet.service
sed 's#/usr/bin/kubelet#/usr/local/bin/kubelet#g' "$WORK_DIR/downloads/10-kubeadm.conf" >/etc/systemd/system/kubelet.service.d/10-kubeadm.conf
containerd config default >"$WORK_DIR/containerd-default.toml"
python3 -I - "$WORK_DIR/containerd-default.toml" <<'PY'
from pathlib import Path
import re, sys
text = Path(sys.argv[1]).read_text()
text, count = re.subn(r'SystemdCgroup = false', 'SystemdCgroup = true', text)
assert count == 1, 'Unexpected containerd cgroup configuration'
text, count = re.subn(r'sandbox_image = "[^"]+"', 'sandbox_image = "registry.k8s.io/pause@sha256:8d4106c88ec0bd28001e34c975d65175d994072d65341f62a8ab0754b0fafe10"', text)
assert count == 1, 'Unexpected containerd sandbox configuration'
Path('/etc/containerd/config.toml').write_text(text)
PY
cat >/etc/crictl.yaml <<'EOF'
runtime-endpoint: unix:///run/containerd/containerd.sock
image-endpoint: unix:///run/containerd/containerd.sock
timeout: 20
EOF
systemctl daemon-reload
systemctl enable --now containerd
crictl info >"$WORK_DIR/cri-info.json"

# Registry response bytes were hashed locally; these are Linux AMD64 manifests.
# Preload by immutable digest and provide the expected kubeadm tags locally.
cat >"$WORK_DIR/images.txt" <<'EOF'
registry.k8s.io/kube-apiserver:v1.30.1 sha256:a9cf4f4eb92ef02b0a8ba4148f50b4a1b2bd3e9b28a8f9913ea8c3bcc08e610c
registry.k8s.io/kube-controller-manager:v1.30.1 sha256:110a010162e119e768e13bb104c0883fb4aceb894659787744abf115fcc56027
registry.k8s.io/kube-scheduler:v1.30.1 sha256:8ebcbcb8ecc9fc76029ac1dc12f3f15e33e6d26f018d49d5db4437f3d4b34973
registry.k8s.io/kube-proxy:v1.30.1 sha256:2eec8116ed9b8f46b6a90a46434711354d2222575ab50a4aca42bb6ab19989fa
registry.k8s.io/pause:3.9 sha256:8d4106c88ec0bd28001e34c975d65175d994072d65341f62a8ab0754b0fafe10
registry.k8s.io/etcd:3.5.12-0 sha256:2e6b9c67730f1f1dce4c6e16d60135e00608728567f537e8ff70c244756cbb62
registry.k8s.io/coredns/coredns:v1.11.1 sha256:2169b3b96af988cf69d7dd69efbcc59433eb027320eb185c6110e0850b997870
EOF
while read -r image digest; do
  [[ $digest =~ ^sha256:[a-f0-9]{64}$ ]] || die "Missing image digest for $image"
  immutable="${image%:*}@$digest"
  ctr --namespace k8s.io images pull --platform linux/amd64 "$immutable"
  ctr --namespace k8s.io images tag "$immutable" "$image"
  component=${image##*/}
  component=${component%%:*}
  case "$component" in
    kube-apiserver|kube-controller-manager|kube-scheduler|etcd)
      cat >"$WORK_DIR/patches/$component+strategic.yaml" <<EOF
spec:
  containers:
    - name: $component
      image: $immutable
EOF
      ;;
  esac
done <"$WORK_DIR/images.txt"

cat >"$WORK_DIR/kubeadm.yaml" <<EOF
apiVersion: kubeadm.k8s.io/v1beta3
kind: InitConfiguration
localAPIEndpoint:
  advertiseAddress: $NODE_INTERNAL_IP
  bindPort: 6443
nodeRegistration:
  name: $NODE_NAME
  criSocket: unix:///run/containerd/containerd.sock
  imagePullPolicy: IfNotPresent
  kubeletExtraArgs:
    node-ip: $NODE_INTERNAL_IP
patches:
  directory: $WORK_DIR/patches
---
apiVersion: kubeadm.k8s.io/v1beta3
kind: ClusterConfiguration
kubernetesVersion: v1.30.1
clusterName: model-demo-l3
networking:
  podSubnet: 10.244.0.0/16
  serviceSubnet: 10.96.0.0/12
apiServer:
  extraArgs:
    bind-address: $NODE_INTERNAL_IP
  certSANs:
    - $NODE_INTERNAL_IP
---
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: systemd
EOF
kubeadm config images list --config "$WORK_DIR/kubeadm.yaml" | sort >"$WORK_DIR/expected-images.txt"
cut -d ' ' -f 1 "$WORK_DIR/images.txt" | sort >"$WORK_DIR/pinned-images.txt"
diff -u "$WORK_DIR/pinned-images.txt" "$WORK_DIR/expected-images.txt"
systemctl enable --now kubelet
kubeadm init --config "$WORK_DIR/kubeadm.yaml" --skip-token-print
export KUBECONFIG=/etc/kubernetes/admin.conf
chmod 600 "$KUBECONFIG"

# The add-ons first use preloaded tags; pin their controller templates for restarts.
kubectl --kubeconfig "$KUBECONFIG" -n kube-system set image daemonset/kube-proxy \
  kube-proxy=registry.k8s.io/kube-proxy@sha256:2eec8116ed9b8f46b6a90a46434711354d2222575ab50a4aca42bb6ab19989fa
kubectl --kubeconfig "$KUBECONFIG" -n kube-system set image deployment/coredns \
  coredns=registry.k8s.io/coredns/coredns@sha256:2169b3b96af988cf69d7dd69efbcc59433eb027320eb185c6110e0850b997870
kubectl --kubeconfig "$KUBECONFIG" taint node "$NODE_NAME" node-role.kubernetes.io/control-plane:NoSchedule-
kubectl --kubeconfig "$KUBECONFIG" label node "$NODE_NAME" node.kubernetes.io/worker=
kubectl --kubeconfig "$KUBECONFIG" apply -f "$FLANNEL_MANIFEST"
kubectl --kubeconfig "$KUBECONFIG" -n kube-flannel rollout status daemonset/kube-flannel-ds --timeout=300s
kubectl --kubeconfig "$KUBECONFIG" wait --for=condition=Ready node/"$NODE_NAME" --timeout=300s
kubectl --kubeconfig "$KUBECONFIG" -n kube-system rollout status deployment/coredns --timeout=300s
kubectl --kubeconfig "$KUBECONFIG" -n kube-system rollout status daemonset/kube-proxy --timeout=300s
kubectl --kubeconfig "$KUBECONFIG" get nodes -o wide
printf 'Kubernetes bootstrap finished. CoCo, Kata and Trustee are not installed.\n'
printf 'Admin kubeconfig stays at /etc/kubernetes/admin.conf; do not print or commit it.\n'
