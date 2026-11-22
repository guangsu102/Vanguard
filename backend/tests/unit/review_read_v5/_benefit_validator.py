"""Load the checked-in read-only validator without a tmp/PYTHONPATH dependency."""
import importlib.util
from pathlib import Path
import sys

_path = Path(__file__).resolve().parents[4] / "scripts/ops/review_read_v5/validate_benefit.py"
_spec = importlib.util.spec_from_file_location("vanguard_review_read_benefit_validator", _path)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
COMPLEXITIES = _module.COMPLEXITIES
PHASE1_FLAGS = _module.PHASE1_FLAGS
PHASE2_FLAGS = _module.PHASE2_FLAGS
analyse = _module.analyse
