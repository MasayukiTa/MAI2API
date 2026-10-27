# Test bootstrap: point all runtime state at a throwaway directory
# BEFORE importing the application modules (they resolve their
# state directory at import time). No network, no browser, no real
# token is involved anywhere in this suite.
import os
import sys
import tempfile

os.environ["MAI2API_STATE_DIR"] = tempfile.mkdtemp(prefix="mai2api-test-")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
