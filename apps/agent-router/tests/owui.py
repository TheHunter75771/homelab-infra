"""Stand-ins for the Open WebUI v0.12.4 pieces the router uses, standard library only.

Re-implemented (not imported) from the real 0.12.4 source, keeping the signatures:
  open_webui/utils/plugin.py     extract_frontmatter, replace_imports, load_function_module_by_id
  open_webui/functions.py        generate_function_chat_completion: the pipe's parameters
                                 (get_function_params) and how its result is streamed
                                 (stream_content / process_line)
  open_webui/utils/chat.py       generate_chat_completion(request, form_data, user,
                                 bypass_filter=False, bypass_system_prompt=False)
  open_webui/models/access_grants.py
                                 AccessGrants.get_accessible_resource_ids(user_id, resource_type,
                                 resource_ids, permission='read', user_group_ids=None, db=None)
  open_webui/utils/middleware.py chat_completion_files_handler(request, body, extra_params, user)
                                 apply_source_context_to_messages(request, messages, sources,
                                 user_message, include_content=True)
  open_webui/utils/tools.py      get_tools(request, tool_ids, user, extra_params)
  open_webui/models/users.py     Users.get_user_by_id(id, db=None)
  open_webui/models/chats.py     Chats.get_chat_by_id(id, db=None) -> ChatModel (.user_id, .chat
                                 with the history blob) and
                                 Chats.get_message_by_id_and_message_id(id, message_id), which
                                 returns the message REBUILT FROM chat_message ROWS
                                 (models/chat_messages.py get_messages_map_by_chat_id): only the
                                 row columns survive, so blob-only keys such as selectedModelId
                                 are missing there, exactly as in 0.12.4
  open_webui/utils/response.py   the SSE lines convert_streaming_response_ollama_to_openai yields

Each fake records its calls in WORLD so tests can check what the router asked for.
pydantic is in the open-webui container but may be missing on the host; then a tiny shim is
injected into sys.modules here, in the tests only.
"""

import inspect
import json
import os
import re
import sys
import time
import types
import uuid

PIPE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "router_pipe.py"
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
    """Everything the fakes return, plus a log of what they were called with."""

    def __init__(self):
        self.users = {}
        self.grants = (
            set()
        )  # (user_id, model_id) read grants; ("*", model_id) is public
        self.chats = {}  # chat_id -> {"user_id": ..., "messages": {id: blob message}}
        self.chats_error = False
        self.agent_replies = {}  # agent id -> callable(form) -> response, or a list of responses
        self.sources = []
        self.tools = {}
        self.calls = []  # (name, args) in order

    def call(self, name, **kwargs):
        self.calls.append((name, kwargs))

    def calls_to(self, name):
        return [kwargs for n, kwargs in self.calls if n == name]

    def store_message(self, chat_id, message, owner="u1"):
        """Save a message the way Chats.upsert_message_to_chat_by_id_and_message_id does: merged
        into the chat's history blob (every key). The chat_message row view of it is derived on
        read (row_to_message), so only row columns come back from the row API."""
        chat = self.chats.setdefault(chat_id, {"user_id": owner, "messages": {}})
        old = chat["messages"].get(message["id"], {})
        chat["messages"][message["id"]] = {**old, **message}


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


class FakeStreamingResponse:
    """starlette StreamingResponse: the router only reads body_iterator."""

    def __init__(self, chunks):
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream"}
        self.background = None

        async def gen():
            for chunk in chunks:
                yield chunk

        self.body_iterator = gen()


def sse(model, content=None, reasoning=None, tool_calls=None, finish=None, usage=None):
    """One SSE line as convert_streaming_response_ollama_to_openai yields it."""
    delta = {}
    if content:
        delta["content"] = content
    if reasoning:
        delta["reasoning_content"] = reasoning
    if tool_calls:
        delta["tool_calls"] = tool_calls
    data = {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "created": int(time.time()),
        "model": model,
        "object": "chat.completion.chunk",
        "choices": [
            {"index": 0, "logprobs": None, "finish_reason": finish, "delta": delta}
        ],
    }
    if usage:
        data["usage"] = usage
    return f"data: {json.dumps(data)}\n\n"


