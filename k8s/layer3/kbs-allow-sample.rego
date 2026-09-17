# DEVELOPMENT ONLY: sample evidence is not hardware-backed attestation.
# This permissive policy is intended only for the isolated assessment lab.
package policy

default allow = false

allow {
    input["tee"] == "sample"
}
