"""Stand-ins for the Open WebUI v0.12.4 pieces the filter touches, standard library only.

Re-implemented (not imported) from the real 0.12.4 source, keeping signatures and behaviour:
  open_webui/utils/plugin.py     extract_frontmatter, replace_imports, load_function_module_by_id
                                 (a module with a Filter class gives Filter())
  open_webui/utils/filter.py     get_filter_params, process_filter_function: the handler gets
                                 {'body': form_data} plus only those extra params its signature
                                 names (and __id__); a coroutine handler is awaited; whatever it
                                 returns REPLACES form_data; an exception propagates (for inlet
                                 that fails the chat). A module attribute `toggle` makes the
                                 filter opt-in per chat; `file_handler` makes it delete files.
  open_webui/utils/middleware.py process_chat_payload: the inlet's extra params (exactly these
                                 keys; no __task__), and outlet_filter_handler: the outlet body
                                 (messages from get_message_list with id/role/content/info/
                                 timestamp and output/usage/sources only when set), its extra
                                 params, and how a changed content/output is written back with
                                 originalContent and sent to the browser as chat:outlet
  open_webui/utils/misc.py       get_output_text, add_or_update_system_message,
                                 update_message_content
  open_webui/models/models.py    Models.get_model_by_id(id, db=None) -> ModelModel (.params, a
                                 pydantic model with extra='allow'; .model_dump()); None when
                                 missing or on any error
  open_webui/models/chats.py     Chats.get_chat_by_id(id, db=None) -> ChatModel (.user_id, .chat
                                 with the history blob); the write methods are recorded so a test
                                 can prove the filter never calls them
  src/lib/components/chat/Chat.svelte          the chat:outlet handler (merge when content differs)
  src/lib/components/chat/Messages/ContentRenderer.svelte
                                 what is shown: the structured output when it has items, else
                                 the content

Each fake records its calls in WORLD. pydantic is in the open-webui container but may be
missing on the host; then a tiny shim is injected into sys.modules here, in the tests only.
"""

import copy
import inspect
import json
import os
import re
import sys
import types
import uuid

# AUTO_COMPACT_FILTER_PATH lets a mutation run point the tests at a changed copy.
FILTER_PATH = os.environ.get("AUTO_COMPACT_FILTER_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "compact_filter.py"
)

try:
    import pydantic  # noqa: F401
except ImportError:

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

    _shim = types.ModuleType("pydantic")
    _shim.BaseModel = BaseModel
    _shim.Field = Field
    sys.modules["pydantic"] = _shim


class World:
    def __init__(self):
        self.model_rows = {}  # model id -> params dict (the DB row's params)
        self.models_error = False
        self.chats = {}  # chat id -> {"user_id": ..., "messages": {id: blob message}}
        self.calls = []  # (name, kwargs)
        self.writes = []  # any DB write the filter made (must stay empty)

    def call(self, name, **kwargs):
        self.calls.append((name, kwargs))

    def calls_to(self, name):
        return [kwargs for n, kwargs in self.calls if n == name]


WORLD = World()


def reset_world():
    global WORLD
    WORLD = World()
    return WORLD


class FakeUser:
    def __init__(
        self, id="u1", role="admin", name="Noah", email="noah@example.invalid"
    ):
        self.id = id
        self.role = role
        self.name = name
        self.email = email

    def model_dump(self):
        return {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "email": self.email,
        }


# ---------------------------------------------------------------- fake open_webui modules


class _Params:
    """models/models.py ModelParams (extra='allow')."""

    def __init__(self, data):
        self._data = dict(data)

    def model_dump(self):
        return copy.deepcopy(self._data)


class FakeModelModel:
    def __init__(self, model_id, params):
        self.id = model_id
        self.user_id = "u1"
        self.base_model_id = "assistant"
        self.params = _Params(params)


class _Models:
    async def get_model_by_id(self, id, db=None):
        WORLD.call("get_model_by_id", id=id)
        if WORLD.models_error:
            return None  # the real method swallows any error and returns None
        if id not in WORLD.model_rows:
            return None
        return FakeModelModel(id, WORLD.model_rows[id])


class FakeChatModel:
    def __init__(self, chat_id, user_id, messages):
        self.id = chat_id
        self.user_id = user_id
        self.title = "New Chat"
        self.chat = {"history": {"messages": copy.deepcopy(messages)}}


class _Chats:
    async def get_chat_by_id(self, id, db=None):
        WORLD.call("get_chat_by_id", chat_id=id)
        chat = WORLD.chats.get(id)
        if chat is None:
            return None
        return FakeChatModel(id, chat["user_id"], chat["messages"])

    async def upsert_message_to_chat_by_id_and_message_id(self, *args, **kwargs):
        WORLD.writes.append(("upsert_message", args, kwargs))

    async def update_chat_by_id(self, *args, **kwargs):
        WORLD.writes.append(("update_chat", args, kwargs))

    async def add_message_status_to_chat_by_id_and_message_id(self, *args, **kwargs):
        WORLD.writes.append(("add_status", args, kwargs))


