"""Render the optional Kata Consumer Job locally; never apply it or read credentials."""

import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Public Linux amd64 OCI image, pinned by digest.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--kbs-url", required=True, help="Lab ClusterIP URL, e.g. http://10.96.0.10:8080.")
    parser.add_argument("--name", help="Fresh Job name; defaults to consumer-l3 or consumer-l3-signed.")
    parser.add_argument("--signed", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9./_-]*@sha256:[0-9a-f]{64}", args.image):
        parser.error("--image must be an OCI reference pinned by SHA-256 digest.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo must be owner/model.")
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        parser.error("--revision must be a full Hub commit.")
    name = args.name or ("consumer-l3-signed" if args.signed else "consumer-l3")
    if not re.fullmatch(r"consumer-l3(?:[a-z0-9-]{0,39}[a-z0-9])?", name):
        parser.error("Use a Job name beginning with consumer-l3.")
    try:
        url = urlsplit(args.kbs_url)
        address = ipaddress.IPv4Address(url.hostname)
        private = any(address in ipaddress.ip_network(n) for n in
                      ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
        valid_url = (url.scheme == "http" and url.port == 8080 and private
                     and not any((url.username, url.password, url.path, url.query, url.fragment)))
    except (ValueError, TypeError):
        valid_url = False
    if not valid_url:
        parser.error("--kbs-url must be the isolated lab's HTTP private IPv4 on port 8080.")

    directory = Path(__file__).resolve().parents[2] / "k8s" / "layer3"
    command = ["kubectl", "patch", "--local", "-f", str(directory / "consumer-job.yaml"), "-o", "json"]
    if args.signed:
        command += ["--type=strategic", "--patch-file", str(directory / "consumer-signed-patch.yaml")]
    else:
        command += ["--type=merge", "--patch", "{}"]
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    job = json.loads(result.stdout)
    job["metadata"]["name"] = name
    pod = job["spec"]["template"]
    pod["metadata"]["annotations"]["io.katacontainers.config.hypervisor.kernel_params"] = (
        "agent.aa_kbc_params=cc_kbc::" + args.kbs_url)
    container = pod["spec"]["containers"][0]
    container["image"] = args.image
    container["args"] = [
        {"HUB_REPOSITORY": args.repo, "HUB_REVISION": args.revision}.get(value, value)
        for value in container["args"]
    ]
    print(json.dumps(job, indent=2))


if __name__ == "__main__":
    main()
