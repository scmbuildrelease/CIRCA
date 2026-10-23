from circa.knowledge import find_similar_incidents, load_incidents
from circa.models import CIFailure
from circa.resources import data_path


def test_historical_search_prefers_matching_category_and_environment() -> None:
    incidents = load_incidents(data_path("incidents.json"))
    failure = CIFailure(
        failure_text="Dependency installation hit a package registry connection timeout",
        category="network",
        environment={"network": "external"},
    )

    matches = find_similar_incidents(failure, incidents)

    assert matches[0].id == "synthetic-network-timeout-005"
    assert matches[0].synthetic is True


def test_generic_words_and_unrelated_numbers_do_not_count_as_overlap() -> None:
    incidents = load_incidents(data_path("incidents.json"))
    failure = CIFailure(failure_text="job failed with exit code 1 after 1200 seconds")

    assert find_similar_incidents(failure, incidents) == []


def test_literal_status_codes_still_match() -> None:
    incidents = load_incidents(data_path("incidents.json"))
    failure = CIFailure(failure_text="artifact upload returned 503")

    assert find_similar_incidents(failure, incidents)[0].id == "synthetic-infrastructure-service-008"
