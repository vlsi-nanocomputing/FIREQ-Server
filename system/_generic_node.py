from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from anytree import Node

logger = logging.getLogger(__name__)

_driver_wrappers = dict()


class RegisterNode(type):
    """Meta class that binds the node object (wrapper) to a specific target."""

    def __init__(cls, name, bases, attrs):
        if "wraps" in attrs:
            for driver_name in cls.wraps:
                _driver_wrappers[driver_name] = cls
        super().__init__(name, bases, attrs)


class _GenericNode(Node, metaclass=RegisterNode):
    """
    Class representing a generic sub-system as a node of a tree.

    The class can be created and modified through a dictionary configuration via the apply_configuration method.
    For more information about the configuration dictionary, see the documentation of the apply_configuration method.
    The class also provides a mechanism to register callbacks for parameters, which are used to modify parameters in an
    efficient way. Callbacks return an integer value that can be used to check if the operation was successful.
    The class also provides a mechanism to register dependencies between parameters, which are used to ensure that
    parameters are modified in the correct order.
    """

    _callback_registry = {}
    nodetype = "generic"

    def __init__(self, name: str, parent: _GenericNode = None, **kwargs: dict[str, Any]) -> None:
        super().__init__(name=name, parent=parent, **kwargs)

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        cls._callback_registry = {}

        for _, value in cls.__dict__.items():
            key = getattr(value, "_callback_key", None)

            if key is not None:
                cls._callback_registry[key] = (value, value._is_callback_sweepable, value._callback_cost)

    def parameter_callback(key, sweepable: bool = False, cost: int = 1) -> Callable:
        """Decorate the function to add it to the callback registry.

        The function is added to the callback registry with the key provided at the class level.
        """

        def deco(func: Callable) -> int:
            func._callback_key = key
            func._is_callback_sweepable = sweepable
            func._callback_cost = cost
            return func

        return deco

    def _get_callback(self, key: str) -> tuple:
        """Get the callback for a parameter."""
        if key in self._callback_registry.keys():
            return self._callback_registry[key]
        else:
            logger.error("key %s not found in callback registry", key)
            raise KeyError("key not found in callback registry")

    def create_child(self, name: str, of_type: str, **kwargs: dict[str, Any]) -> _GenericNode:
        """Create a child node of the specified type."""
        raise NotImplementedError("create_child not implemented for this node type")

    def get_child(self, name: str) -> _GenericNode:
        for child in self.children:
            if child.name == name:
                return child
        logger.error("child %s not found", name)
        raise KeyError("child not found")

    def apply_configuration(self, configuration: dict[str, object]) -> list[tuple]:
        """Apply a configuration dictionary to the tree, starting from this node.

        The configuration dictionary is a nested dictionary of parameters, with the following structure:
          - keys are the names of the parameters, sub-systems or the type of object to create:
            - if the key starts with a $, it is a parameter to apply to the current node
            - otherwise, it is the name of a sub-system
            - if the value is a list of dictionaries, each dict creates an object of the type defined by the key
          - values are either:
            - a value, which is the value of the parameter or a sweepable parameter (if it is a string starting with #)
            - a dictionary, which is the configuration of a pre-existing sub-system
            - a list of dictionaries, which are the configurations of multiple sub-systems of the same type
              - each dictionary must contain metadata key-value pairs:
                - _name: the name of the object to create
                - other metadata depending on the object type
              - each dictionary can also contain the configuration of the object, which is applied after creation

        Returns a list of paramters to sweep, where each element is a tuple of:
            - the callback
            - the expression to evaluate for the sweepable parameter
            - the cost of the sweepable parameter

        Raises errors if the configuration or paramter values are not valid.
        """
        callback_list = []
        callback_error = 0
        for key, value in configuration.items():
            # if the key starts with a $, treat it as a parameter to apply
            if key.startswith("$"):
                callback = self._get_callback(key)
                # bind the callback to self
                bound_method = callback[0].__get__(self, self.__class__)
                if value.startswith("#"):
                    # this is a sweepable parameter
                    if not callback[1]:
                        # this parameter cannot be swept, therefore raise an error
                        logger.error("parameter %s cannot be swept", key)
                        raise ValueError("parameter cannot be swept")
                    callback_list.append((bound_method, value, callback[2]))
                else:
                    # this is a single parameter, apply the configuration
                    callback_error += bound_method(value)
            else:
                # if the value is a dict, then take the child and apply the dict
                if isinstance(value, dict):
                    child = self.get_child(key)
                    callback_list |= child.apply_configuration(value)
                # if it is a list of dicts, then create the children and apply the configuration
                elif isinstance(value, list):
                    for dictitem in value:
                        if not isinstance(dictitem, dict):
                            logger.error("item in list is not a dictionary")
                            raise ValueError("item in list must be a dictionary")
                        if "_name" not in dictitem.keys():
                            logger.error("item in list does not have a name key")
                            raise KeyError("item in list must have a name key")
                        if dictitem["_name"].startswith("_"):
                            logger.error("item name cannot start with an underscore")
                            raise ValueError("item name cannot start with an underscore")
                        child = self.create_child(
                            name=dictitem["_name"],
                            of_type=key,
                            **{k: v for k, v in dictitem.items() if k.startswith("_")},
                        )
                        item_copy = {k: v for k, v in dictitem.items() if not k.startswith("_")}
                        callback_list |= child.apply_configuration(item_copy)
                else:
                    logger.error("unsupported value type for key %s", key)
                    raise TypeError("unsupported value type in configuration dictionary")
        # throw error if any callback has failed
        if callback_error != 0:
            logger.error("error applying configuration to system node")
            raise RuntimeError("error applying configuration to system node")

        return callback_list
