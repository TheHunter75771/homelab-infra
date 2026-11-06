"""Stand-ins for the Open WebUI 0.12.4 internals the payday tool calls in process.

Re-implemented (not imported) from the live container's source, ghcr.io/open-webui/open-webui:v0.12.4,
keeping the behaviour the tool depends on (README, "Open WebUI 0.12.4 internals"):

- open_webui.models.users      Users.get_user_by_id (models/users.py 414-422): async, a UserModel or None.
- open_webui.models.knowledge  KnowledgeForm (165-168); Knowledges.get_knowledge_bases (234-264): every
                               collection, newest update first, with access_grants; insert_new_knowledge
                               (206-232): grants replaced by the form's list, None on failure;
                               add_file_to_knowledge_by_id (699-730): None on failure; has_file (732-741).
- open_webui.models.files      FileForm (105-111); Files.insert_new_file (121-151): None on failure;
                               get_file_by_id; update_file_data_by_id (merges into data).
- open_webui.routers.knowledge create_new_knowledge (318-367): non-admins need workspace.knowledge, the
                               grants are filtered, insert, embed (never raises), 400 when the insert fails.
- open_webui.routers.files     upload_file_handler (320-470) and process_uploaded_file (128-273):
                               extension allowlist, storage upload (empty -> error), size cap, file row with
                               status pending, then (process_in_background=False) indexing and, with
                               metadata.knowledge_id, the write-access gate, a second index into the
                               collection and the link. EVERY processing or linking error is caught and only
                               recorded as data.status = "failed" + data.error; the handler still returns
                               {"status": True, **file}.
- fastapi.UploadFile / HTTPException and starlette.datastructures.Headers, only when the real
  packages are missing (UploadFile(file, *, size, filename, headers), content_type from headers).

All state lives in one FakeOpenWebUI (reset per test). Failure switches let tests drive each path.
"""

import sys
import time
import types
import uuid

STATE = None


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self):
        return dict(self.__dict__)


class FakeOpenWebUI:
    def __init__(self):
        self.users = {}
        self.knowledge = []  # _Obj(id, user_id, name, description, meta, access_grants, created_at, updated_at)
        self.files = {}  # id -> _Obj(id, user_id, filename, path, data, meta, ...)
        self.links = []  # (knowledge_id, file_id, user_id)
        self.storage = {}  # path -> bytes
        self.calls = []
        self.allowed_extensions = []  # rag.file.allowed_extensions ([] = all)
        self.max_size_mb = None
        self.fail_index = None  # error text for index_file
        self.fail_link = False  # add_file_to_knowledge_by_id returns None
        self.fail_insert_file = False
        self.fail_insert_knowledge = False
        self.user_permissions = {"workspace.knowledge": False}
        self.on_upload = None  # callable run inside upload_file_handler (e.g. a slow step)
        self.on_list = None  # awaited inside get_knowledge_bases (a DB round trip that yields)
        self.deny_link_gate = False  # the auto-link gate refuses: logged, no link, status still "completed"

    def add_user(self, uid, role="admin", name="Noah", email="noah@example.invalid"):
        self.users[uid] = _Obj(id=uid, role=role, name=name, email=email)
        return self.users[uid]

    def add_knowledge(self, user_id, name, grants=None, meta=None, created_at=None):
        kb = _Obj(
            id=str(uuid.uuid4()),
            user_id=user_id,
            name=name,
            description="",
            meta=meta,
            access_grants=list(grants or []),
            created_at=created_at if created_at is not None else int(time.time()),
            updated_at=int(time.time()),
        )
        self.knowledge.append(kb)
        return kb

    def files_in(self, kb_id):
        return [self.files[f] for k, f, _ in self.links if k == kb_id]


# ---------------------------------------------------------------- fastapi / starlette (only if missing)


class HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class Headers:
    def __init__(self, headers=None):
        self._h = {str(k).lower(): v for k, v in (headers or {}).items()}

    def get(self, key, default=None):
        return self._h.get(str(key).lower(), default)


class UploadFile:
    def __init__(self, file, *, size=None, filename=None, headers=None):
        self.filename = filename
        self.file = file
        self.size = size
        self.headers = headers or Headers()

    @property
    def content_type(self):
        return self.headers.get("content-type", None)