def stream_reply(model, *texts, done=True):
    lines = [sse(model, content=t) for t in texts]
    if done:
        lines.append(
            sse(
                model, finish="stop", usage={"prompt_tokens": 3, "completion_tokens": 2}
            )
        )
        lines.append("data: [DONE]\n\n")
    return FakeStreamingResponse(lines)


# ---------------------------------------------------------------- fake open_webui modules


async def generate_chat_completion(
    request, form_data, user, bypass_filter=False, bypass_system_prompt=False
):
    WORLD.call(
        "generate_chat_completion",
        form_data=json.loads(json.dumps(form_data)),
        user=user,
        bypass_filter=bypass_filter,
        bypass_system_prompt=bypass_system_prompt,
    )
    model_id = form_data["model"]
    if model_id not in request.app.state.MODELS:
        raise Exception("Model not found")  # noqa: TRY002 - utils/chat.py raises exactly this
    reply = WORLD.agent_replies.get(model_id)
    if isinstance(reply, list):
        reply = reply.pop(0)
    if callable(reply):
        reply = reply(form_data)
    if reply is None:
        reply = stream_reply(model_id, f"answer from {model_id}")
    return reply


class _AccessGrants:
    async def get_accessible_resource_ids(
        self,
        user_id,
        resource_type,
        resource_ids,
        permission="read",
        user_group_ids=None,
        db=None,
    ):
        WORLD.call(
            "get_accessible_resource_ids",
            user_id=user_id,
            resource_type=resource_type,
            resource_ids=list(resource_ids),
            permission=permission,
        )
        return {
            rid
            for rid in resource_ids
            if (user_id, rid) in WORLD.grants or ("*", rid) in WORLD.grants
        }


async def chat_completion_files_handler(request, body, extra_params, user):
    WORLD.call(
        "chat_completion_files_handler", body=json.loads(json.dumps(body)), user=user
    )
    await extra_params["__event_emitter__"](
        {"type": "status", "data": {"action": "sources_retrieved", "done": True}}
    )
    return body, {"sources": list(WORLD.sources)}


async def apply_source_context_to_messages(
    request, messages, sources, user_message, include_content=True
):
    WORLD.call(
        "apply_source_context_to_messages", sources=sources, user_message=user_message
    )
    context = "".join(doc for s in sources for doc in s.get("document", []))
    return [{"role": "system", "content": f"<source>{context}</source>"}, *messages]


async def get_tools(request, tool_ids, user, extra_params):
    WORLD.call(
        "get_tools",
        tool_ids=list(tool_ids),
        user=user,
        extra_keys=sorted(extra_params),
        extra_user=dict(extra_params.get("__user__") or {}),
    )
    return dict(WORLD.tools)


class _Users:
    async def get_user_by_id(self, id, db=None):
        return WORLD.users.get(id)


# models/chat_messages.py (0.12.4): the chat_message columns, how a message dict fills them
# (_build_message) and how get_messages_map_by_chat_id turns a row back into a dict.
ROW_COLUMNS = (
    "id", "chat_id", "user_id", "role", "parent_id", "content", "output", "model_id", "files",
    "sources", "embeds", "meta", "done", "status_history", "error", "usage", "context_summary",
    "created_at", "updated_at",
)  # fmt: skip
DB_TO_JSON_KEY_MAP = {
    "parent_id": "parentId",
    "model_id": "model",
    "status_history": "statusHistory",
    "context_summary": "contextSummary",
    "created_at": "timestamp",
}
EXCLUDED_COLUMNS = frozenset({"id", "chat_id", "updated_at"})


