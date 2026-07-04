from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from anytree import Node

logger = logging.getLogger(__name__)

_driver_wrappers: dict[str, type] = dict()


class RegisterNode(type):
    """Metaclass that binds a node object (wrapper) to a specific target driver.

    When a class defines a ``wraps`` attribute listing driver names, this metaclass
    automatically registers the class in the ``_driver_wrappers`` dictionary so that
    the system node can later instantiate the correct wrapper for each discovered IP.
    """

    def __init__(cls, name: str, bases: tuple, attrs: dict[str, Any]) -> None:
        """Register the class if it declares a ``wraps`` attribute.

        :param name: Name of the class being created
        :type name: str
        :param bases: Base classes
        :type bases: tuple
        :param attrs: Class attribute dictionary
        :type attrs: dict[str, Any]
        """
        if "wraps" in attrs:
            for driver_name in cls.wraps:
                _driver_wrappers[driver_name] = cls
        super().__init__(name, bases, attrs)


class _GenericNode(Node, metaclass=RegisterNode):
    """Generic sub-system node in the FIREQ system tree.

    The class can be created and modified through a dictionary configuration via the
    :meth:`apply_configuration` method.  It provides a callback registry to modify
    hardware parameters efficiently and a dependency mechanism to order parameter
    updates correctly.

    Callbacks are decorated with :meth:`parameter_callback` and return an integer
    status code (0 on success).
    """

    _callback_registry: dict[str, tuple] = {}
    nodetype: str = "generic"

    def __init__(
        self,
        name: str,
        parent: _GenericNode | None = None,
        **kwargs: dict[str, Any],
    ) -> None:
        """Initialize a generic node.

        :param name: Unique name for this node
        :type name: str
        :param parent: Parent node in the system tree, or None for the root
        :type parent: _GenericNode or None
        :param kwargs: Additional keyword arguments passed to anytree Node
        :type kwargs: dict[str, Any]
        """
        super().__init__(name=name, parent=parent, **kwargs)
        self.log = logging.getLogger(__name__)

    def set_logger(self, new_logger: logging.Logger) -> None:
        """Set the logger for this object.

        :param new_logger: Logger object to use
        :type new_logger: logging.Logger
        """
        self.log = new_logger

    def __init_subclass__(cls) -> None:
        """Build the callback registry from decorated methods on the subclass."""
        super().__init_subclass__()
        cls._callback_registry = {}

        for _, value in cls.__dict__.items():
            key = getattr(value, "_callback_key", None)
            if key is not None:
                cls._callback_registry[key] = (
                    value,
                    value._is_callback_sweepable,
                    value._callback_cost,
                )

    @staticmethod
    def parameter_callback(
        key: str,
        sweepable: bool = False,
        cost: int = 1,
    ) -> Callable:
        """Decorate the method to add it to the callback registry.

        :param key: Parameter key (must start with ``$``, e.g. ``"$duration"``)
        :type key: str
        :param sweepable: Whether the parameter may be swept at run time
        :type sweepable: bool
        :param cost: Relative cost of applying this parameter
        :type cost: int
        :return: Decorator that tags the function and returns it unchanged
        :rtype: Callable
        """

        def deco(func: Callable) -> Callable:
            func._callback_key = key
            func._is_callback_sweepable = sweepable
            func._callback_cost = cost
            return func

        return deco

    def _get_callback(self, key: str) -> tuple:
        """Retrieve the callback tuple for a parameter.

        :param key: Parameter key (e.g. ``"$duration"``)
        :type key: str
        :return: Tuple of ``(callback, is_sweepable, cost)``
        :rtype: tuple
        :raises KeyError: If the key is not found in the callback registry
        """
        if key in self._callback_registry:
            return self._callback_registry[key]
        self.log.error("key %s not found in callback registry", key)
        raise KeyError(f"key {key} not found in callback registry")

    def create_child(self, name: str, of_type: str, **kwargs: dict[str, Any]) -> _GenericNode:
        """Create a child node of the specified type.

        Subclasses must override this method to support their specific child types.

        :param name: Name for the new child node
        :type name: str
        :param of_type: Type identifier of the child node to create
        :type of_type: str
        :param kwargs: Additional metadata arguments (prefixed with ``_``)
        :type kwargs: dict[str, Any]
        :return: The newly created child node
        :rtype: _GenericNode
        :raises NotImplementedError: If the subclass does not override this method
        """
        raise NotImplementedError("create_child not implemented for this node type")

    def get_child(self, name: str) -> _GenericNode:
        """Get a direct child node by name.

        :param name: Name of the child node to retrieve
        :type name: str
        :return: The matching child node
        :rtype: _GenericNode
        :raises KeyError: If no child with the given name exists
        """
        for child in self.children:
            if child.name == name:
                return child
        self.log.error("child %s not found", name)
        raise KeyError(f"child {name} not found")

    def apply_configuration(self, configuration: dict[str, object]) -> list[tuple]:
        """Apply a configuration dictionary to the tree, starting from this node.

        The configuration dictionary has the following structure:

        * Keys that start with ``$`` are **parameters** applied to the current node.
          Their values are either a plain value or a sweepable expression (string
          starting with ``#``).
        * Other keys name **sub-systems** (existing children) or **types** of objects
          to create.
        * A ``dict`` value is the configuration of a pre-existing child.
        * A ``list`` of ``dict`` values creates multiple sub-systems of the same
          type. Each dictionary **must** contain a ``_name`` key and may contain
          other metadata keys prefixed with ``_``.

        :param configuration: Nested configuration dictionary
        :type configuration: dict[str, object]
        :return: List of sweepable parameter tuples ``(callback, expression, cost)``
        :rtype: list[tuple]
        :raises ValueError: If a parameter value or configuration entry is invalid
        :raises KeyError: If a required metadata key is missing
        :raises TypeError: If an unsupported value type is encountered
        :raises RuntimeError: If any callback returns a non-zero error code
        """
        callback_list: list[tuple] = []
        callback_error: int = 0
        for key, value in configuration.items():
            # check if the key is a string, only strings are allowed
            if not isinstance(key, str):
                self.log.error("key %s is not a string", key)
                raise TypeError(f"key {key} is not a string")
            # if the key starts with $, treat it as a parameter to apply
            elif key.startswith("$"):
                callback = self._get_callback(key)
                # bind the callback to self
                bound_method = callback[0].__get__(self, self.__class__)
                if isinstance(value, str) and value.startswith("#"):
                    # this is a sweepable parameter
                    if not callback[1]:
                        self.log.error("parameter %s cannot be swept", key)
                        raise ValueError(f"parameter {key} cannot be swept")
                    callback_list.append((bound_method, value, callback[2]))
                else:
                    # this is a single parameter, apply the configuration
                    callback_error = bound_method(value)
                    # throw error if acallback has failed
                    if callback_error != 0:
                        self.log.error("callback %s failed with error code %s", key, callback_error)
                        raise RuntimeError(f"callback {key} failed with error code {callback_error}")
            # if the value is a dict, then take the child and apply the dict
            elif isinstance(value, dict):
                child = self.get_child(key)
                callback_list.extend(child.apply_configuration(value))
            # if it is a list of dicts, then create the children and apply the configuration
            elif isinstance(value, list):
                for dictitem in value:
                    if not isinstance(dictitem, dict):
                        self.log.error("item in list is not a dictionary")
                        raise ValueError("item in list must be a dictionary")
                    if "_name" not in dictitem:
                        self.log.error("item in list does not have a name key")
                        raise KeyError("item in list must have a _name key")
                    if dictitem["_name"].startswith("_"):
                        self.log.error("item name cannot start with an underscore")
                        raise ValueError("item name cannot start with an underscore")
                    child = self.create_child(
                        name=dictitem["_name"],
                        of_type=key,
                        **{k: v for k, v in dictitem.items() if k.startswith("_") and k != "_name"},
                    )
                    item_copy = {k: v for k, v in dictitem.items() if not k.startswith("_")}
                    callback_list.extend(child.apply_configuration(item_copy))
            else:
                self.log.error("unsupported value type for key %s", key)
                raise TypeError(f"unsupported value type for key {key}")
        return callback_list

    def get_capability_dict() -> dict | None:
        """Get an example configuration for experiment definition purposes.

        :return: Dictionary describing the capability of the peripheral or None if not applicable
        :rtype: dict | None
        """
        return None
