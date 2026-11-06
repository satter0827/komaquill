"""Verify quality contracts within the declared test scope."""

from tools.checks import secrets as security


def test_security_excludes_only_canonical_public_package_integrity():
    line = "    resolution: {integrity: sha512-" + "A" * 86 + "==}"
    assert security.is_public_package_integrity("pnpm-lock.yaml", line)
    assert security.is_public_package_integrity(
        "pnpm-lock.yaml", 'integrity: "sha512-' + "A" * 86 + '=="'
    )
    assert not security.is_public_package_integrity("private.yaml", line)
    assert not security.is_public_package_integrity("pnpm-lock.yaml", line + " password: value")
    assert not security.is_public_package_integrity("pnpm-lock.yaml", "token: " + "A" * 86)
