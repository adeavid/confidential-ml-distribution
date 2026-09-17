"""CDH client contract tests with HTTP fixtures; these do not prove attestation."""

from unittest.mock import Mock

import httpx
import pytest

import cdh


RESOURCE = "default/key/my-model"


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    def close(self):
        self.closed = True


def serve(monkeypatch, stream, *, status=200, headers=None):
    real_client = httpx.Client
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == f"http://127.0.0.1:8006/cdh/resource/{RESOURCE}"
        assert request.headers["Accept-Encoding"] == "identity"
        assert "Authorization" not in request.headers
        return httpx.Response(status, headers=headers, stream=stream)

    def client(**options):
        assert options["trust_env"] is False
        assert options["follow_redirects"] is False
        assert options["timeout"].connect == 5.0
        assert options["timeout"].read == 60.0
        return real_client(transport=httpx.MockTransport(respond), **options)

    monkeypatch.setattr(cdh.httpx, "Client", client)
    return requests


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "32"}])
def test_reads_exact_binary_key_without_transformation(monkeypatch, headers):
    key = b"\x00\xff" + b"k" * 29 + b"\n"
    stream = Chunks([key[:7], key[7:]])
    requests = serve(monkeypatch, stream, headers=headers)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:1234")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:1234")
    monkeypatch.setenv("HF_TOKEN", "test-token-must-not-be-forwarded")
    assert cdh.read_cdh_key(RESOURCE) == key
    assert len(requests) == 1 and stream.closed


@pytest.mark.parametrize("body,headers", [
    (b"", {}), (b"k" * 31, {}), (b"k" * 33, {}),
    (b"k" * 31, {"Content-Length": "32"}),
    (b"k" * 32, {"Content-Length": "33"}),
    (b"k" * 32, {"Content-Length": "invalid"}),
])
def test_rejects_wrong_or_truncated_key_lengths(monkeypatch, body, headers):
    stream = Chunks([body])
    serve(monkeypatch, stream, headers=headers)
    with pytest.raises(ValueError, match="exactly 32 raw bytes"):
        cdh.read_cdh_key(RESOURCE)
    assert stream.closed


def test_oversized_response_stops_before_reading_rest(monkeypatch):
    stream = Chunks([b"k" * 33, b"must not be requested"])
    serve(monkeypatch, stream)
    with pytest.raises(ValueError, match="exactly 32 raw bytes"):
        cdh.read_cdh_key(RESOURCE)
    assert stream.read_count == 1 and stream.closed


@pytest.mark.parametrize("status", [302, 403, 500])
def test_http_failure_does_not_read_body_or_follow_redirect(monkeypatch, status):
    marker = "SENSITIVE_REMOTE_RESPONSE"
    stream = Chunks([marker.encode()])
    requests = serve(monkeypatch, stream, status=status, headers={"Location": "https://external.invalid/key"})
    with pytest.raises(ValueError, match=f"HTTP {status}") as error:
        cdh.read_cdh_key(RESOURCE)
    assert marker not in str(error.value)
    assert len(requests) == 1 and stream.read_count == 0 and stream.closed


def test_encoded_response_is_rejected_before_decoding(monkeypatch):
    stream = Chunks([b"compressed data is not an AES key"])
    serve(monkeypatch, stream, headers={"Content-Encoding": "gzip"})
    with pytest.raises(ValueError, match="content encoding"):
        cdh.read_cdh_key(RESOURCE)
    assert stream.read_count == 0 and stream.closed


def test_transport_timeout_message_is_withheld(monkeypatch):
    real_client = httpx.Client
    marker = "SENSITIVE_TRANSPORT_MESSAGE"

    def fail(request):
        raise httpx.ReadTimeout(marker, request=request)

    monkeypatch.setattr(cdh.httpx, "Client", lambda **options: real_client(
        transport=httpx.MockTransport(fail), **options))
    with pytest.raises(ValueError, match="CDH key request failed") as error:
        cdh.read_cdh_key(RESOURCE)
    assert marker not in str(error.value)


@pytest.mark.parametrize("resource", [
    "", "default/key", "default/../key", "default/key/my-model/extra",
    "default/key/%2fother", "default/key/my-model?x=1", "http://example.invalid/key",
    "default/key/" + "x" * 65,
])
def test_invalid_resource_is_rejected_before_client_creation(monkeypatch, resource):
    client = Mock(side_effect=AssertionError("Invalid identifier reached network client"))
    monkeypatch.setattr(cdh.httpx, "Client", client)
    with pytest.raises(ValueError, match="repository/type/tag"):
        cdh.read_cdh_key(resource)
    client.assert_not_called()
