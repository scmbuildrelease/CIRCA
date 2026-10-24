import pytest

from circa.fingerprint import dedup_fingerprint, failure_fingerprint, normalize_volatile_text
from circa.models import CIFailure


def test_equivalent_failures_have_the_same_fingerprint() -> None:
    first = CIFailure(
        failure_text="  ERROR at 0xA12F: worker 12345 timed out ",
        environment={"OS": "Linux", "Runner": "Container"},
    )
    second = CIFailure(
        failure_text="error at 0xbeef: worker 98765 timed out",
        environment={"runner": "container", "os": "linux"},
    )

    assert failure_fingerprint(first) == failure_fingerprint(second)


def test_primary_fingerprint_output_is_unchanged() -> None:
    failure = CIFailure(failure_text="Worker disk full: no space left for compiler output.")

    # Pinned before Phase 10: the dedup fingerprint must not alter existing outputs.
    assert failure_fingerprint(failure) == "4606823b25d1948506228a8e743d6147876779c061ba8f0ed6e13113fc2c985f"


def test_same_failure_at_different_timestamps_has_the_same_dedup_fingerprint() -> None:
    first = [
        "2026-10-22T10:14:16Z [ERROR] no space left on device on ci-linux-17",
        "10:14:16 ERROR: linker failed in /tmp/build-ab12/out.o after 12.5s (build #4521)",
    ]
    second = [
        "2026-10-23 08:02:51,123 [error] no space left on device on ci-linux-04",
        "08:02:51 ERROR: linker failed in /tmp/build-zz99/out.o after 3s (build #4600)",
    ]

    assert dedup_fingerprint(first) == dedup_fingerprint(second)
    assert dedup_fingerprint(first) == dedup_fingerprint(list(reversed(first)))


@pytest.mark.parametrize(
    ("volatile", "normalized"),
    [
        ("job 123e4567-e89b-12d3-a456-426614174000 failed", "job <uuid> failed"),
        ("checkout 3d3c42e5aac5 failed", "checkout <hash> failed"),
        ("segfault at 0x7ffd1234", "segfault at <address>"),
        ("pid 12345 killed", "<id> killed"),
    ],
)
def test_volatile_values_are_normalized(volatile: str, normalized: str) -> None:
    assert normalize_volatile_text(volatile) == normalized


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("process exited with exit code 1", "process exited with exit code 137"),
        ("artifact service returned HTTP 503", "artifact service returned HTTP 404"),
        ("no space left on device", "connection refused by registry"),
        ("ModuleNotFoundError: No module named 'foo2'", "ModuleNotFoundError: No module named 'foo3'"),
        ("python3.12 ImportError", "python3.13 ImportError"),
    ],
)
def test_materially_different_failures_have_different_dedup_fingerprints(first: str, second: str) -> None:
    assert dedup_fingerprint([first]) != dedup_fingerprint([second])
