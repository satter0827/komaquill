"""Check the real CLI process without a GPU or a wheel build."""

from tests.support.acceptance import run_acceptance


def test_local_cli_operates_through_standard_apis():
    run_acceptance()
