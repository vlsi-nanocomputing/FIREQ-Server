from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class _MutableRef:
    """A mutable reference wrapper that holds data in an internal dictionary.

    Designed to be stored in the root node's reference table so that
    multiple consumers can observe updates without breaking when the
    producer recomputes a value.  The internal dictionary is never
    replaced — only mutated in place via :meth:`clear`, :meth:`set`,
    :meth:`update`, or ``__setitem__``.

    Usage::

        ref = _MutableRef(value=42)
        ref["value"] = 99        # __setitem__
        ref.hash()               # hash of internal dict
        bool(ref)                # True if internal dict is non-empty
    """

    def __init__(self, **kwargs: dict[str, Any]) -> None:
        """Initialize the mutable reference with optional key/value pairs.

        :param kwargs: Keys and values to populate the internal dictionary
        """
        self.log = logging.getLogger(__name__)
        self._data: dict[str, Any] = dict(kwargs)
        self.cached_hash = None

    def reset_hash(self) -> None:
        """Reset the object's hash to the init status, to force hash and compare to return true."""
        self.cached_hash = None

    def set_logger(self, new_logger: logging.Logger) -> None:
        """Set the logger for this object.

        :param new_logger: Logger object to use
        :type new_logger: logging.Logger
        """
        self.log = new_logger

    # ------------------------------------------------------------------
    # Dict-like access
    # ------------------------------------------------------------------

    def __getitem__(self, key: str) -> object:
        """Get a value by key from the internal dictionary."""
        return self._data[key]

    def __setitem__(self, key: str, value: object) -> None:
        """Set a value by key in the internal dictionary."""
        self._data[key] = value

    def __contains__(self, key: str) -> bool:
        """Check if a key exists in the internal dictionary."""
        return key in self._data

    def __bool__(self) -> bool:
        """Return ``True`` if the internal dictionary is non-empty."""
        return bool(self._data)

    def __repr__(self) -> str:
        """Return a readable representation."""
        return f"_MutableRef({self._data})"

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    def get(self, key: str, default: object = None) -> object:
        """Get a value, returning *default* if the key is missing.

        :param key: Key to look up
        :param default: Fallback value when *key* is absent
        :return: The stored value or *default*
        """
        return self._data.get(key, default)

    def set(self, key: str, value: object) -> None:
        """Set a value for *key* in the internal dictionary.

        :param key: Key to set
        :param value: Value to store
        """
        self._data[key] = value

    def update(self, **kwargs: dict[str, Any]) -> None:
        """Update the internal dictionary with new key/value pairs.

        :param kwargs: Keys and values to merge into the internal dictionary
        """
        self._data.update(kwargs)

    def clear(self) -> None:
        """Remove all entries from the internal dictionary."""
        self._data.clear()

    def hash(self) -> int:
        """Compute the hash of the internal dictionary for change detection.

        The object also stores a cached hash, which is the previous computed hash.

        :return: Hash of the frozenset of the internal dictionary's items
        """
        self.cached_hash = hash(frozenset(self._data.items()))
        return self.cached_hash

    def hash_and_compare(self) -> bool:
        """Recomputes the hash and returns `True` if it is different that the cached hash."""
        cache = self.cached_hash
        if cache == self.hash():
            return False
        return True


def _get_periods_from_clock(time: float, clock_frequency: float) -> int:
    """Compute the number of clock cycles from a time value and a clock frequency.

    :param time: Time value in nanoseconds
    :type time: float
    :param clock_frequency: Clock frequency in megahertz
    :type clock_frequency: float
    :return: Number of clock cycles
    :rtype: int
    """
    clock_cycles = int(time * clock_frequency / 1e3)
    return clock_cycles


def _require_attributes(obj: object, attributes: list[str]) -> None:
    """Check if an object has the specified attributes.

    :param obj: Object to check
    :type obj: object
    :param attributes: List of attribute names to verify
    :type attributes: list[str]
    :raises AttributeError: If the object does not have one or more of the specified attributes
    """
    for attribute in attributes:
        if not hasattr(obj, attribute):
            logger.error("object does not have attribute %s", attribute)
            raise AttributeError(f"object does not have attribute {attribute}")


def _get_dict_hash(dictionary: dict[str, Any]) -> int:
    """Calculate a hash of a dictionary.

    :param dictionary: Dictionary to hash
    :type dictionary: dict[str, Any]
    :return: Hash value of the dictionary
    :rtype: int
    """
    return hash(frozenset(dictionary.items()))