def message_to_row(chat_id, data):
    return {
        "id": f"{chat_id}-{data['id']}",
        "chat_id": chat_id,
        "user_id": data.get("user_id"),
        "role": data.get("role", "user"),
        "parent_id": data.get("parent_id") or data.get("parentId"),
        "content": data.get("content"),
        "output": data.get("output"),
        "model_id": data.get("model_id") or data.get("model"),
        "files": data.get("files"),
        "sources": data.get("sources"),
        "embeds": data.get("embeds"),
        "meta": data.get("meta"),
        "done": data.get("done", True),
        "status_history": data.get("status_history") or data.get("statusHistory"),
        "error": data.get("error"),
        "usage": data.get("usage"),
        "context_summary": data.get("context_summary") or data.get("contextSummary"),
        "created_at": data.get("timestamp") or 1,
        "updated_at": 1,
    }


def row_to_message(message_id, row):
    msg = {"id": message_id}
    for key in ROW_COLUMNS:
        if key in EXCLUDED_COLUMNS or row.get(key) is None:
            continue
        msg[DB_TO_JSON_KEY_MAP.get(key, key)] = row[key]
    msg.setdefault("content", "")
    return msg


class FakeChatModel:
    """models/chats.py ChatModel: the fields the router reads."""

    def __init__(self, chat_id, user_id, messages):
        self.id = chat_id
        self.user_id = user_id
        self.title = "New Chat"
        self.chat = {"history": {"messages": json.loads(json.dumps(messages))}}


class _Chats:
    async def get_chat_by_id(self, id, db=None):
        WORLD.call("get_chat_by_id", chat_id=id)
        if WORLD.chats_error:
            return None  # the real method swallows any error and returns None
        chat = WORLD.chats.get(id)
        if chat is None:
            return None
        return FakeChatModel(id, chat["user_id"], chat["messages"])

    async def get_message_by_id_and_message_id(self, id, message_id):
        WORLD.call("get_message", chat_id=id, message_id=message_id)
        chat = WORLD.chats.get(id)
        if chat is None or message_id not in chat["messages"]:
            return None
        row = message_to_row(id, chat["messages"][message_id])
        return row_to_message(message_id, row)


def install_fake_open_webui():
    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod
        return mod

    module("open_webui")
    module("open_webui.utils")
    module("open_webui.models")
    module("open_webui.utils.chat", generate_chat_completion=generate_chat_completion)
    module(
        "open_webui.utils.middleware",
        chat_completion_files_handler=chat_completion_files_handler,
        apply_source_context_to_messages=apply_source_context_to_messages,
    )
    module("open_webui.utils.tools", get_tools=get_tools)
    module("open_webui.models.users", Users=_Users())
    module("open_webui.models.chats", Chats=_Chats())
    module("open_webui.models.access_grants", AccessGrants=_AccessGrants())


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


def read_source(path=PIPE_PATH):
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_pipe(path=PIPE_PATH):
    """(Pipe instance, frontmatter, module), like load_function_module_by_id."""
    content = read_source(path)
    frontmatter = extract_frontmatter(content)
    content = replace_imports(content)
    module_name = f"function_agent_router_{uuid.uuid4().hex}"
    module = types.ModuleType(module_name)
    sys.modules[module_name] = module
    try:
        module.__dict__["__file__"] = path
        exec(content, module.__dict__)  # noqa: S102 - how plugin.py loads a function
        if not hasattr(module, "Pipe"):
            raise Exception("No Function class found in the module")  # noqa: TRY002 - as plugin.py
        return module.Pipe(), frontmatter, module
    finally:
        sys.modules.pop(module_name, None)


# ---------------------------------------------------------------- the request, like functions.py


class _State:
    pass


class FakeRequest:
    def __init__(self, models):
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(MODELS=models))
        self.state = _State()


