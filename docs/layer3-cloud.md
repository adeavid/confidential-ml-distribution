# Disposable Google Compute Engine node

[Back to Layer 3](layer3.md)

This optional route creates billable resources in **your own authorized project**.
The base Layer 1/2 demo needs no cloud account. Use a separate lab project with
billing already enabled, Google Cloud CLI installed and your user authenticated.
The tested CLI was 585.0.0. Do not place account credentials in this repository.

The tested VM was `n2-standard-4` (4 vCPU, 16 GiB), with a 50 GB balanced disk,
Ubuntu 22.04 and nested virtualization explicitly enabled. A generic VPS is not
equivalent: the guest must be able to use KVM. This is CPU-only development
attestation; no confidential hardware is claimed.

## Create the network and node

Choose your project and resource names. Check VPC/peering routes before using
these ranges; they must not overlap Kubernetes Pods (`10.244.0.0/16`) or Services
(`10.96.0.0/12`). The commands below assume these names do not exist yet.

```bash
export LAB_PROJECT='<your-authorized-project>'
export LAB_REGION='us-east1'
export LAB_ZONE='us-east1-b'
export LAB_VM='model-coco-dev'

gcloud --project="$LAB_PROJECT" services enable \
  compute.googleapis.com iap.googleapis.com oslogin.googleapis.com
gcloud --project="$LAB_PROJECT" compute networks create model-lab-net \
  --subnet-mode=custom
gcloud --project="$LAB_PROJECT" compute networks subnets create model-lab-subnet \
  --network=model-lab-net --region="$LAB_REGION" --range=10.51.0.0/24
gcloud --project="$LAB_PROJECT" compute firewall-rules create model-lab-iap-ssh \
  --network=model-lab-net --direction=INGRESS --action=ALLOW \
  --rules=tcp:22 --source-ranges=35.235.240.0/20 --target-tags=model-lab-iap

gcloud --project="$LAB_PROJECT" compute instances create "$LAB_VM" \
  --zone="$LAB_ZONE" --machine-type=n2-standard-4 \
  --image=ubuntu-2204-jammy-v20260906 --image-project=ubuntu-os-cloud \
  --boot-disk-size=50GB --boot-disk-type=pd-balanced --boot-disk-auto-delete \
  --network=model-lab-net --subnet=model-lab-subnet --tags=model-lab-iap \
  --enable-nested-virtualization --no-service-account --no-scopes \
  --metadata=enable-oslogin=TRUE,block-project-ssh-keys=TRUE \
  --max-run-duration=8h --instance-termination-action=STOP --no-restart-on-failure \
  --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
  --labels=purpose=confidential-ml-lab,environment=development
```

The custom network permits inbound SSH only from IAP; Kubernetes and KBS have no
public ingress rule. The ephemeral external IPv4 supplies outbound Internet
access without adding a NAT gateway. No Google service account is attached to
the VM. An 8-hour stop is a backstop, not a total spending cap: disks and any
retained chargeable resources can continue billing after a VM stops.

The VM/network settings above were used in the lab. Project/resource names in
this guide are user inputs or generic examples. Zone capacity varies: four
attempts in another region returned `ZONE_RESOURCE_POOL_EXHAUSTED` before the
tested zone succeeded. Check failed operations and leftover resources before
retrying; do not automatically upgrade to a larger machine.

## Connect and continue

The signed-in user needs permission to create these project resources, use IAP
TCP forwarding and use OS Login with administrative access. Project creation
alone does not grant those roles in an organization's restricted project.

```bash
gcloud --project="$LAB_PROJECT" compute instances describe "$LAB_VM" \
  --zone="$LAB_ZONE" \
  --format='value(networkInterfaces[0].networkIP)'
gcloud --project="$LAB_PROJECT" compute ssh "$LAB_VM" \
  --zone="$LAB_ZONE" --tunnel-through-iap
```

On the VM, obtain the reviewed repository revision:

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/adeavid/confidential-ml-distribution.git
cd confidential-ml-distribution
git rev-parse HEAD
```

The optional Layer 3 files must be present in that revision. If reviewing an
unpublished change, transfer the reviewed source checkout instead; cloning the
older public branch will not contain that change. Follow
[the Layer 3 node bootstrap](layer3.md#1-prepare-the-disposable-linux-node), passing
the expected project, VM name and internal IP. The script refuses an unexpected
host, existing Kubernetes state or unusable KVM. Do not copy a personal cloud
credential file onto the node.

## Stop or remove the lab

From your authenticated operator machine, after recording the needed evidence:

```bash
# Stop compute usage while retaining the disposable disk for a later session.
gcloud --project="$LAB_PROJECT" compute instances stop "$LAB_VM" --zone="$LAB_ZONE"

# When finished permanently, delete the VM and its auto-delete boot disk.
gcloud --project="$LAB_PROJECT" compute instances delete "$LAB_VM" --zone="$LAB_ZONE"
gcloud --project="$LAB_PROJECT" compute disks list
gcloud --project="$LAB_PROJECT" compute addresses list
```

Run the deletion command only when the retained lab data is no longer needed.
Remove this lab's firewall, subnet and network after the VM is gone; do not
delete unrelated project resources. KBS uses ephemeral memory volumes, so plan
to re-provision its resources/policy after a new Pod or node restart.

Check actual billing in your account. At the reference US on-demand prices,
this configuration was approximately US$0.21/hour while running, before taxes
and outbound traffic; regional prices, currency, credits and eligibility vary.
No real billed total is inferred from this estimate.

References: [nested virtualization](https://docs.cloud.google.com/compute/docs/instances/nested-virtualization/overview),
[runtime limits](https://docs.cloud.google.com/compute/docs/instances/limit-vm-runtime),
[VM pricing](https://cloud.google.com/products/compute/pricing/general-purpose),
[disk pricing](https://cloud.google.com/compute/disks-image-pricing),
[IPv4 pricing](https://cloud.google.com/vpc/network-pricing#ipaddress).
