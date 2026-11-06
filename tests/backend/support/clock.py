"""Override the owning module's clock without changing library timeout clocks."""

from types import SimpleNamespace


def patch_clock(monkeypatch, module, **changes):
    monkeypatch.setattr(module, "time", SimpleNamespace(**(vars(module.time) | changes)))
