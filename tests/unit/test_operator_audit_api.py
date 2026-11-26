import json

from api import main
from core.operator_audit import OperatorAuditLedger
from tools import operator_audit as operator_audit_cli


KEY = "operator-audit-api-key-0123456789abcdef"


def _ledger(monkeypatch, tmp_path):
    import core.operator_audit as operator_audit

    ledger = OperatorAuditLedger(
        path=tmp_path / "operator_audit.jsonl",
        key=KEY,
        required=True,
    )
    monkeypatch.setattr(operator_audit, "_LEDGER", ledger)
    main._rate_limit_store.clear()
    return ledger


def _records(ledger):
    return [
        json.loads(line)
        for line in ledger.path.read_text(encoding="utf-8").splitlines()
    ]


def test_api_mutation_is_audited_without_body_or_credentials(
    client, monkeypatch, tmp_path
):
    ledger = _ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_AUTH_ENABLED", False)

    response = client.post(
        "/api/not-a-real-route",
        json={"api_key": "must-never-appear", "trade_thesis": "private"},
        headers={"X-Request-ID": "audit-contract-request"},
    )

    assert response.status_code == 404
    verification = ledger.verify()
    assert verification["verified"] is True
    records = _records(ledger)
    assert len(records) == 1
    assert records[0]["request_id"] == "audit-contract-request"
    assert records[0]["actor"] == "development_unauthenticated"
    serialized = ledger.path.read_text(encoding="utf-8")
    assert "must-never-appear" not in serialized
    assert "private" not in serialized
    assert "x-api-key" not in serialized.lower()


def test_auth_rejection_and_success_are_attributed(client, monkeypatch, tmp_path):
    ledger = _ledger(monkeypatch, tmp_path)
    secret = "browser-auth-secret-that-is-long-enough"
    monkeypatch.setattr(main, "_AUTH_ENABLED", True)
    monkeypatch.setattr(main, "_API_KEY", secret)
    monkeypatch.setattr(main, "_IS_PRODUCTION", False)

    rejected = client.post("/api/not-a-real-route")
    accepted = client.post("/api/auth/login", json={"api_key": secret})

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    records = _records(ledger)
    assert [record["actor"] for record in records] == [
        "authentication_rejected",
        "api_key_login",
    ]
    assert [record["outcome"] for record in records] == ["denied", "success"]
    assert secret not in ledger.path.read_text(encoding="utf-8")


def test_tampered_audit_chain_refuses_new_mutation(client, monkeypatch, tmp_path):
    ledger = _ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_AUTH_ENABLED", False)
    first = client.post("/api/not-a-real-route")
    assert first.status_code == 404
    record = _records(ledger)[0]
    record["path"] = "/api/tampered"
    ledger.path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    refused = client.post("/api/not-a-real-route")

    assert refused.status_code == 503
    assert refused.json()["detail"] == "Operator audit unavailable; mutation refused."
    assert len(_records(ledger)) == 1


def test_rate_limited_mutation_is_still_audited(client, monkeypatch, tmp_path):
    ledger = _ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_AUTH_ENABLED", False)
    monkeypatch.setattr(main, "_RATE_LIMIT_MAX_WRITES", 1)

    first = client.post("/api/not-a-real-route")
    blocked = client.post("/api/not-a-real-route")

    assert first.status_code == 404
    assert blocked.status_code == 429
    records = _records(ledger)
    assert [record["status_code"] for record in records] == [404, 429]
    assert records[1]["outcome"] == "denied"


def test_operator_audit_status_and_cli_verification(
    client, monkeypatch, tmp_path, capsys
):
    ledger = _ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_AUTH_ENABLED", False)
    client.post("/api/not-a-real-route")

    status = client.get("/api/ops/audit/status")
    assert status.status_code == 200
    assert status.json()["ready"] is True
    assert status.json()["record_count"] == 1

    monkeypatch.setenv("MS_AUDIT_HMAC_KEY", KEY)
    exit_code = operator_audit_cli.main(
        ["verify", "--path", str(ledger.path)]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["verified"] is True
    assert output["record_count"] == 1
