# Code Contribution

Before making your contribution:
- open an issue detailing the problem or enhancement (optional but highly recomended).
- create a new branch for your feature.
- make your modifications following the style guidelines.
- once you are done, create a merge request.

## Style Guidelines

### Python

Use the [PEP 8](https://www.python.org/dev/peps/pep-0008/) style guide for Python code.

#### Naming convention:
- package and module names: snake_case (ex: my_package)
- class names: CamelCase (ex: MyClass)
- function and variable names: snake_case (ex: my_function, my_variable)
- constants: UPPER_SNAKE_CASE (ex: MY_CONSTANT)
- private variables and methods: _snake_case (ex: _my_private_variable, _my_private_variable)
- private classes: _MyPrivateClass (ex: _MyPrivateClass)

#### Docstrings:
Use *reStructuredText* format for docstrings. <br />
For **Pycharm IDE**, chose "reStructured Text" docstrings format. <br />
For **VS Code IDE**, can be used [Pylance](https://marketplace.visualstudio.com/items?itemName=ms-python.vscode-pylance) extension. <br />
```
"""
Function description.

:param param1: Description of the first parameter
:type param1: ...
:param param2: ...
:type param2: ...
:return: Description of the return value
:rtype: ...
"""
```
