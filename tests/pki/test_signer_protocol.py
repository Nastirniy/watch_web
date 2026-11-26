"""The signer protocol (RELEASE.md, PKI.md): every request names its version in X-Signer-Protocol; the signer refuses a
missing or unknown one with 409 protocol_unsupported, and the api answers 503 certificate_authority_unavailable
with one log line that names both versions. api and pki-signer run one release tag, so a mismatch means a container
was started by hand."""
import httpx
import pytest
from loguru import logger

from app.backend.enums.routes import SIGNER_PROTOCOL_HEADER, SignerProtocol, SignerRoutes
from app.backend.services.signer_client import SignerClient, SignerProtocolMismatch, SignerUnavailable
from app.backend.signer.app import create_app
from tests.pki.test_signer import settings_for, valid_body

V1 = str(SignerProtocol.V1.value)


@pytest.fixture
async def raw(tmp_path):
    """A client of the signer that sends no protocol header by itself"""
    app = create_app(settings_for(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://signer") as client:
        yield client


def test_the_protocol_is_version_1():
    assert SIGNER_PROTOCOL_HEADER == "X-Signer-Protocol"
    assert [member.value for member in SignerProtocol] == [1]


@pytest.mark.parametrize("headers", [{}, {SIGNER_PROTOCOL_HEADER: "2"}, {SIGNER_PROTOCOL_HEADER: "x"},
                                     {SIGNER_PROTOCOL_HEADER: ""}])
@pytest.mark.parametrize("method, path", [("GET", SignerRoutes.HEALTH.value), ("POST", SignerRoutes.SIGN_CLIENT.value)])
async def test_a_missing_or_unknown_protocol_is_refused(raw, headers, method, path):
    r = await raw.request(method, path, headers=headers, json=valid_body() if method == "POST" else None)
    assert r.status_code == 409
    assert r.json() == {"type": "about:blank", "title": "Protocol not supported", "status": 409,
                        "reason": "protocol_unsupported", "supported": [1]}


async def test_version_1_is_served(raw):
    r = await raw.get(SignerRoutes.HEALTH.value, headers={SIGNER_PROTOCOL_HEADER: V1})
    assert r.status_code == 200 and r.json()["protocols"] == [1]
    r = await raw.post(SignerRoutes.SIGN_CLIENT.value, headers={SIGNER_PROTOCOL_HEADER: V1}, json=valid_body())
    assert r.status_code == 200


async def test_an_extra_request_field_is_refused(raw):
    r = await raw.post(SignerRoutes.SIGN_CLIENT.value, headers={SIGNER_PROTOCOL_HEADER: V1},
                       json={**valid_body(), "issuer": "CN=Evil"})
    assert r.status_code == 422


# ---------------------------------------------------------------- the client

class Recording(httpx.AsyncBaseTransport):
    def __init__(self, answer: httpx.Response):
        self.answer = answer
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return self.answer


HEALTHY = {"subject": "CN=x", "not_after": "2030-01-01T00:00:00+00:00", "fingerprint_sha256": "ab",
           "protocols": [1], "something_new": True}


async def test_the_client_sends_the_header_on_every_call():
    transport = Recording(httpx.Response(200, json={**HEALTHY, "certificate_pem": "p", "serial": "a", "crl_pem": "c"}))
    client = SignerClient(transport)
    await client.health()
    await client.sign_client("pem", "label", 30)
    await client.sign_crl(1, __import__("datetime").datetime(2026, 1, 1), __import__("datetime").datetime(2026, 1, 2), [])
    assert [r.headers.get(SIGNER_PROTOCOL_HEADER) for r in transport.requests] == [V1, V1, V1]


async def test_the_client_accepts_an_answer_with_an_unknown_key():
    status = await SignerClient(Recording(httpx.Response(200, json=HEALTHY))).health()
    assert status.subject == "CN=x"


async def test_a_protocol_refusal_is_a_mismatch_with_one_log_line():
    answer = httpx.Response(409, json={"type": "about:blank", "title": "Protocol not supported", "status": 409,
                                       "reason": "protocol_unsupported", "supported": [2]})
    lines = []
    sink = logger.add(lambda message: lines.append(message.record["message"]), level="ERROR")
    try:
        with pytest.raises(SignerProtocolMismatch) as raised:
            await SignerClient(Recording(answer)).health()
    finally:
        logger.remove(sink)
    assert isinstance(raised.value, SignerUnavailable)
    assert lines == ["Signer protocol mismatch: the api speaks 1, the signer supports [2]. Fix: run one release tag "
                     "for api and pki-signer (deploy/release.sh status)."]


async def test_issue_answers_503_on_a_protocol_mismatch(client, db, headers, pki):
    """A signer of another release: issue answers 503 certificate_authority_unavailable, nothing is stored"""
    from app.backend.services.signer_client import use_signer_transport
    from tests.helpers import make_admin, make_user
    from tests.pki.test_certificate_routes import issue, problem_code, rows_of
    admin = await make_admin(db)
    user = await make_user(db)
    use_signer_transport(Recording(httpx.Response(409, json={
        "type": "about:blank", "title": "Protocol not supported", "status": 409, "reason": "protocol_unsupported",
        "supported": [2]})))
    r = await issue(client, headers, admin, user)
    assert r.status_code == 503 and problem_code(r) == "certificate_authority_unavailable"
    assert await rows_of(db, user.id) == []


# ---------------------------------------------------------------- the signer's check command

def test_signer_check_command(monkeypatch):
    from app.backend.signer import __main__ as signer_main, health
    seen = []

    def handler(request):
        seen.append(request.headers.get(SIGNER_PROTOCOL_HEADER))
        return httpx.Response(200, json=HEALTHY)

    monkeypatch.setattr(health, "_transport", lambda: httpx.MockTransport(handler))
    assert signer_main.main(["check"]) == 0
    assert seen == [V1], "the check speaks the protocol"
    monkeypatch.setattr(health, "_transport", lambda: httpx.MockTransport(lambda request: httpx.Response(409)))
    assert signer_main.main(["check"]) == 1
