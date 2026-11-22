"""Tests import forge and library from the sibling apps/skill-library checkout."""
import os
import sys

_LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skill-library")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
