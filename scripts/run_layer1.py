"""Provision an isolated kind demo and run Producer and Consumer Jobs."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from huggingface_hub import HfApi, get_token
from huggingface_hub.utils import validate_repo_id

from artifact import _read_regular, _write_new_file
from hub_artifact import _check_revision
from signing import MAX_PEM_BYTES, generate_key_pair


class Kubernetes:
    def __init__(self, kubeconfig: Path, context: str):
        if not context.startswith("kind-"):
            raise ValueError("This bootstrap is restricted to an explicitly named kind context.")
        self.command = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context]

    def call(self, arguments: list[str], document: dict | None = None) -> str:
        process = subprocess.run(
            self.command + arguments,
            input=json.dumps(document) if document is not None else None,
            capture_output=True, text=True, timeout=75,
        )
        if process.returncode:
            # kubectl validation errors may repeat input data, including Secrets.
            raise RuntimeError(f"kubectl {arguments[0]} failed (exit {process.returncode}); output withheld.")
        return process.stdout

    def template(self, filename: str) -> dict:
        resource = json.loads(self.call([
            "create", "--dry-run=client", "--validate=false", "-f", str(ROOT / "k8s" / filename), "-o", "json",
        ]))
        # kubectl injects the current namespace even when YAML omits it.
        # Each run explicitly supplies its isolated namespace at creation.
        resource["metadata"].pop("namespace", None)
        return resource

    def create(self, resource: dict, namespace: str | None = None) -> None:
        arguments = ["create", "-f", "-", "-o", "name"]
        if namespace:
            arguments += ["-n", namespace]
        self.call(arguments, resource)

    def secret(self, filename: str, namespace: str, field: str, value: bytes, name: str | None = None) -> None:
        resource = self.template(filename)
        if name:
            resource["metadata"]["name"] = name
        resource["data"] = {field: base64.b64encode(value).decode("ascii")}
        # create, not apply: avoid duplicating secret data in last-applied annotations.
        self.create(resource, namespace)

    def verification_key(self, namespace: str, value: bytes, name: str | None = None) -> None:
        resource = self.template("producer-verification-key-configmap.yaml")
        if name:
            resource["metadata"]["name"] = name
        resource["data"] = {"public.pem": value.decode("ascii")}
        self.create(resource, namespace)

    def wait_job(self, namespace: str, name: str, record_directory: Path, sensitive: tuple[str, ...], *,
                 success: bool = True, expected_error: str = "Artifact authentication failed") -> dict:
        deadline = time.monotonic() + 360
        while time.monotonic() < deadline:
            job = json.loads(self.call(["get", "job", name, "-n", namespace, "-o", "json"]))
            terminal = next((c["type"] for c in job.get("status", {}).get("conditions", [])
                             if c["status"] == "True" and c["type"] in {"Complete", "Failed"}), None)
            if terminal:
                break
            time.sleep(2)
        else:
            self.call(["delete", "job", name, "-n", namespace, "--cascade=foreground", "--wait=true", "--ignore-not-found", "--timeout=60s"])
            raise RuntimeError(f"Job {name} timed out and was stopped.")
        pods = json.loads(self.call(["get", "pods", "-n", namespace, "-l", f"job-name={name}", "-o", "json"]))["items"]
        if len(pods) != 1:
            raise RuntimeError("Expected exactly one Pod per Job; refusing an ambiguous run.")
        pod = pods[0]
        if not any(owner["uid"] == job["metadata"]["uid"] for owner in pod["metadata"].get("ownerReferences", [])):
            raise RuntimeError("Pod is not owned by the expected Job.")
        logs = self.call(["logs", pod["metadata"]["name"], "-n", namespace])
        if any(value and value in logs for value in sensitive):
            raise RuntimeError("Sensitive material detected in workload logs; logs withheld.")
        _write_new_file(record_directory / f"{name}.log", logs.encode())
        if (terminal == "Complete") != success:
            raise RuntimeError(f"Job {name} had unexpected result {terminal}; inspect its saved log.")
        reports = []
        for line in logs.splitlines():
            try:
                report = json.loads(line)
            except ValueError:
                continue
            if isinstance(report, dict) and report.get("status") in {"published", "loaded"}:
                reports.append(report)
        if success and len(reports) != 1:
            raise RuntimeError("Expected one successful workload report.")
        if not success and (reports or not expected_error or expected_error not in logs):
            raise RuntimeError(f"Negative check did not fail at the expected check: {expected_error}.")
        containers = pod.get("status", {}).get("containerStatuses", [])
        if len(containers) != 1:
            raise RuntimeError("Expected exactly one terminated workload container.")
        terminated = containers[0].get("state", {}).get("terminated", {})
        exit_code = terminated.get("exitCode")
        if type(exit_code) is not int or ((exit_code == 0) != success):
            raise RuntimeError("Workload exit code does not match the expected result.")
        return {
            "condition": terminal, "pod": pod["metadata"]["name"], "pod_uid": pod["metadata"]["uid"],
            "image_id": containers[0]["imageID"], "exit_code": exit_code,
            "report": reports[0] if reports else None,
        }


def run(args: argparse.Namespace) -> dict:
    os.umask(0o077)
    validate_repo_id(args.repo)
    layer = getattr(args, "layer", 1)
    if layer not in {1, 2}:
        raise ValueError("Layer must be 1 or 2.")
    verify_wrong_public_key = getattr(args, "verify_wrong_public_key", False)
    if verify_wrong_public_key and layer != 2:
        raise ValueError("The wrong-public-key check requires --layer 2.")
    prefix = f"model-demo-l{layer}-"
    namespace = args.namespace or prefix + secrets.token_hex(4)
    if not re.fullmatch(prefix + r"[a-z0-9](?:[a-z0-9-]{0,45}[a-z0-9])?", namespace):
        raise ValueError(f"Use a fresh namespace beginning with {prefix}.")
    producer_template = "producer-job-layer2.yaml" if layer == 2 else "producer-job.yaml"
    consumer_template = "consumer-job-layer2.yaml" if layer == 2 else "consumer-job.yaml"
    kube = Kubernetes(args.kubeconfig, args.context)
    if kube.call(["get", "namespace", namespace, "--ignore-not-found", "-o", "name"]).strip():
        raise ValueError("Namespace already exists; use a fresh run name.")
    token = get_token()
    if not token:
        raise ValueError("Log in with hf auth login before running the Producer.")
    if HfApi(token=token).whoami().get("name") != args.repo.split("/")[0]:
        raise ValueError("Use a test repository owned by the authenticated user.")

    key_directory = Path.home() / ".config" / "confidential-ml-distribution" / "runs" / namespace
    key_directory.mkdir(parents=True, mode=0o700)
    key = AESGCM.generate_key(bit_length=256)
    _write_new_file(key_directory / "model.key", key)
    records = ROOT / "runtime" / namespace
    records.mkdir(parents=True, mode=0o700)
    sensitive = (token, key.hex(), base64.b64encode(key).decode("ascii"))
    signing_private = None
    signing_public = None
    if layer == 2:
        private_path = key_directory / "producer-signing.pem"
        public_path = records / "producer-verification.public.pem"
        generate_key_pair(private_path, public_path)
        signing_private = _read_regular(private_path, MAX_PEM_BYTES)
        signing_public = _read_regular(public_path, MAX_PEM_BYTES)
        signing_key = serialization.load_pem_private_key(signing_private, password=None)
        sensitive += (
            signing_private.decode("ascii"), base64.b64encode(signing_private).decode("ascii"),
            signing_key.private_bytes_raw().hex(),
            # PEM bodies may be logged without their surrounding header/footer.
            "".join(signing_private.decode("ascii").splitlines()[1:-1]),
        )
    print(json.dumps({"phase": "bootstrap", "namespace": namespace, "layer": layer}), flush=True)
    resource = kube.template("namespace.yaml")
    resource["metadata"]["name"] = namespace
    kube.create(resource)
    kube.secret("model-key-secret.yaml", namespace, "key", key)
    settings = kube.template("settings.yaml")
    settings["data"] = {"HF_REPO": args.repo, "HF_REVISION": ""}
    kube.create(settings, namespace)
    if layer == 2:
        kube.verification_key(namespace, signing_public)

    try:
        kube.secret("publisher-token-secret.yaml", namespace, "token", token.encode())
        if layer == 2:
            kube.secret("signing-key-secret.yaml", namespace, "private.pem", signing_private)
        kube.create(kube.template(producer_template), namespace)
        producer = kube.wait_job(namespace, "producer", records, sensitive)
    except BaseException:
        # Cancellation must stop the Pod too; deleting a Secret does not revoke
        # a credential that a still-running process has already read.
        kube.call(["delete", "job", "producer", "-n", namespace, "--cascade=foreground", "--ignore-not-found", "--wait=true", "--timeout=60s"])
        raise
    finally:
        # Deletion does not revoke credentials already read by a running process.
        temporary_secrets = ["hf-publisher-token"]
        if layer == 2:
            temporary_secrets.append("signing-key")
        kube.call(["delete", "secret", *temporary_secrets, "-n", namespace, "--ignore-not-found"])
    publication = producer["report"]
    if publication.get("status") != "published" or publication.get("repo_id") != args.repo:
        raise RuntimeError("Producer report does not match the requested repository.")
    if layer == 2 and publication.get("signature_filename") != "model.cml.sig":
        raise RuntimeError("Layer 2 Producer did not report the required signature publication.")
    revision = publication["revision"]
    _check_revision(revision)
    result = {"namespace": namespace, "layer": layer, "repo_id": args.repo,
              "artifact_revision": revision, "producer": producer}
    (records / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"phase": "published", "namespace": namespace, "revision": revision}), flush=True)
    settings["data"]["HF_REVISION"] = revision
    kube.call(["apply", "-n", namespace, "-f", "-", "-o", "name"], settings)
    kube.create(kube.template(consumer_template), namespace)
    result["consumer"] = kube.wait_job(namespace, "consumer", records, sensitive)
    report = result["consumer"]["report"]
    if report.get("status") != "loaded" or report.get("revision") != revision or report.get("repo_id") != args.repo:
        raise RuntimeError("Consumer report does not match the published revision.")
    if layer == 2 and report.get("signature_verified") is not True:
        raise RuntimeError("Layer 2 Consumer did not report successful signature verification.")
    if args.verify_wrong_key:
        kube.secret("model-key-secret.yaml", namespace, "key", AESGCM.generate_key(bit_length=256), "wrong-model-key")
        negative = kube.template(consumer_template)
        negative["metadata"]["name"] = "consumer-wrong-key"
        for volume in negative["spec"]["template"]["spec"]["volumes"]:
            if volume["name"] == "model-key":
                volume["secret"]["secretName"] = "wrong-model-key"
        kube.create(negative, namespace)
        result["wrong_key"] = kube.wait_job(namespace, "consumer-wrong-key", records, sensitive, success=False)
    if verify_wrong_public_key:
        wrong_public = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        kube.verification_key(namespace, wrong_public, "wrong-producer-verification-key")
        negative = kube.template(consumer_template)
        negative["metadata"]["name"] = "consumer-wrong-public-key"
        for volume in negative["spec"]["template"]["spec"]["volumes"]:
            if volume["name"] == "verification-key":
                volume["configMap"]["name"] = "wrong-producer-verification-key"
        kube.create(negative, namespace)
        result["wrong_public_key"] = kube.wait_job(
            namespace, "consumer-wrong-public-key", records, sensitive, success=False,
            expected_error="Signature verification failed",
        )
    (records / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--layer", type=int, choices=(1, 2), default=1)
    parser.add_argument("--namespace", help="Optional fresh model-demo-l1-* or model-demo-l2-* namespace matching --layer.")
    parser.add_argument("--kubeconfig", type=Path, default=Path.home() / ".config/confidential-ml-distribution/kubeconfig")
    parser.add_argument("--context", default="kind-model-demo")
    parser.add_argument("--verify-wrong-key", action="store_true")
    parser.add_argument("--verify-wrong-public-key", action="store_true")
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as error:
        # Network/auth exceptions can include request details: do not print them.
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        parser.exit(1, f"error: {message}. Preserve the run key and inspect this run before retrying.\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
