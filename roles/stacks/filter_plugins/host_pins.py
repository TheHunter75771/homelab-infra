"""Host image pins for role stacks (docs/CONTRACTS.md §11.7.4; Python stdlib only).

The dashboard's root helper (homelab-dockerctl `set-pin`, §11.7.1) can move a media app to a newer image on
the host. It records that in /var/lib/homelab-dockerctl/pins.json:

    {"version": 1, "pins": {"media/<service>": {"image": <new pin>, "previous": <repo pin>, "at": <ISO>}}}

`previous` is the repo's pin from before the FIRST dashboard update of that service. A pin is
`<repository>:<tag>@sha256:<64 hex>`. Entries are matched to `images` keys by repository (never by tag).

Every function returns problems as data (an `errors` list) and never raises on bad input, so the calling
task can fail with a clear message instead of a template traceback.

stacks_host_pins_merge(content, images, apps, drop=False, b64=False) -> dict
    content  pins.json text (or ansible.builtin.slurp's base64 `content` with b64=True); None or "" with
             b64=False and no file means "no record" (pass None when the file is absent).
    images   the `images` dict from versions.yml.
    apps     {images key: label} for the keys the media compose file renders.
    drop     -e stacks_drop_host_pins=true: the repo wins for every matched entry.
    Returns:
      errors    non-empty -> fail before the stack is touched (malformed record, conflicts).
      warnings  print, keep going (repo behind, dropped pins, ignored entries).
      images    {key: pin to render} for every key in `apps`.
      remove    entry names to take out of pins.json once the stack is up (synced, dropped).
      delete    true when no entries remain after the removal (delete the file instead of writing it).
      content   the new pins.json text when `remove` is non-empty and `delete` is false, else "".
      synced, behind, dropped, ignored   entry names, for reporting.
    Rules for an entry matched to key K (repo pin R = images[K], host pin H, previous P):
      drop      -> render R, remove the entry (warning).
      R == H    -> synced: render R, remove the entry.
      R == P    -> repo behind: render H, keep the entry (warning: run playbooks/sync-pins.yml).
      otherwise -> conflict: error naming the app, both pins and the two ways out.
    An entry outside media/ or for a service that is not one of `apps` and whose repository matches no app
    is ignored with a warning and kept as is. A media/<app> entry whose repository is not the repository
    the repo pins for that app (or matches more than one app, or another app's repository) is an error:
    rendering the repo pin there would silently replace the host's image. With drop=True such an entry is
    dropped instead (repo pin rendered, entry removed, warning), as long as its service name names one of `apps`.

stacks_host_pins_sync_plan(content, images, apps, b64=False) -> {errors, warnings, changes}
    changes: [{entry, key, label, old, new, line}] for every matched entry whose host pin differs from the repo
    pin (repo behind AND conflict: running the sync playbook is how the owner adopts the host pin).

stacks_replace_once(text, changes) -> {errors, text}
    Replaces each change's exact `old` string by `new` in `text`. Each `old` must occur exactly once
    (before any replacement), else an error and the original text.
"""

import base64
import binascii
import json
import re

PINS_VERSION = 1
SYNC_COMMAND = "ansible-playbook playbooks/sync-pins.yml"
DROP_FLAG = "-e stacks_drop_host_pins=true"

_PIN_RE = re.compile(r"^(?P<repo>[A-Za-z0-9][A-Za-z0-9._/:-]*):(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})@sha256:[0-9a-f]{64}$")
_ENTRY_RE = re.compile(r"^(?P<stack>[a-z0-9][a-z0-9_.-]*)/(?P<service>[A-Za-z0-9][A-Za-z0-9_.-]*)$")
_PIN_MAX = 512
_MEDIA_STACK = "media"


def repository(pin):
    """The repository of an image reference: strip `@digest` first, then a `:tag` after the last `/`
    (a `:` before the last `/` is a registry port and stays). None for an empty or non-string value."""
    if not isinstance(pin, str) or not pin.strip():
        return None
    ref = pin.strip().split("@", 1)[0]
    colon = ref.rfind(":")
    if colon > ref.rfind("/"):
        ref = ref[:colon]
    return ref or None


def _valid_pin(value):
    return isinstance(value, str) and len(value) <= _PIN_MAX and _PIN_RE.match(value) is not None


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key %r" % key)
        out[key] = value
    return out


