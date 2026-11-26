import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.operator_audit import (
    GENESIS_HASH,
    OperatorAuditLedger,
    OperatorAuditVerificationError,
)


KEY = "operator-audit-unit-key-0123456789abcdef"


def _append(ledger: OperatorAuditLedger, index: int, *, actor: str = "api_key"):
    return ledger.append_http_event(
        request_id=f"request-{index}",
        method="POST",
        path=f"/api/resource/{index}",
        status_code=200,
        duration_ms=12.5,
        actor=actor,
        client_address="192.0.2.10",
    )


def test_audit_records_form_verified_chain_without_sensitive_payloads(tmp_path):
    path = tmp_path / "operator_audit.jsonl"
    ledger = OperatorAuditLedger(path=path, key=KEY, required=True)

    first = _append(ledger, 1)
    second = _append(ledger, 2, actor="browser_session")
    verification = ledger.verify()

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert verification["verified"] is True
    assert verification["record_count"] == 2
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["previous_hash"] == GENESIS_HASH
    assert records[1]["previous_hash"] == records[0]["entry_hash"]
    assert records[0]["client_fingerprint"] == records[1]["client_fingerprint"]
    serialized = path.read_text(encoding="utf-8").lower()
    for forbidden in ("x-api-key", "cookie", "csrf", "request_body", "response_body"):
        assert forbidden not in serialized


def test_tampering_is_detected_and_blocks_future_appends(tmp_path):
    path = tmp_path / "operator_audit.jsonl"
    ledger = OperatorAuditLedger(path=path, key=KEY, required=True)
    _append(ledger, 1)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["status_code"] = 500
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    verification = ledger.verify()

    assert verification["verified"] is False
    assert "HMAC mismatch" in verification["error"]
    assert ledger.ensure_ready() is False
    with pytest.raises(OperatorAuditVerificationError, match="HMAC mismatch"):
        _append(ledger, 2)


def test_wrong_key_cannot_verify_existing_ledger(tmp_path):
    path = tmp_path / "operator_audit.jsonl"
    _append(OperatorAuditLedger(path=path, key=KEY, required=True), 1)

    verification = OperatorAuditLedger(
        path=path,
        key="different-operator-audit-key-abcdef012345",
        required=True,
    ).verify()

    assert verification["verified"] is False
    assert "No verification key" in verification["error"]


def test_concurrent_and_independent_writers_preserve_sequence(tmp_path):
    path = tmp_path / "operator_audit.jsonl"
    first = OperatorAuditLedger(path=path, key=KEY, required=True)
    second = OperatorAuditLedger(path=path, key=KEY, required=True)

    def writer(index: int):
        return _append(first if index % 2 else second, index)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(writer, range(1, 31)))

    assert sorted(item["sequence"] for item in results) == list(range(1, 31))
    verification = first.verify()
    assert verification["verified"] is True
    assert verification["record_count"] == 30


def test_optional_development_ledger_stays_disabled_without_key(tmp_path):
    ledger = OperatorAuditLedger(path=tmp_path / "audit.jsonl", key="", required=False)

    assert ledger.ensure_ready() is True
    assert _append(ledger, 1) == {
        "recorded": False,
        "reason": "audit_key_not_configured",
    }
    assert not (tmp_path / "audit.jsonl").exists()


def test_controlled_key_rotation_preserves_old_and_new_chain_records(tmp_path):
    path = tmp_path / "operator_audit.jsonl"
    old_key = "old-operator-audit-key-0123456789abcdef"
    new_key = "new-operator-audit-key-fedcba9876543210"
    old_ledger = OperatorAuditLedger(path=path, key=old_key, required=True)
    _append(old_ledger, 1)

    without_previous = OperatorAuditLedger(path=path, key=new_key, required=True)
    assert without_previous.verify()["verified"] is False

    rotated = OperatorAuditLedger(
        path=path,
        key=new_key,
        previous_keys=[old_key],
        required=True,
    )
    assert rotated.verify()["verified"] is True
    _append(rotated, 2)
    result = rotated.verify()

    assert result["verified"] is True
    assert result["record_count"] == 2
    assert result["verification_key_count"] == 2


def test_verified_snapshot_copy_is_byte_identical(tmp_path):
    source = tmp_path / "operator_audit.jsonl"
    destination = tmp_path / "snapshot" / "operator_audit.jsonl"
    ledger = OperatorAuditLedger(path=source, key=KEY, required=True)
    for index in range(1, 4):
        _append(ledger, index)

    result = ledger.copy_verified_snapshot(destination)

    assert result["verified"] is True
    assert result["record_count"] == 3
    assert destination.read_bytes() == source.read_bytes()
