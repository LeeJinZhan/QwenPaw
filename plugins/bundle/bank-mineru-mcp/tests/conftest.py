from __future__ import annotations

from pathlib import Path
import sys

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
BANK_RUNTIME_ROOT = PLUGIN_ROOT.parent / "bank-runtime"
for root in (PLUGIN_ROOT, BANK_RUNTIME_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
