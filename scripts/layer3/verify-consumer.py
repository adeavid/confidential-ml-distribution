"""Read one completed Layer 3 Job using the inherited KUBECONFIG; never print raw logs."""

import argparse
import json
import re
import subprocess
from uuid import UUID

NAMESPACE = "model-demo-l3-lab"
IMAGE = r"[a-z0-9][a-z0-9./_-]*@sha256:[0-9a-f]{64}"


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def kubectl(*arguments):
    # Preserve CRLF bytes so truncation cannot hide behind text-mode normalization.
    return subprocess.run(["kubectl", "--request-timeout=20s", "-n", NAMESPACE, *arguments],
                          check=True, capture_output=True, timeout=30).stdout.decode("utf-8")


def verify(args):
    job = json.loads(kubectl("get", "job", args.job, "-o", "json"))
    job_uid = str(UUID(job["metadata"]["uid"]))
    check(job["metadata"]["name"] == args.job and job["metadata"]["namespace"] == NAMESPACE, "Unexpected Job identity.")
    allowed = args.expect == "allowed"
    terminal = {c["type"] for c in job.get("status", {}).get("conditions", [])
                if c.get("status") == "True" and c["type"] in {"Complete", "Failed"}}
    check(terminal == {"Complete" if allowed else "Failed"}, "Job is not in the expected terminal state.")
    pods = json.loads(kubectl("get", "pods", "-l", f"job-name={args.job}", "-o", "json"))["items"]
    check(len(pods) == 1, "Expected exactly one Pod for this Job.")
    pod = pods[0]
    check(pod["metadata"]["namespace"] == NAMESPACE and any(
        owner.get("uid") == job_uid and owner.get("name") == args.job
        and owner.get("kind") == "Job" and owner.get("controller") is True
        for owner in pod["metadata"].get("ownerReferences", [])), "Pod is not controlled by this Job UID.")
    pod_uid = str(UUID(pod["metadata"]["uid"]))
    spec = pod["spec"]
    check(spec.get("runtimeClassName") == "kata-qemu-coco-dev" and spec.get("automountServiceAccountToken") is False,
          "Pod must use the CoCo dev runtime without an automatic ServiceAccount token.")
    check(not any("secret" in volume or any("secret" in source for source in volume.get("projected", {}).get("sources", []))
                  for volume in spec.get("volumes", [])), "Pod must not mount a Secret.")
    containers, states = spec["containers"], pod.get("status", {}).get("containerStatuses", [])
    check(len(containers) == len(states) == 1 and containers[0]["name"] == states[0]["name"] == "consumer"
          and not spec.get("initContainers") and not spec.get("ephemeralContainers"), "Expected only the Consumer container.")
    container, state = containers[0], states[0]
    check(container["image"] == args.image, "Pod image differs from the expected pinned reference.")
    flags = container.get("args", [])
    check(flags.count("--cdh-resource") == 1 and flags[flags.index("--cdh-resource") + 1] == "default/key/my-model"
          and not any(flag.split("=", 1)[0] == "--key-file" for flag in flags), "Pod must select CDH without a key file.")
    check(flags.count("--require-signature") == int(args.signed), "Pod signature mode differs from the expected mode.")
    check(flags.count("--revision") == 1 and flags[flags.index("--revision") + 1] == args.revision, "Pod Hub revision differs.")
    exit_code = state.get("state", {}).get("terminated", {}).get("exitCode")
    check(type(exit_code) is int and exit_code == (0 if allowed else 1), "Unexpected Consumer termination or exit code.")
    image_id = state.get("imageID", "")
    check(re.fullmatch(r"(?:[a-z-]+://)?(?:[a-z0-9][a-z0-9./_-]*@)?sha256:[0-9a-f]{64}", image_id), "Missing or malformed image ID.")
    logs = kubectl("logs", f"pod/{pod['metadata']['name']}", "-c", "consumer", "--limit-bytes=65537")
    check(len(logs.encode()) <= 65536, "Consumer logs exceed the verification limit.")
    reports = [json.loads(line) for line in logs.splitlines() if line.lstrip().startswith("{")]
    reports = [report for report in reports if isinstance(report, dict) and report.get("status") == "loaded"]
    summary = dict(namespace=NAMESPACE, job=args.job, pod_uid=pod_uid, image_id=image_id,
                   exit_code=exit_code, expected=args.expect, revision=args.revision, key_source="cdh")
    if allowed:
        check(len(reports) == 1, "Expected exactly one loaded report.")
        report = reports[0]
        check(report.get("revision") == args.revision and report.get("key_source") == "cdh"
              and report.get("signature_verified", False) is args.signed, "Unexpected revision, key source or signature result.")
        model = report["model"]
        check(model.get("device") == "cpu" and model.get("finite_output") is True, "CPU forward check did not pass.")
        check(all(type(model.get(k)) is int and 0 < model[k] < 10**9 for k in ("parameter_count", "input_tokens"))
              and isinstance(model.get("output_shape"), list) and len(model["output_shape"]) == 3
              and all(type(n) is int and 0 < n < 10**6 for n in model["output_shape"]), "Malformed model counts or shape.")
        summary["model"] = {k: model[k] for k in ("device", "finite_output", "parameter_count", "input_tokens", "output_shape")}
        summary["signature_verified"] = args.signed
    else:
        check(not reports and not re.search(r'"status"\s*:\s*"loaded"', logs) and re.search(
            r"(?m)^error: CDH key retrieval failed \(HTTP (403|500)\); no Secret fallback is permitted\.$", logs),
            "Expected CDH HTTP denial without a loaded report; policy cause requires KBS evidence.")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("job", "revision", "image"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--expect", choices=("allowed", "denied"), required=True)
    parser.add_argument("--signed", action="store_true")
    args = parser.parse_args()
    if not (re.fullmatch(r"consumer-l3(?:[a-z0-9-]{0,39}[a-z0-9])?", args.job)
            and re.fullmatch(r"[0-9a-f]{40}", args.revision) and re.fullmatch(IMAGE, args.image)):
        parser.error("Use a consumer-l3 Job name, full Hub commit and digest-pinned image.")
    try:
        print(json.dumps(verify(args), sort_keys=True))
    except RuntimeError as error:
        parser.exit(1, f"error: {error}\n")
    except (KeyError, IndexError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        parser.exit(1, "error: Could not verify Kubernetes records; raw output withheld.\n")


if __name__ == "__main__":
    main()
