import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_HERE)
for path in (_APP, os.path.join(os.path.dirname(_APP), "skill-library")):
    if path not in sys.path:
        sys.path.insert(0, path)