def _decode(content, b64):
    """-> (text or None, error or None)."""
    if content is None:
        return None, None
    if b64:
        try:
            raw = base64.b64decode(str(content), validate=True)
        except (binascii.Error, ValueError) as exc:
            return None, "pins.json could not be read (bad base64 from slurp: %s)" % exc
        try:
            return raw.decode("utf-8"), None
        except UnicodeDecodeError as exc:
            return None, "pins.json is not UTF-8 text (%s)" % exc
    if isinstance(content, bytes):
        try:
            return content.decode("utf-8"), None
        except UnicodeDecodeError as exc:
            return None, "pins.json is not UTF-8 text (%s)" % exc
    return str(content), None


def parse(content, b64=False):
    """-> (doc or None, errors). doc is None when there is no record (content None). Strict: an empty file,
    bad JSON, duplicate keys, a version other than 1, or a malformed entry are all errors."""
    text, err = _decode(content, b64)
    if err:
        return None, [err]
    if text is None:
        return None, []
    if not text.strip():
        return None, ["pins.json is empty; expected {\"version\": 1, \"pins\": {...}}"]
    try:
        doc = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except ValueError as exc:
        return None, ["pins.json is not valid JSON (%s)" % exc]
    if not isinstance(doc, dict):
        return None, ["pins.json must be a JSON object, not %s" % type(doc).__name__]
    version = doc.get("version")
    if isinstance(version, bool) or version != PINS_VERSION:
        return None, ["pins.json has version %s; this role understands only version %d" % (json.dumps(version), PINS_VERSION)]
    pins = doc.get("pins")
    if not isinstance(pins, dict):
        return None, ["pins.json has no \"pins\" object"]
    errors = []
    for name, entry in pins.items():
        if not _ENTRY_RE.match(name):
            errors.append("pins.json entry %r is not named <stack>/<service>" % name)
            continue
        if not isinstance(entry, dict):
            errors.append("pins.json entry %r is not an object" % name)
            continue
        for field in ("image", "previous"):
            if not _valid_pin(entry.get(field)):
                errors.append("pins.json entry %r: %r is not a pin (<repository>:<tag>@sha256:<64 hex>): %s"
                              % (name, field, json.dumps(entry.get(field))))
        if not isinstance(entry.get("at"), str) or not entry.get("at"):
            errors.append("pins.json entry %r has no \"at\" time" % name)
        if _valid_pin(entry.get("image")) and _valid_pin(entry.get("previous")) \
                and repository(entry["image"]) != repository(entry["previous"]):
            errors.append("pins.json entry %r: image and previous are different repositories (%s, %s)"
                          % (name, repository(entry["image"]), repository(entry["previous"])))
    if errors:
        return None, errors
    return doc, []


def _match(name, entry, images, apps):
    """-> (key or None, error or None, ignore-warning or None, drop key or None) for one valid entry.
    The drop key is set with an error when the entry can still be attributed to one app by its service name
    (media/<app>): with -e stacks_drop_host_pins=true that app's repo pin wins and the entry is removed."""
    stack, service = _ENTRY_RE.match(name).group("stack", "service")
    repo = repository(entry["image"])
    if stack != _MEDIA_STACK:
        return None, None, "pins.json entry %r is not a media-stack app; role stacks ignores it (left in the file)" % name, None
    attributable = service if service in apps else None
    way_out = (" Fix versions.yml, or let the repo win with %s (images.%s is deployed and the entry is removed)."
               % (DROP_FLAG, service)) if attributable else " Fix versions.yml or pins.json by hand."
    keys = [key for key in apps if repository(images.get(key)) == repo]
    if len(keys) > 1:
        return None, ("pins.json entry %r (%s) matches more than one media app in versions.yml (%s); "
                      "the media stack was not touched.%s" % (name, repo, ", ".join(sorted(keys)), way_out)), None, attributable
    if not keys:
        if attributable:
            return None, ("%s (%s): the host runs %s, a repository that versions.yml no longer pins for it "
                          "(images.%s is %s). The media stack was not touched.%s"
                          % (apps[service], name, entry["image"], service, images.get(service), way_out)), None, attributable
        return None, None, ("pins.json entry %r (%s) matches no media app in versions.yml; role stacks ignores it "
                            "(left in the file)" % (name, repo)), None
    if attributable and service != keys[0]:
        return None, ("pins.json entry %r runs %s, which versions.yml pins for images.%s, not images.%s; "
                      "the media stack was not touched.%s" % (name, repo, keys[0], service, way_out)), None, attributable
    return keys[0], None, None, None


def _apps(apps):
    return dict(apps or {})


def _images(images):
    return dict(images or {})


