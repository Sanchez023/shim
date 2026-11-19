from __future__ import annotations

import pytest

from openharness.runtime.assembly import CapabilityAssembler, CapabilityModule
from openharness.runtime.registry import CapabilityRegistry


def test_assembler_installs_in_dependency_order_and_unwinds():
    registry = CapabilityRegistry()
    host = CapabilityAssembler(registry)
    events: list[str] = []

    def setup_a(reg: CapabilityRegistry):
        events.append("setup_a")
        dispose = reg.register("a", object())
        return lambda: (events.append("dispose_a"), dispose())

    def setup_b(reg: CapabilityRegistry):
        events.append("setup_b")
        dispose = reg.register("b", object())
        return lambda: (events.append("dispose_b"), dispose())

    module_a = CapabilityModule(name="a", setup=setup_a)
    module_b = CapabilityModule(name="b", requires=("a",), setup=setup_b)

    host.install_all([module_b, module_a])
    assert host.installed == ["a", "b"]
    assert registry.resolve("a") is not None and registry.resolve("b") is not None

    host.uninstall_all()

    assert host.installed == []
    assert events == ["setup_a", "setup_b", "dispose_b", "dispose_a"]
    assert registry.snapshot() == {}


def test_assembler_rejects_missing_dependencies():
    host = CapabilityAssembler(CapabilityRegistry())
    module = CapabilityModule(name="b", requires=("missing",))

    with pytest.raises(ValueError, match="missing"):
        host.install(module)


def test_assembler_rejects_uninstall_with_dependents():
    registry = CapabilityRegistry()
    host = CapabilityAssembler(registry)
    host.install(CapabilityModule(name="a"))
    host.install(CapabilityModule(name="b", requires=("a",)))

    with pytest.raises(ValueError, match="required by"):
        host.uninstall("a")


def test_assembler_rejects_cycles():
    host = CapabilityAssembler(CapabilityRegistry())
    a = CapabilityModule(name="a", requires=("b",))
    b = CapabilityModule(name="b", requires=("a",))

    with pytest.raises(ValueError, match="cannot resolve"):
        host.install_all([a, b])


def test_runtime_public_api_has_no_plugin_alias():
    import openharness.runtime as rt

    assert not any("plugin" in name.lower() for name in rt.__all__)
