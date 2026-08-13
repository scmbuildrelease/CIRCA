from circa.fingerprint import failure_fingerprint
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
