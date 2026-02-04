"""Signal conversion utilities for hardware communication.

This module provides data conversion functions used for translating
between high-level user representations and hardware formats.
"""

import numpy as np


def iq_float_to_cint16(
    samples_iq: list[list[float]] | np.ndarray,
    sample_bits: int,
) -> np.ndarray:
    """Convert user-space floating-point IQ samples into fixed-point complex int16 format.

    This helper performs the necessary quantization for the FPGA, ensuring signal
    integrity via:
    1. **Scaling**: Maps the normalized input range [-1.0, 1.0] to the full
    dynamic range defined by ``sample_bits``.
    2. **Symmetric Rounding**: Uses standard rounding to the nearest integer to
    minimize quantization noise.
    3. **Hard Clipping**: Enforces explicit saturation limits to prevent
    arithmetic overflow.

    :param samples_iq: Either List[List[float]] or np.ndarray with shape (N, 2)
        and dtype float32 (binary input).
    :type samples_iq: Union[List[List[float]], np.ndarray]
    :param sample_bits: The resolution (bit depth) of the target DAC or memory
        (e.g., 16).
    :type sample_bits: int
    :return: A numpy array of complex16 numbers ready for hardware upload.
    :rtype: np.ndarray
    """
    vmax = (1 << (sample_bits - 1)) - 1

    # Fast path for binary input (numpy float32 arrays)
    if isinstance(samples_iq, np.ndarray) and samples_iq.dtype == np.float32:
        # Binary input: skip intermediate float64 conversion
        i = np.clip(np.rint(samples_iq[:, 0] * vmax), -vmax - 1, vmax).astype(np.int16)
        q = np.clip(np.rint(samples_iq[:, 1] * vmax), -vmax - 1, vmax).astype(np.int16)
    else:
        # Legacy JSON input or other numpy dtypes: convert to float64 first
        a = np.asarray(samples_iq, dtype=np.float64)
        i = np.clip(np.rint(a[:, 0] * vmax), -vmax - 1, vmax).astype(np.int16)
        q = np.clip(np.rint(a[:, 1] * vmax), -vmax - 1, vmax).astype(np.int16)

    return i + 1j * q


__all__ = [
    "iq_float_to_cint16",
]
