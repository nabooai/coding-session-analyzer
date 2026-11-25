"""Make `analyzer` importable when pytest runs from server/ (the package is not installed —
the server runs from the checkout, so tests do the same)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
