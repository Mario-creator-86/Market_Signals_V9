def test_calendar_range_for_historical_month_uses_in_process_api(client):
    response = client.get(
        "/api/calendar/range",
        params={"start": "2026-01-01", "end": "2026-01-31"},
    )

    assert response.status_code == 200
    payload = response.json()
    collection_keys = ("economic", "earnings", "ipo", "treasury_rates")
    assert set(collection_keys).issubset(payload)
    assert all(isinstance(payload[key], list) for key in collection_keys)
    for event in payload["earnings"]:
        assert isinstance(event, dict)
        assert event.get("symbol")
        assert str(event.get("date") or event.get("date_formatted") or "").startswith(
            "2026-01"
        )