def _http_exception():
    return sys.modules["fastapi"].HTTPException


# ---------------------------------------------------------------- models


class KnowledgeForm:
    def __init__(self, name, description, access_grants=None):
        self.name = name
        self.description = description
        self.access_grants = access_grants


class _Users:
    async def get_user_by_id(self, id, db=None):
        STATE.calls.append(("Users.get_user_by_id", id))
        return STATE.users.get(id)


class _Knowledges:
    async def get_knowledge_bases(self, skip=0, limit=30, db=None):
        STATE.calls.append(("Knowledges.get_knowledge_bases",))
        snapshot = list(STATE.knowledge)
        if STATE.on_list:
            await STATE.on_list()
        return sorted(snapshot, key=lambda k: -k.updated_at)
        return sorted(STATE.knowledge, key=lambda k: -k.updated_at)

    async def get_knowledge_by_id(self, id, db=None):
        return next((k for k in STATE.knowledge if k.id == id), None)

    async def insert_new_knowledge(self, user_id, form_data, db=None):
        STATE.calls.append(("Knowledges.insert_new_knowledge", user_id, form_data.name))
        if STATE.fail_insert_knowledge:
            return None
        kb = STATE.add_knowledge(user_id, form_data.name, grants=form_data.access_grants or [])
        kb.description = form_data.description
        return kb

    async def add_file_to_knowledge_by_id(self, knowledge_id, file_id, user_id, directory_id=None, db=None):
        STATE.calls.append(("Knowledges.add_file_to_knowledge_by_id", knowledge_id, file_id))
        if STATE.fail_link:
            return None
        STATE.links.append((knowledge_id, file_id, user_id))
        return _Obj(id=str(uuid.uuid4()), knowledge_id=knowledge_id, file_id=file_id, user_id=user_id)

    async def has_file(self, knowledge_id, file_id, db=None):
        return any(k == knowledge_id and f == file_id for k, f, _ in STATE.links)


class _Files:
    async def insert_new_file(self, user_id, form_data, db=None):
        STATE.calls.append(("Files.insert_new_file", user_id, form_data.filename))
        if STATE.fail_insert_file:
            return None
        rec = _Obj(
            id=form_data.id,
            user_id=user_id,
            filename=form_data.filename,
            path=form_data.path,
            data=dict(form_data.data),
            meta=dict(form_data.meta),
            created_at=int(time.time()),
            updated_at=int(time.time()),
        )
        STATE.files[rec.id] = rec
        return rec

    async def get_file_by_id(self, id, db=None):
        return STATE.files.get(id)

    async def update_file_data_by_id(self, id, data, db=None):
        rec = STATE.files.get(id)
        if rec:
            rec.data = {**(rec.data or {}), **data}
        return rec


class FileForm:
    def __init__(self, id, filename, path, data=None, meta=None, hash=None):
        self.id = id
        self.filename = filename
        self.path = path
        self.data = data or {}
        self.meta = meta or {}
        self.hash = hash


Users = _Users()
Knowledges = _Knowledges()
Files = _Files()


# ---------------------------------------------------------------- routers


async def create_new_knowledge(request, form_data, user=None):
    STATE.calls.append(("create_new_knowledge", user.id, form_data.name, form_data.access_grants))
    if user.role != "admin" and not STATE.user_permissions.get("workspace.knowledge"):
        raise _http_exception()(401, "unauthorized")
    knowledge = await Knowledges.insert_new_knowledge(user.id, form_data)
    if knowledge:
        return knowledge  # embed_knowledge_base_metadata never raises; publish_event is a no-op here
    raise _http_exception()(400, "file exists")


async def _index_file(file_id, collection_name=None):
    rec = STATE.files[file_id]
    STATE.calls.append(("index_file", file_id, collection_name))
    if STATE.fail_index:
        raise Exception(STATE.fail_index)
    content = STATE.storage[rec.path].decode("utf-8")
    rec.data = {**(rec.data or {}), "content": content}


