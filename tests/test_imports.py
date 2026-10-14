"""Import every Home Assistant module of the integration against stubs.

The planner tests do not need Home Assistant, so module-level mistakes in the
HA side (e.g. a name used before it is defined in a schema) would only show
up when Home Assistant loads the integration. These stubs stand in for
Home Assistant and voluptuous: base classes are real (empty) classes, every
other name is a MagicMock, so importing runs all module-level code.
"""

import importlib
import sys
import types
from unittest import mock

import pytest

STUBBED = (
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.binary_sensor",
    "homeassistant.components.number",
    "homeassistant.components.sensor",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.event",
    "homeassistant.helpers.selector",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.util",
    "homeassistant.util.dt",
    "homeassistant.util.location",
    "voluptuous",
)
BASE_CLASSES = {
    "homeassistant.config_entries": ("ConfigFlow", "OptionsFlow"),
    "homeassistant.helpers.update_coordinator": ("DataUpdateCoordinator", "CoordinatorEntity"),
    "homeassistant.components.sensor": ("SensorEntity",),
    "homeassistant.components.number": ("RestoreNumber",),
    "homeassistant.components.binary_sensor": ("BinarySensorEntity",),
}
INTEGRATION_MODULES = ("__init__", "binary_sensor", "config_flow", "coordinator", "number", "sensor")


class _Stub:
    def __init_subclass__(cls, **kwargs):
        pass

    def __class_getitem__(cls, item):
        return cls


class _Module(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = mock.MagicMock(name=f"{self.__name__}.{name}")
        setattr(self, name, value)
        return value


@pytest.fixture
def stub_home_assistant():
    saved = {name: sys.modules.get(name) for name in STUBBED}
    for name in STUBBED:
        module = _Module(name)
        module.__path__ = []  # a package, so submodules can be imported
        for cls in BASE_CLASSES.get(name, ()):
            setattr(module, cls, type(cls, (_Stub,), {}))
        sys.modules[name] = module
    loaded = [m for m in sys.modules if m == "ev_charge_planner" or m.startswith("ev_charge_planner.")]
    for m in loaded:
        sys.modules.pop(m)
    yield
    for m in [m for m in sys.modules if m == "ev_charge_planner" or m.startswith("ev_charge_planner.")]:
        sys.modules.pop(m)
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


@pytest.mark.parametrize("module", INTEGRATION_MODULES)
def test_integration_module_imports(stub_home_assistant, module):
    name = "ev_charge_planner" if module == "__init__" else f"ev_charge_planner.{module}"
    importlib.import_module(name)
