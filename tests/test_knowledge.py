from pathlib import Path

from circa.knowledge import find_similar_incidents, load_incidents
from circa.models import CIFailure


def test_historical_search_prefers_matching_category_and_environment() -> None:
    data_path = Path(__file__).parents[1] / "data" / "incidents.json"
    incidents = load_incidents(data_path)
    failure = CIFailure(
        failure_text="Dependency installation hit a package registry connection timeout",
        category="network",
        environment={"network": "external"},
    )

    matches = find_similar_incidents(failure, incidents)

    assert matches[0].id == "synthetic-network-timeout-005"
    assert matches[0].synthetic is True
