from circa.models import AnalysisResult, CIFailure, HistoricalIncident


def test_models_parse_valid_data() -> None:
    failure = CIFailure.model_validate({"failure_text": "AssertionError: expected 2", "environment": {"os": "Linux"}})
    incident = HistoricalIncident.model_validate(
        {
            "id": "synthetic-test-001",
            "title": "Synthetic test incident",
            "category": "software",
            "summary": "A synthetic software test incident.",
            "keywords": ["assertion"],
            "environment": {"os": "linux"},
            "recommendation": "Inspect the assertion.",
            "synthetic": True,
        }
    )
    result = AnalysisResult.model_validate(
        {
            "fingerprint": "abc123",
            "category": "software",
            "root_cause": "Assertion regression",
            "recommendation": "Inspect the change.",
            "matched_incident_ids": [incident.id],
        }
    )

    assert failure.environment["os"] == "Linux"
    assert result.matched_incident_ids == ["synthetic-test-001"]
