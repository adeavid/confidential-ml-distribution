"""Read one AES key from the guest's CDH REST API, without a file fallback."""

import re

import httpx


CDH_ORIGIN = "http://127.0.0.1:8006"
KEY_BYTES = 32


def validate_cdh_resource(resource: str) -> None:
    """Accept a bounded repository/type/tag identifier, never a caller's URL."""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_-]{1,64}", resource) is None:
        raise ValueError("CDH resource must be repository/type/tag using letters, digits, '_' or '-'.")


def read_cdh_key(resource: str) -> bytes:
    """Return exactly 32 raw bytes; never log or persist the response body."""
    validate_cdh_resource(resource)
    url = f"{CDH_ORIGIN}/cdh/resource/{resource}"
    # The v0.10.0 REST bridge allows 50 seconds for its CDH request.
    timeout = httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=5.0)
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
            with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
                if response.status_code != 200:
                    # This CDH version can report a KBS policy denial as HTTP 500.
                    raise ValueError(f"CDH key retrieval failed (HTTP {response.status_code}); no Secret fallback is permitted.")
                encoding = response.headers.get("Content-Encoding", "identity")
                if encoding.lower() != "identity":
                    raise ValueError("CDH key response must not use content encoding.")
                length = response.headers.get("Content-Length")
                if length is not None and length != str(KEY_BYTES):
                    raise ValueError("CDH key must contain exactly 32 raw bytes.")
                key = bytearray()
                # Raw streaming avoids decompression and never accumulates an unbounded body.
                for chunk in response.iter_raw(chunk_size=KEY_BYTES + 1):
                    if len(key) + len(chunk) > KEY_BYTES:
                        raise ValueError("CDH key must contain exactly 32 raw bytes.")
                    key.extend(chunk)
                if len(key) != KEY_BYTES:
                    raise ValueError("CDH key must contain exactly 32 raw bytes.")
                return bytes(key)
    except httpx.HTTPError:
        # Upstream exception text or response bodies may contain sensitive data.
        raise ValueError("CDH key request failed; no Secret fallback is permitted.") from None
