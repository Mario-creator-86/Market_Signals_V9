from core.autonomous_control import (
    consume_autonomous_cycle_request,
    get_autonomous_cycle_status,
    record_autonomous_cycle_result,
    request_autonomous_cycle,
)


def test_autonomous_cycle_request_is_consumed_once_and_result_is_observable(tmp_path):
    request_path = tmp_path / "cycle-request.json"
    status_path = tmp_path / "cycle-status.json"

    queued = request_autonomous_cycle(request_path=request_path)
    consumed = consume_autonomous_cycle_request(request_path=request_path)

    assert consumed["request_id"] == queued["request_id"]
    assert consume_autonomous_cycle_request(request_path=request_path) == {}

    recorded = record_autonomous_cycle_result(
        {"status": "completed", "accepted": True},
        request=consumed,
        status_path=status_path,
    )
    observed = get_autonomous_cycle_status(status_path=status_path)

    assert recorded["request_id"] == queued["request_id"]
    assert recorded["trigger"] == "run_once"
    assert observed["result"] == {"status": "completed", "accepted": True}