def chunk_message(model, content):
    """openai_chat_chunk_message_template(model, content) from open_webui/utils/misc.py."""
    template = {
        "id": f"{model}-{uuid.uuid4()}",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "logprobs": None, "finish_reason": None}],
        "object": "chat.completion.chunk",
    }
    template["choices"][0]["delta"] = {}
    if content:
        template["choices"][0]["delta"]["content"] = content
    if not content:
        template["choices"][0]["finish_reason"] = "stop"
    return template


def process_line(form_data, line):
    if isinstance(line, dict):
        line = f"data: {json.dumps(line)}"
    try:
        line = line.decode("utf-8")
    except Exception:  # noqa: BLE001, S110 - mirrors functions.py process_line
        pass
    if line.startswith("data:"):
        return f"{line}\n\n"
    return f"data: {json.dumps(chunk_message(form_data['model'], line))}\n\n"


async def call_pipe(
    pipe, form_data, user, request, metadata=None, emitter=None, task=None
):
    """generate_function_chat_completion: builds the parameters the same way and returns the
    SSE lines (stream) or the response dict (no stream)."""
    form_data = {**form_data}
    metadata = dict(metadata or {})
    extra_params = {
        "__event_emitter__": emitter,
        "__event_call__": None,
        "__chat_id__": metadata.get("chat_id", None),
        "__session_id__": metadata.get("session_id", None),
        "__message_id__": metadata.get("message_id", None),
        "__task__": task,
        "__task_body__": None,
        "__files__": metadata.get("files") or [],
        "__user__": user.model_dump(),
        "__metadata__": metadata,
        "__oauth_token__": None,
        "__request__": request,
    }
    extra_params["__tools__"] = metadata.get("tools", {})
    sig = inspect.signature(pipe.pipe)
    params = {"body": form_data} | {
        k: v for k, v in extra_params.items() if k in sig.parameters
    }

    if form_data.get("stream", False):
        lines = []
        res = await pipe.pipe(**params)
        if isinstance(res, FakeStreamingResponse):
            async for data in res.body_iterator:
                lines.append(data)
            return lines
        if isinstance(res, dict):
            lines.append(f"data: {json.dumps(res)}\n\n")
            return lines
        if isinstance(res, str):
            lines.append(
                f"data: {json.dumps(chunk_message(form_data['model'], res))}\n\n"
            )
        if inspect.isasyncgen(res):
            async for line in res:
                lines.append(process_line(form_data, line))
        finish = chunk_message(form_data["model"], "")
        lines.append(f"data: {json.dumps(finish)}\n\n")
        lines.append("data: [DONE]")
        return lines
    res = await pipe.pipe(**params)
    if isinstance(res, FakeStreamingResponse):
        return res
    return res


def parse_lines(lines):
    """(events, content, reasoning, selected_model_id) from SSE lines."""
    events = []
    for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    content = []
    reasoning = []
    selected = None
    for event in events:
        if "selected_model_id" in event and selected is None:
            selected = event["selected_model_id"]
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
    return events, "".join(content), "".join(reasoning), selected


# ---------------------------------------------------------------- model list entries


def agent_model(
    model_id,
    name=None,
    description="",
    tags=("agent",),
    knowledge=None,
    tool_ids=None,
    **extra,
):
    """A Workspace model as get_all_models puts it in app.state.MODELS (utils/models.py)."""
    meta = {"description": description, "tags": [{"name": t} for t in tags]}
    if knowledge is not None:
        meta["knowledge"] = knowledge
    if tool_ids is not None:
        meta["toolIds"] = tool_ids
    model = {
        "id": model_id,
        "name": name or model_id.replace("-", " ").title(),
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
            "meta": meta,
            "is_active": True,
        },
        "action_ids": [],
        "filter_ids": [],
    }
    model.update(extra)
    return model


def pipe_model(model_id="agent_router.auto", name="Auto"):
    """A function (pipe) model as get_function_models lists it (functions.py)."""
    return {
        "id": model_id,
        "name": name,
        "object": "model",
        "created": 1,
        "owned_by": "openai",
        "pipe": {"type": "pipe"},
        "has_user_valves": False,
    }
