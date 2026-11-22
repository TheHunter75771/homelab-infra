"""Load the tool the way Open WebUI v0.12.4 does, with the standard library only.

Mirrors (re-implemented, not imported) open_webui/utils/plugin.py:
  extract_frontmatter, replace_imports, load_tool_module_by_id (exec into a fresh ModuleType),
and open_webui/utils/tools.py:
  get_functions_from_tool, parse_description, parse_docstring, the parameter walk of
  convert_function_to_pydantic_model, and coerce_kwargs from
  get_async_tool_function_and_apply_extra_params.

pydantic is in the open-webui container but may be missing on the host. If it is missing, a tiny
shim is injected into sys.modules here, in the tests only. The shipped file imports the real one.
"""

import inspect
import os
import re
import sys
import types
import uuid
from typing import get_args, get_type_hints

TOOL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "moneyhub_readonly.py")

try:
    import pydantic  # noqa: F401

    HAVE_PYDANTIC = True
except ImportError:
    HAVE_PYDANTIC = False

    class _FieldInfo:
        def __init__(self, default, extra):
            self.default = default
            self.extra = extra

    def Field(default=None, **extra):
        return _FieldInfo(default, extra)

    class BaseModel:
        def __init__(self, **kwargs):
            for name in inspect.get_annotations(type(self)):
                default = getattr(type(self), name, None)
                if isinstance(default, _FieldInfo):
                    default = default.default
                setattr(self, name, kwargs.get(name, default))

    shim = types.ModuleType("pydantic")
    shim.BaseModel = BaseModel
    shim.Field = Field
    sys.modules["pydantic"] = shim


def extract_frontmatter(content):
    frontmatter = {}
    pattern = re.compile(r"^\s*([a-z_]+):\s*(.*)\s*$", re.IGNORECASE)
    lines = content.splitlines()
    if len(lines) < 1 or lines[0].strip() != '"""':
        return {}
    for line in lines[1:]:
        if '"""' in line:
            break
        match = pattern.match(line)
        if match:
            key, value = match.groups()
            frontmatter[key.strip()] = value.strip()
    return frontmatter


def replace_imports(content):
    for old, new in {
        "from utils": "from open_webui.utils",
        "from apps": "from open_webui.apps",
        "from main": "from open_webui.main",
        "from config": "from open_webui.config",
    }.items():
        content = content.replace(old, new)
    return content


def read_source(path=TOOL_PATH):
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_tool(path=TOOL_PATH):
    """Returns (Tools instance, frontmatter, module), like load_tool_module_by_id."""
    content = read_source(path)
    frontmatter = extract_frontmatter(content)
    content = replace_imports(content)
    module_name = f"tool_moneyhub_{uuid.uuid4().hex}"
    module = types.ModuleType(module_name)
    sys.modules[module_name] = module
    try:
        module.__dict__["__file__"] = path
        exec(content, module.__dict__)
        if not hasattr(module, "Tools"):
            raise Exception("No Tools class found in the module")
        return module.Tools(), frontmatter, module
    finally:
        sys.modules.pop(module_name, None)


def get_functions_from_tool(tool):
    return [
        getattr(tool, name)
        for name in dir(tool)
        if callable(getattr(tool, name)) and not name.startswith("_") and not inspect.isclass(getattr(tool, name))
    ]


def parse_description(docstring):
    if not docstring:
        return ""
    out = []
    for line in [line.strip() for line in docstring.strip().split("\n")]:
        if re.match(r":param", line) or re.match(r":return", line):
            break
        out.append(line)
    return "\n".join(out)


def parse_docstring(docstring):
    if not docstring:
        return {}
    pattern = re.compile(r":param (\w+):\s*(.*)")
    params = {}
    current = None
    for line in docstring.splitlines():
        line = line.strip()
        match = pattern.match(line)
        if match:
            name, desc = match.groups()
            current = None if name.startswith("__") else name
            if current:
                params[current] = desc
            continue
        if line.startswith(":"):
            current = None
            continue
        if current and line:
            params[current] = "\n".join(filter(None, [params[current], line]))
    return params


def tool_specs(tool):
    """A stdlib stand-in for get_tool_specs: name, description and typed parameters per function."""
    specs = []
    for func in get_functions_from_tool(tool):
        hints = get_type_hints(func)
        sig = inspect.signature(func)
        descs = parse_docstring(func.__doc__)
        params = {}
        for name, param in sig.parameters.items():
            params[name] = {
                "type": hints.get(name),
                "default": param.default,
                "description": descs.get(name),
            }
        specs.append(
            {
                "name": func.__name__,
                "description": parse_description(func.__doc__),
                "parameters": params,
                "is_async": inspect.iscoroutinefunction(func),
                "returns": hints.get("return"),
            }
        )
    return specs


try:
    from langchain_core.utils.function_calling import convert_to_openai_function

    HAVE_LANGCHAIN = HAVE_PYDANTIC
except ImportError:
    HAVE_LANGCHAIN = False


def _clean_properties(schema):
    if not isinstance(schema, dict):
        return
    if "anyOf" in schema:
        non_null = [t for t in schema["anyOf"] if t.get("type") != "null"]
        if len(non_null) == 1:
            schema.update(non_null[0])
            del schema["anyOf"]
        else:
            schema["anyOf"] = non_null
    if "default" in schema and schema["default"] is None:
        del schema["default"]
    if "type" not in schema and "anyOf" not in schema and "properties" not in schema:
        schema["type"] = "string"
    for prop in schema.get("properties", {}).values():
        _clean_properties(prop)
    if "items" in schema:
        _clean_properties(schema["items"])


def openai_tool_specs(tool):
    """open_webui get_tool_specs: convert_function_to_pydantic_model -> convert_to_openai_function -> clean."""
    import copy

    from pydantic import Field, create_model

    specs = []
    for func in get_functions_from_tool(tool):
        hints = get_type_hints(func)
        sig = inspect.signature(func)
        descs = parse_docstring(func.__doc__)
        fields = {}
        for name, param in sig.parameters.items():
            default = param.default if param.default is not param.empty else ...
            hint = hints.get(name)
            fields[name] = (hint, Field(default, description=descs[name])) if descs.get(name) else (hint, default)
        model = create_model(func.__name__, **fields)
        model.__doc__ = parse_description(func.__doc__)
        spec = copy.deepcopy(convert_to_openai_function(model))
        if "parameters" in spec:
            _clean_properties(spec["parameters"])
        specs.append(spec)
    return specs


def coerce_kwargs(func, kwargs):
    """open_webui's coerce_kwargs: str -> int for int params, number -> str for str params."""
    sig = inspect.signature(func)
    hints = get_type_hints(func)
    for name, value in list(kwargs.items()):
        if name not in sig.parameters or value is None:
            continue
        annotation = hints.get(name, sig.parameters[name].annotation)
        args = set(get_args(annotation))
        if isinstance(value, str) and (annotation is int or args == {int, type(None)}):
            kwargs[name] = int(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and (annotation is str or args == {str, type(None)}):
            kwargs[name] = str(value)
    return kwargs
