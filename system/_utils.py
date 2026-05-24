from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, Dict, List, Tuple, Union

from anytree import Node

logger = logging.getLogger(__name__)


def _get_periods_from_clock(time: float, clock_frequency: float):
    """Compute the number of clock cycles from a time value and a clock frequency.

    Time is in nanoseconds and clock frequency is in megahertz.
    """
    clock_cycles = int(time * clock_frequency / 1e3)
    return clock_cycles


def _require_attributes(obj: object, attributes: list[str]) -> None:
    """Check if an object has the specified attributes.

    :param obj: Object to check
    :type obj: object
    :param attributes: List of attributes to check
    :type attributes: list[str]
    :raises AttributeError: If the object does not have one or more of the specified attributes
    """
    for attribute in attributes:
        if not hasattr(obj, attribute):
            logger.error("object does not have attribute %s", attribute)
            raise AttributeError(f"object does not have attribute {attribute}")