async def process_uploaded_file(request, file, file_path, file_item, file_metadata, user, db=None):
    try:
        await Files.update_file_data_by_id(file_item.id, {"status": "processing"})
        await _index_file(file_item.id)
        knowledge_id = file_metadata.get("knowledge_id")
        if knowledge_id:
            knowledge = await Knowledges.get_knowledge_by_id(knowledge_id)
            can_write = not STATE.deny_link_gate and bool(knowledge) and (
                knowledge.user_id == user.id
                or user.role == "admin"
                or any(g.get("principal_id") == user.id and g.get("permission") == "write" for g in knowledge.access_grants)
            )
            if can_write:
                await _index_file(file_item.id, collection_name=knowledge_id)
                link = await Knowledges.add_file_to_knowledge_by_id(
                    knowledge_id=knowledge_id, file_id=file_item.id, user_id=user.id
                )
                if not link:
                    raise Exception(f"Failed to link file {file_item.id} to knowledge {knowledge_id}")
        await Files.update_file_data_by_id(file_item.id, {"status": "completed", "error": None})
    except Exception as e:
        await Files.update_file_data_by_id(
            file_item.id, {"status": "failed", "error": str(e.detail) if hasattr(e, "detail") else str(e)}
        )


async def upload_file_handler(
    request, file=None, metadata=None, process=True, process_in_background=True, user=None, background_tasks=None, db=None
):
    STATE.calls.append(("upload_file_handler", file.filename, metadata, process, process_in_background))
    HTTPExc = _http_exception()
    file_metadata = metadata if metadata else {}
    try:
        name = file.filename.rsplit("/", 1)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if process and STATE.allowed_extensions and ext not in STATE.allowed_extensions:
            raise HTTPExc(400, f"File type {ext} is not allowed")
        fid = str(uuid.uuid4())
        contents = file.file.read()
        if not contents:
            raise ValueError("empty content")
        path = f"/app/backend/data/uploads/{fid}_{name}"
        STATE.storage[path] = contents
        if STATE.max_size_mb and len(contents) > STATE.max_size_mb * 1024 * 1024:
            del STATE.storage[path]
            raise HTTPExc(413, "file too large")
        if STATE.on_upload:
            await STATE.on_upload()
        item = await Files.insert_new_file(
            user.id,
            FileForm(
                id=fid,
                filename=name,
                path=path,
                data={"status": "pending"} if process else {},
                meta={"name": name, "content_type": file.content_type, "size": len(contents), "data": file_metadata},
            ),
        )
        if process:
            if background_tasks and process_in_background:
                raise AssertionError("the tool must not process in the background")
            await process_uploaded_file(request, file, path, item, file_metadata, user, db=db)
            return {"status": True, **item.model_dump()}
        return item
    except HTTPExc:
        raise
    except Exception:
        raise HTTPExc(400, "Error uploading file") from None


# ---------------------------------------------------------------- install


def install():
    """Put the stand-ins into sys.modules (idempotent) and return a fresh state."""
    global STATE
    try:
        import fastapi  # noqa: F401
    except ImportError:
        fastapi = types.ModuleType("fastapi")
        fastapi.UploadFile = UploadFile
        fastapi.HTTPException = HTTPException
        sys.modules["fastapi"] = fastapi
        starlette = types.ModuleType("starlette")
        ds = types.ModuleType("starlette.datastructures")
        ds.Headers = Headers
        starlette.datastructures = ds
        sys.modules["starlette"] = starlette
        sys.modules["starlette.datastructures"] = ds
    mods = {
        "open_webui": {},
        "open_webui.models": {},
        "open_webui.routers": {},
        "open_webui.models.users": {"Users": Users},
        "open_webui.models.knowledge": {"Knowledges": Knowledges, "KnowledgeForm": KnowledgeForm},
        "open_webui.models.files": {"Files": Files, "FileForm": FileForm},
        "open_webui.routers.knowledge": {"create_new_knowledge": create_new_knowledge},
        "open_webui.routers.files": {"upload_file_handler": upload_file_handler},
    }
    for name, attrs in mods.items():
        mod = sys.modules.get(name) or types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
    STATE = FakeOpenWebUI()
    return STATE


def uninstall_open_webui():
    """Remove the open_webui modules (to test the "not Open WebUI" path)."""
    for name in [n for n in sys.modules if n == "open_webui" or n.startswith("open_webui.")]:
        del sys.modules[name]
