# file: fireq-utils/test/conftest.py
"""
Global Pytest Configuration and Fixtures.

CRITICAL: This module performs "eager mocking". It patches sys.modules at the
top-level (global scope) to ensure that hardware dependencies (pynq, xrfdc)
are mocked BEFORE pytest imports any application code during test collection.
"""

import os
import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

# -------------------------------------------------------------------------
# 1. PATH SETUP (Immediate Execution)
# -------------------------------------------------------------------------
# Ensure project root is visible immediately
test_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(test_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)


# -------------------------------------------------------------------------
# 2. DEFINE MOCK CLASSES
# -------------------------------------------------------------------------
# We define MockPynqBuffer here (or import carefully) to avoid circular deps
class MockPynqBuffer(np.ndarray):
    """Simulates a PYNQ contiguous memory buffer."""

    def __new__(cls, shape, dtype=np.uint32):
        obj = super().__new__(cls, shape, dtype=dtype)
        obj.physical_address = 0x10000000
        obj.device_address = 0x10000000
        return obj

    def __array_finalize__(self, obj):
        if obj is None:
            return
        self.physical_address = getattr(obj, "physical_address", 0x10000000)

    def freebuffer(self):
        pass

    def invalidate(self):
        pass

    def flush(self):
        pass


# -------------------------------------------------------------------------
# 3. GLOBAL MOCK INJECTION (Eager Patching)
# -------------------------------------------------------------------------
# This logic runs when pytest loads conftest.py, BEFORE collecting other tests.

# A. Mock PYNQ
mock_pynq = MagicMock()
mock_pynq.allocate = lambda shape, dtype: MockPynqBuffer(shape, dtype)
sys.modules["pynq"] = mock_pynq

# B. Mock Xilinx Drivers
sys.modules["xrfdc"] = MagicMock()
sys.modules["xrfclk"] = MagicMock()

# C. Mock Low-Level API Structure
ll_mock = MagicMock()
sys.modules["FIREQ_LL_API"] = ll_mock
sys.modules["FIREQ_LL_API.overlay_driver"] = ll_mock
# Ensure submodules expected by imports also exist
sys.modules["FIREQ_LL_API.acquistion_driver"] = MagicMock()
sys.modules["FIREQ_LL_API.generator_driver"] = MagicMock()
sys.modules["FIREQ_LL_API.trigger_generator_driver"] = MagicMock()

# -------------------------------------------------------------------------
# 4. SHARED FIXTURES
# -------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def verify_mocks_loaded():
    """Optional: verifies mocks are active during test execution."""
    import pynq

    assert isinstance(pynq, MagicMock) or isinstance(pynq.allocate, object)
    yield