def install_fake_open_webui():
    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod
        return mod

    module("open_webui")
    module("open_webui.models")
    module("open_webui.models.models", Models=_Models())
    module("open_webui.models.chats", Chats=_Chats())


install_fake_open_webui()


# ---------------------------------------------------------------- loading, like plugin.py


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


def read_source(path=FILTER_PATH):
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_filter(path=FILTER_PATH):
    """(Filter instance, type, frontmatter, module), like load_function_module_by_id."""
    content = read_source(path)
    frontmatter = extract_frontmatter(content)
    content = replace_imports(content)
    module_name = f"function_auto_compact_{uuid.uuid4().hex}"
    module = types.ModuleType(module_name)
    sys.modules[module_name] = module
    try:
        module.__dict__["__file__"] = path
        exec(content, module.__dict__)  # noqa: S102 - how plugin.py loads a function
        if hasattr(module, "Pipe"):
            return module.Pipe(), "pipe", frontmatter, module
        if hasattr(module, "Filter"):
            return module.Filter(), "filter", frontmatter, module
        raise Exception("No Function class found in the module")  # noqa: TRY002 - as plugin.py
    finally:
        sys.modules.pop(module_name, None)


# ---------------------------------------------------------------- calling, like filter.py


INLET_EXTRA_KEYS = (
    "__event_emitter__",
    "__event_call__",
    "__user__",
    "__metadata__",
    "__oauth_token__",
    "__request__",
    "__model__",
    "__chat_id__",
    "__message_id__",
)
OUTLET_EXTRA_KEYS = (
    "__event_emitter__",
    "__event_call__",
    "__user__",
    "__metadata__",
    "__request__",
    "__model__",
)


def get_filter_params(sig, filter_id, filter_type, form_data, extra_params):
    params = {"event": form_data} if filter_type == "stream" else {"body": form_data}
    return params | {
        k: v
        for k, v in {**extra_params, "__id__": filter_id}.items()
        if k in sig.parameters
    }


async def process_filter_function(
    function_module, filter_type, form_data, extra_params, filter_id="auto_compact"
):
    """filter.py process_filter_function for one filter: returns the new form_data."""
    handler = getattr(function_module, filter_type, None)
    if not callable(handler):
        return form_data
    sig = inspect.signature(handler)
    params = get_filter_params(sig, filter_id, filter_type, form_data, extra_params)
    if "__user__" in sig.parameters and hasattr(function_module, "UserValves"):
        params["__user__"]["valves"] = function_module.UserValves()
    if inspect.iscoroutinefunction(handler):
        return await handler(**params)
    return handler(**params)


def inlet_extra_params(user, metadata, model, emitter, request=None):
    """middleware.py process_chat_payload's extra_params, exactly these keys."""
    return {
        "__event_emitter__": emitter,
        "__event_call__": None,
        "__user__": user.model_dump(),
        "__metadata__": metadata,
        "__oauth_token__": None,
        "__request__": request,
        "__model__": model,
        "__chat_id__": metadata.get("chat_id"),
        "__message_id__": metadata.get("message_id"),
    }


def outlet_extra_params(user, metadata, model, emitter, request=None):
    return {
        "__event_emitter__": emitter,
        "__event_call__": None,
        "__user__": user.model_dump(),
        "__metadata__": metadata,
        "__request__": request,
        "__model__": model,
    }


# ---------------------------------------------------------------- misc.py helpers


def get_output_text(output):
    if not isinstance(output, list):
        return ""
    texts = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        parts = item.get("content") or []
        if not isinstance(parts, list):
            continue
        text = "".join(
            str(part.get("text"))
            for part in parts
            if isinstance(part, dict) and part.get("text") is not None
        )
        if text and not text.isspace():
            texts.append(text)
    return "\n".join(texts)


def update_message_content(message, content, append=True):
    if isinstance(message["content"], list):
        for item in message["content"]:
            if item["type"] == "text":
                if append:
                    item["text"] = f"{item['text']}\n{content}"
                else:
                    item["text"] = f"{content}\n{item['text']}"
    else:
        if append:
            message["content"] = f"{message['content']}\n{content}"
        else:
            message["content"] = f"{content}\n{message['content']}"
    return message


def add_or_update_system_message(content, messages, append=False):
    if messages and messages[0].get("role") == "system":
        messages[0] = update_message_content(messages[0], content, append)
    else:
        messages.insert(0, {"role": "system", "content": content})
    return messages


# ---------------------------------------------------------------- outlet, like middleware.py


def outlet_body(message_list, message_id, chat_id, session_id="s1", finish_reason=None):
    """outlet_filter_handler's outlet_data for a saved chat (message_list from the DB)."""
    return {
        "model": "m",
        "messages": [
            {
                "id": m.get("id"),
                "role": m.get("role"),
                "content": m.get("content") or get_output_text(m.get("output")),
                "info": m.get("info"),
                "timestamp": m.get("timestamp"),
                **({"output": copy.deepcopy(m["output"])} if m.get("output") else {}),
                **({"usage": m["usage"]} if m.get("usage") else {}),
                **({"sources": m["sources"]} if m.get("sources") else {}),
                **(
                    {"finish_reason": finish_reason}
                    if finish_reason and m.get("id") == message_id
                    else {}
                ),
            }
            for m in message_list
        ],
        "filter_ids": [],
        "chat_id": chat_id,
        "session_id": session_id,
        "id": message_id,
    }