def stacks_host_pins_merge(content, images, apps, drop=False, b64=False):
    images, apps = _images(images), _apps(apps)
    result = {"errors": [], "warnings": [], "images": {}, "remove": [], "delete": False, "content": "",
              "synced": [], "behind": [], "dropped": [], "ignored": []}
    for key in apps:
        if repository(images.get(key)) is None:
            result["errors"].append("versions.yml has no usable images.%s" % key)
        else:
            result["images"][key] = images[key]
    doc, errors = parse(content, b64=b64)
    result["errors"].extend(errors)
    if doc is None or result["errors"]:
        return result

    pins = doc["pins"]
    for name in sorted(pins):
        entry = pins[name]
        key, error, ignore, drop_key = _match(name, entry, images, apps)
        if error and drop and drop_key:
            # The repo wins: the app's own repo pin is rendered (already in result["images"]) and the entry goes.
            result["dropped"].append(name)
            result["remove"].append(name)
            result["warnings"].append(
                "%s: dropping the host pin %s (stacks_drop_host_pins=true; it no longer matches what versions.yml pins "
                "for images.%s) and deploying the repo pin %s. If the app's data already migrated to the newer version, "
                "it may not start on the older one." % (apps[drop_key], entry["image"], drop_key, images[drop_key]))
            continue
        if error:
            result["errors"].append(error)
            continue
        if ignore:
            result["warnings"].append(ignore)
            result["ignored"].append(name)
            continue
        label, repo_pin, host_pin, previous = apps[key], images[key], entry["image"], entry["previous"]
        if drop and repo_pin != host_pin:
            result["dropped"].append(name)
            result["remove"].append(name)
            result["warnings"].append(
                "%s: dropping the host pin %s (stacks_drop_host_pins=true) and deploying the repo pin %s. If the app's "
                "data already migrated to the newer version, it may not start on the older one." % (label, host_pin, repo_pin))
        elif repo_pin == host_pin:
            result["synced"].append(name)
            result["remove"].append(name)
        elif repo_pin == previous:
            result["behind"].append(name)
            result["images"][key] = host_pin
            result["warnings"].append(
                "%s: the dashboard updated it on the host to %s; versions.yml still pins %s. Deploying the host pin "
                "(no rollback). Bring the repo up to date: %s, then review and commit versions.yml."
                % (label, host_pin, repo_pin, SYNC_COMMAND))
        else:
            result["errors"].append(
                "%s: versions.yml and the host disagree. Repo (images.%s): %s. Host (set from the dashboard at %s): %s, "
                "which replaced %s. The media stack was not touched. Two ways out: adopt the host pin with %s "
                "(then review and commit versions.yml and re-run), or let the repo win with %s (the app goes to the repo "
                "pin and the host entry is removed)." % (label, key, repo_pin, entry["at"], host_pin, previous,
                                                         SYNC_COMMAND, DROP_FLAG))

    if result["errors"]:
        result["images"] = {key: images[key] for key in result["images"]}
        result["remove"] = []
        return result
    if result["remove"]:
        remaining = {name: pins[name] for name in pins if name not in result["remove"]}
        if remaining:
            new_doc = dict(doc)
            new_doc["pins"] = remaining
            result["content"] = json.dumps(new_doc, indent=2, sort_keys=True) + "\n"
        else:
            result["delete"] = True
    return result


def stacks_host_pins_sync_plan(content, images, apps, b64=False):
    images, apps = _images(images), _apps(apps)
    result = {"errors": [], "warnings": [], "changes": []}
    doc, errors = parse(content, b64=b64)
    result["errors"].extend(errors)
    if doc is None or errors:
        return result
    pins = doc["pins"]
    for name in sorted(pins):
        key, error, ignore, _ = _match(name, pins[name], images, apps)
        if error:
            result["errors"].append(error)
        elif ignore:
            result["warnings"].append(ignore)
        elif images[key] != pins[name]["image"]:
            result["changes"].append({"entry": name, "key": key, "label": apps[key],
                                      "old": images[key], "new": pins[name]["image"],
                                      "line": "%s (images.%s): %s -> %s" % (apps[key], key, images[key],
                                                                           pins[name]["image"])})
    return result


def stacks_replace_once(text, changes):
    text = "" if text is None else str(text)
    errors = []
    for change in changes or []:
        count = text.count(change["old"])
        if count != 1:
            errors.append("images.%s: the old pin %s occurs %d times in versions.yml (expected exactly once); "
                          "nothing was changed" % (change["key"], change["old"], count))
    if errors:
        return {"errors": errors, "text": text}
    for change in changes or []:
        text = text.replace(change["old"], change["new"], 1)
    return {"errors": [], "text": text}


class FilterModule:
    def filters(self):
        return {
            "stacks_host_pins_merge": stacks_host_pins_merge,
            "stacks_host_pins_sync_plan": stacks_host_pins_sync_plan,
            "stacks_replace_once": stacks_replace_once,
        }
