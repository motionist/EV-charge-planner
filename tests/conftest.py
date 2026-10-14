"""Make the integration package importable without Home Assistant installed.

The package ``__init__`` only imports Home Assistant lazily, and the planner
and adapters are pure Python, so ``ev_charge_planner.planner`` and
``ev_charge_planner.adapters`` can be imported directly.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))
