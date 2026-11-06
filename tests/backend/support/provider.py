"""Build provider evidence for tests without contacting Google."""

from komaquill.domain.models import RuntimeObservation


def stopped():
    return RuntimeObservation(state="stopped", endpoint_id="expected", account_sha256="a" * 64)