def outlet_write_back(messages_map, outlet_result):
    """The saved-chat write-back after the outlet: returns the upserts it would make."""
    upserts = []
    for message in outlet_result.get("messages") or []:
        mid = message.get("id")
        if mid and mid in messages_map:
            original = messages_map[mid]
            original_content = original.get("content") or get_output_text(
                original.get("output")
            )
            message_content = message.get("content") or get_output_text(
                message.get("output")
            )
            content_changed = original_content != message_content
            output_changed = message.get("output") and message.get(
                "output"
            ) != original.get("output")
            if content_changed or output_changed:
                update = {
                    "originalContent": original_content,
                    **({"output": message["output"]} if output_changed else {}),
                }
                if content_changed:
                    update["content"] = message_content or ""
                upserts.append((mid, update))
                messages_map[mid] = {**original, **update}
    return upserts


def browser_outlet(history_messages, outlet_messages):
    """Chat.svelte's chat:outlet handler."""
    for msg in outlet_messages:
        if msg.get("id") and msg["id"] in history_messages:
            existing = history_messages[msg["id"]]
            if existing.get("content") != msg.get("content"):
                history_messages[msg["id"]] = {
                    **existing,
                    "originalContent": existing.get("content"),
                    **msg,
                }


def shown_text(message):
    """What ContentRenderer shows: the output's message items when output has items, else the
    content (reasoning items are a collapsed block, not reply text)."""
    output = message.get("output")
    if output:
        return get_output_text(output)
    return message.get("content") or ""


# ---------------------------------------------------------------- model list entries


def workspace_model(model_id, name=None):
    """A Workspace model as get_all_models lists it: info.params holds only the public
    params (public_model_params), so num_ctx is NOT here even when the model sets it."""
    return {
        "id": model_id,
        "name": name or model_id,
        "object": "model",
        "created": 1,
        "owned_by": "ollama",
        "connection_type": "local",
        "preset": True,
        "info": {
            "id": model_id,
            "user_id": "u1",
            "base_model_id": "assistant",
            "name": name or model_id,
            "params": {},
            "meta": {"description": "", "tags": []},
            "is_active": True,
        },
        "action_ids": [],
        "filter_ids": [],
    }


def pipe_model(model_id="agent_router.auto", name="Auto"):
    """A function (pipe) model as get_function_models lists it."""
    return {
        "id": model_id,
        "name": name,
        "object": "model",
        "created": 1,
        "owned_by": "openai",
        "pipe": {"type": "pipe"},
        "has_user_valves": False,
    }


def to_json(value):
    return json.loads(json.dumps(value))


# ---------------------------------------------------------------- Ollama's qwen3.5 renderer


def render_qwen35(messages, think=True):
    """Ollama v0.40.11 model/renderers/qwen35.go Qwen35Renderer.Render, default variant
    (qwen3.5), text only and no tools: what the broker's "assistant" model (qwen3.5:9b,
    template_source renderer_parser) actually sees. It accepts any role order: each user
    message is its own turn, a non-leading system message is rendered as a system turn, and
    consecutive tool results share one user turn."""
    im_start, im_end = "<|im_start|>", "<|im_end|>"
    out = []
    if messages and messages[0]["role"] == "system":
        out.append(
            im_start + "system\n" + messages[0]["content"].strip() + im_end + "\n"
        )
    last_query = len(messages) - 1
    multi_step = True
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if multi_step and m["role"] == "user":
            c = m["content"].strip()
            if not (c.startswith("<tool_response>") and c.endswith("</tool_response>")):
                multi_step = False
                last_query = i
    for i, m in enumerate(messages):
        content = (m.get("content") or "").strip()
        last = i == len(messages) - 1
        prefill = last and m["role"] == "assistant"
        if m["role"] == "user" or (m["role"] == "system" and i != 0):
            out.append(im_start + m["role"] + "\n" + content + im_end + "\n")
        elif m["role"] == "assistant":
            if think and i > last_query:
                out.append(im_start + "assistant\n<think>\n\n</think>\n\n" + content)
            else:
                out.append(im_start + "assistant\n" + content)
            if not prefill:
                out.append(im_end + "\n")
        elif m["role"] == "tool":
            if i == 0 or messages[i - 1]["role"] != "tool":
                out.append(im_start + "user")
            out.append("\n<tool_response>\n" + content + "\n</tool_response>")
            if last or messages[i + 1]["role"] != "tool":
                out.append(im_end + "\n")
        if last and not prefill:
            out.append(im_start + "assistant\n")
            if think:
                out.append("<think>\n")
    return "".join(out)
