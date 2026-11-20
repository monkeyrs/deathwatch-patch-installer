# -*- coding: utf-8 -*-
"""Chinese runtime generation package (data from app/zh_runtime.json; code copied verbatim from the repo generation chain)."""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
