"""Offline auditor/repairer for deepseek-ai/chat-demo history archives (export_format_version 3).

The upstream app validates an archive only inside the browser (TS + IndexedDB).
This module re-implements the parts of that contract that are decidable from the
archive bytes alone, so an archive can be checked before it is imported.

Rules are taken from (commit 5d5dd754ebc1948e977ddb880a9eb5aa25f37b4c):
  src/services/chat/history/README.md      (documented structure / order / limits)
  src/services/chat/history/paths.ts#L24   (image path = NFC(session_files/file_id_name))
  src/services/chat/history/zipReader.ts#L66 (entry names are NFC-normalised)
  src/services/chat/history/toLocal.ts#L301-L310 (image must live in its session dir,
                                                one entry cannot map to two file ids)

Pure stdlib, no network.
"""

from __future__ import annotations

import base64
import json
import os
import re
import unicodedata
import zipfile
from typing import Any, Dict, List, Optional, Tuple

VERSION = 3
MANIFEST_PATH = "manifest.json"
INDEX_PATH = "chat_session_index.json"
USER_PATH = "user.json"
SESSIONS_DIR = "chat_sessions/"
SESSION_RE = re.compile(r"^chat_sessions/([^/]+)\.json$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
ROLES = ("USER", "ASSISTANT")

# Error codes that a layout rewrite can fix without touching user content.
REPAIRABLE = ("E_MANIFEST_NOT_FIRST", "E_NAME_NOT_NFC")

ERROR_TEXT = {
    "E_ARCHIVE_OPEN": "not a readable ZIP archive",
    "E_MANIFEST_MISSING": "manifest.json is missing",
    "E_MANIFEST_NOT_FIRST": "manifest.json is not the first entry",
    "E_MANIFEST_JSON": "manifest.json is not valid JSON",
    "E_MANIFEST_VERSION": "export_format_version is not 3",
    "E_MANIFEST_EXTRA": "manifest.json has fields beyond export_format_version",
    "E_NAME_NOT_NFC": "an entry name or export_path is not NFC",
    "E_NAME_UNSAFE": "an entry name is absolute, empty-segmented, or uses a backslash",
    "E_NAME_DUP": "two entries share one name",
    "E_NAME_UNKNOWN": "an entry name matches no known archive role",
    "E_CRC": "an entry fails its CRC check",
    "E_INDEX_MISSING": "chat_session_index.json is missing",
    "E_INDEX_JSON": "chat_session_index.json is not valid JSON",
    "E_INDEX_MISMATCH": "session files and index entries are not one-to-one",
    "E_SESSION_JSON": "a session file is not valid JSON",
    "E_SESSION_SHAPE": "a session file is missing required fields",
    "E_SESSION_ID": "a session file name and its id disagree",
    "E_TIME": "a timestamp is not UTC RFC 3339 with a Z suffix",
    "E_TREE_PARENT_ORDER": "a child message id is not greater than its parent id",
    "E_TREE_ORPHAN": "a message points at a parent that does not exist",
    "E_TREE_ROOT": "a message tree has a root that is not a USER message",
    "E_TREE_ROLE": "a message role is not USER/ASSISTANT",
    "E_CURRENT_MISSING": "current_message_id is not a message of the session",
    "E_IMAGE_MISSING": "a FILE.export_path references an entry that is not in the archive",
    "E_IMAGE_OUTSIDE": "a FILE.export_path is outside its own session image directory",
    "E_IMAGE_SHARED": "one image entry is referenced by two different file ids",
    "W_IMAGE_ORPHAN": "an image entry is referenced by no FILE.export_path",
}

USAGE = "usage: chat-demo-history-repair <archive.zip> [--json] [--fix <out.zip>]"


class UsageError(Exception):
    pass


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _err(report: Dict[str, Any], code: str, detail: str) -> None:
    report["errors"].append({"code": code, "detail": detail})


def _warn(report: Dict[str, Any], code: str, detail: str) -> None:
    report["warnings"].append({"code": code, "detail": detail})


def _is_iso(value: Any) -> bool:
    return isinstance(value, str) and bool(ISO_RE.match(value))


def _nfc_text(obj: Any) -> bool:
    if isinstance(obj, str):
        return nfc(obj) == obj
    if isinstance(obj, list):
        return all(_nfc_text(v) for v in obj)
    if isinstance(obj, dict):
        return all(nfc(k) == k and _nfc_text(v) for k, v in obj.items())
    return True


def _normalise_export_paths(obj: Any) -> Any:
    """Rewrite every FILE.export_path to NFC, mirroring toLocal.ts#L301-L302."""
    if isinstance(obj, list):
        return [_normalise_export_paths(v) for v in obj]
    if isinstance(obj, dict):
        out = {k: _normalise_export_paths(v) for k, v in obj.items()}
        if isinstance(out.get("export_path"), str):
            out["export_path"] = nfc(out["export_path"])
        return out
    return obj


def _classify(name: str) -> Optional[str]:
    if name in (MANIFEST_PATH, INDEX_PATH, USER_PATH):
        return name
    if SESSION_RE.match(name):
        return "session"
    if name.startswith(SESSIONS_DIR) and "_files/" in name:
        return "image"
    return None


def _check_names(raw_names: List[str], report: Dict[str, Any]) -> None:
    seen = {}
    for name in raw_names:
        if name in seen:
            _err(report, "E_NAME_DUP", "duplicate entry: %s" % name)
        seen[name] = True
        if nfc(name) != name:
            _err(report, "E_NAME_NOT_NFC", "entry name is not NFC: %r" % name)
        if name.startswith("/") or "\\" in name or "\0" in name or re.match(r"^[A-Za-z]:", name):
            _err(report, "E_NAME_UNSAFE", "unsafe entry name: %r" % name)
            continue
        parts = name[:-1].split("/") if name.endswith("/") else name.split("/")
        if any(part in ("", ".", "..") for part in parts):
            _err(report, "E_NAME_UNSAFE", "unsafe path segments: %r" % name)
        if _classify(name) is None:
            _err(report, "E_NAME_UNKNOWN", "unknown entry name: %r" % name)


def _check_tree(session: Dict[str, Any], label: str, report: Dict[str, Any]) -> Tuple[int, int]:
    messages = session.get("messages")
    if not isinstance(messages, list):
        _err(report, "E_SESSION_SHAPE", "%s: messages is not a list" % label)
        return 0, 0
    by_id: Dict[int, Dict[str, Any]] = {}
    roots = 0
    for msg in messages:
        if not isinstance(msg, dict) or not isinstance(msg.get("id"), int):
            _err(report, "E_SESSION_SHAPE", "%s: a message has no integer id" % label)
            continue
        mid = msg["id"]
        if mid in by_id:
            _err(report, "E_TREE_ORPHAN", "%s: duplicate message id %d" % (label, mid))
        by_id[mid] = msg
        role = msg.get("role")
        if role not in ROLES:
            _err(report, "E_TREE_ROLE", "%s: message %d role is %r" % (label, mid, role))
        parent = msg.get("parent_id")
        if parent is None:
            roots += 1
            if role != "USER":
                _err(report, "E_TREE_ROOT", "%s: root message %d is %r, not USER" % (label, mid, role))
        elif not isinstance(parent, int):
            _err(report, "E_SESSION_SHAPE", "%s: message %d parent_id is not an int/null" % (label, mid))
        else:
            if parent >= mid:
                _err(report, "E_TREE_PARENT_ORDER",
                     "%s: message %d has parent %d (parent must be smaller)" % (label, mid, parent))
            if parent not in by_id:
                _err(report, "E_TREE_ORPHAN", "%s: message %d parent %d missing" % (label, mid, parent))
            else:
                parent_role = by_id[parent].get("role")
                if role in ROLES and parent_role in ROLES and role == parent_role:
                    _err(report, "E_TREE_ROLE",
                         "%s: message %d repeats parent role %s" % (label, mid, role))
        if not _is_iso(msg.get("inserted_at")):
            _err(report, "E_TIME", "%s: message %d inserted_at is not a Z timestamp" % (label, mid))
    if roots == 0:
        _err(report, "E_TREE_ROOT", "%s: no root message" % label)
    elif roots > 1:
        _err(report, "E_TREE_ROOT", "%s: %d root messages" % (label, roots))
    current = session.get("current_message_id")
    if current not in by_id:
        _err(report, "E_CURRENT_MISSING",
             "%s: current_message_id %r is not present" % (label, current))
    return len(messages), len(by_id)


def audit(path: str) -> Dict[str, Any]:
    """Audit one archive. Never raises for archive content; raises UsageError for I/O."""
    report: Dict[str, Any] = {
        "archive": os.path.basename(path),
        "ok": True,
        "format_version": None,
        "entries": 0,
        "sessions": 0,
        "messages": 0,
        "images": 0,
        "errors": [],
        "warnings": [],
        "repairs": [],
    }
    if not os.path.isfile(path):
        raise UsageError("no such file: %s" % path)
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError as exc:
        raise UsageError("cannot read %s: %s" % (path, exc))
    if blob[:2] != b"PK":
        _err(report, "E_ARCHIVE_OPEN", "file does not start with PK")
        report["ok"] = False
        return report

    import io

    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        _err(report, "E_ARCHIVE_OPEN", str(exc))
        report["ok"] = False
        return report

    infos = zf.infolist()
    names = [i.filename for i in infos]
    report["entries"] = len(infos)
    _check_names(names, report)

    contents: Dict[str, bytes] = {}
    for info in infos:
        try:
            contents[info.filename] = zf.read(info)
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            _err(report, "E_CRC", "%s: %s" % (info.filename, exc))
    bad = zf.testzip()
    if bad is not None and not any(e["code"] == "E_CRC" for e in report["errors"]):
        _err(report, "E_CRC", "CRC mismatch in %s" % bad)
    zf.close()

    # manifest must be the first entry
    if MANIFEST_PATH not in names:
        _err(report, "E_MANIFEST_MISSING", "no manifest.json entry")
    else:
        if names[0] != MANIFEST_PATH:
            _err(report, "E_MANIFEST_NOT_FIRST",
                 "first entry is %r" % names[0])
        raw = contents.get(MANIFEST_PATH)
        try:
            manifest = json.loads(raw.decode("utf-8")) if raw is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _err(report, "E_MANIFEST_JSON", str(exc))
            manifest = None
        if isinstance(manifest, dict):
            version = manifest.get("export_format_version")
            report["format_version"] = version
            if version != VERSION:
                _err(report, "E_MANIFEST_VERSION", "got %r" % (version,))
            extra = sorted(set(manifest) - {"export_format_version"})
            if extra:
                _err(report, "E_MANIFEST_EXTRA", "extra keys: %s" % ", ".join(extra))
        elif manifest is not None:
            _err(report, "E_MANIFEST_JSON", "manifest is not an object")

    # index
    index: Optional[List[Dict[str, Any]]] = None
    if INDEX_PATH not in names:
        _err(report, "E_INDEX_MISSING", "no chat_session_index.json entry")
    else:
        raw = contents.get(INDEX_PATH)
        try:
            index = json.loads(raw.decode("utf-8")) if raw is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _err(report, "E_INDEX_JSON", str(exc))
        if index is not None and not isinstance(index, list):
            _err(report, "E_INDEX_JSON", "index is not a JSON array")
            index = None

    # sessions
    session_ids: List[str] = []
    sessions: Dict[str, Dict[str, Any]] = {}
    for name in names:
        m = SESSION_RE.match(name)
        if not m:
            continue
        stem = m.group(1)
        session_ids.append(stem)
        raw = contents.get(name)
        try:
            session = json.loads(raw.decode("utf-8")) if raw is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _err(report, "E_SESSION_JSON", "%s: %s" % (name, exc))
            continue
        if not isinstance(session, dict):
            _err(report, "E_SESSION_JSON", "%s: not an object" % name)
            continue
        sessions[stem] = session
        if session.get("id") != stem:
            _err(report, "E_SESSION_ID", "%s: id is %r" % (name, session.get("id")))
        for key in ("title", "pinned", "inserted_at", "updated_at", "messages"):
            if key not in session:
                _err(report, "E_SESSION_SHAPE", "%s: missing %r" % (name, key))
        if not _is_iso(session.get("inserted_at")):
            _err(report, "E_TIME", "%s: inserted_at is not a Z timestamp" % name)
        if not _is_iso(session.get("updated_at")):
            _err(report, "E_TIME", "%s: updated_at is not a Z timestamp" % name)
        if not _nfc_text(session):
            _err(report, "E_NAME_NOT_NFC", "%s: session JSON contains non-NFC strings" % name)
        total, unique = _check_tree(session, name, report)
        report["messages"] += unique
        report["sessions"] += 1

    if index is not None:
        index_ids = [item.get("id") for item in index if isinstance(item, dict)]
        if len(index_ids) != len(set(index_ids)):
            _err(report, "E_INDEX_MISMATCH", "duplicate ids inside the index")
        missing = sorted(set(session_ids) - set(index_ids))
        extra = sorted(set(index_ids) - set(session_ids))
        if missing:
            _err(report, "E_INDEX_MISMATCH", "sessions missing from index: %s" % ", ".join(map(str, missing)))
        if extra:
            _err(report, "E_INDEX_MISMATCH", "index lists absent sessions: %s" % ", ".join(map(str, extra)))
        for item in index:
            if not isinstance(item, dict):
                continue
            for key in ("title", "pinned", "message_count", "inserted_at", "updated_at"):
                if key not in item:
                    _err(report, "E_INDEX_JSON", "index entry %r missing %r" % (item.get("id"), key))
            if not _is_iso(item.get("inserted_at")) or not _is_iso(item.get("updated_at")):
                _err(report, "E_TIME", "index entry %r has a non-Z timestamp" % (item.get("id"),))

    # images: referenced set vs present set
    names_nfc = {nfc(n) for n in names}
    referenced: Dict[str, str] = {}
    for stem, session in sessions.items():
        for msg in session.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            for frag in msg.get("fragments") or []:
                if not isinstance(frag, dict) or frag.get("type") != "FILE":
                    continue
                for entry in frag.get("files") or []:
                    if not isinstance(entry, dict) or not entry.get("is_image"):
                        continue
                    path = entry.get("export_path")
                    if not isinstance(path, str):
                        continue
                    norm = nfc(path)
                    if not norm.startswith(SESSIONS_DIR + stem + "_files/"):
                        _err(report, "E_IMAGE_OUTSIDE", "%s: %s" % (stem, norm))
                    fid = str(entry.get("id"))
                    if norm in referenced and referenced[norm] != fid:
                        _err(report, "E_IMAGE_SHARED", "%s: %s <-> %s" % (norm, referenced[norm], fid))
                    referenced[norm] = fid
                    if norm not in names_nfc:
                        _err(report, "E_IMAGE_MISSING", "%s: referenced but absent" % norm)
    image_names = [n for n in names if _classify(n) == "image"]
    report["images"] = len(image_names)
    for name in image_names:
        if nfc(name) not in referenced:
            _warn(report, "W_IMAGE_ORPHAN", "%s: present but not referenced" % name)

    # what a rewrite could fix
    if names and MANIFEST_PATH in names and names[0] != MANIFEST_PATH:
        report["repairs"].append({"code": "E_MANIFEST_NOT_FIRST",
                                  "detail": "move manifest.json to the first entry"})
    not_nfc = [n for n in names if nfc(n) != n]
    if not_nfc:
        report["repairs"].append({"code": "E_NAME_NOT_NFC",
                                  "detail": "NFC-normalise %d entry name(s)" % len(not_nfc)})
    report["ok"] = not report["errors"]
    return report


def unrepairable(report: Dict[str, Any]) -> List[Dict[str, str]]:
    return [e for e in report["errors"] if e["code"] not in REPAIRABLE]


def fix(path: str, out_path: str) -> Dict[str, Any]:
    """Rewrite layout-level defects. Refuses when any content-level error exists."""
    report = audit(path)
    blockers = unrepairable(report)
    if blockers:
        raise UsageError("refusing to fix: %d non-layout error(s), first is %s"
                         % (len(blockers), blockers[0]["code"]))
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        contents = {i.filename: zf.read(i) for i in infos}
        order = [MANIFEST_PATH] + [n for n in names if n != MANIFEST_PATH]
        tmp = out_path + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as out:
            for name in order:
                data = contents[name]
                new_name = nfc(name)
                if SESSION_RE.match(new_name):
                    try:
                        parsed = json.loads(data.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        parsed = None
                    if isinstance(parsed, dict):
                        parsed = _normalise_export_paths(parsed)
                        data = json.dumps(parsed, ensure_ascii=False,
                                          separators=(",", ":")).encode("utf-8")
                out.writestr(new_name, data)
        os.replace(tmp, out_path)
    return report


def main(argv: List[str]) -> int:
    args = list(argv[1:])
    as_json = False
    out_path: Optional[str] = None
    target: Optional[str] = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--json":
            as_json = True
        elif arg == "--fix":
            if i + 1 >= len(args):
                print(USAGE, file=__import__("sys").stderr)
                return 2
            out_path = args[i + 1]
            i += 1
        elif arg in ("-h", "--help"):
            print(USAGE)
            return 0
        elif arg.startswith("-"):
            print(USAGE, file=__import__("sys").stderr)
            return 2
        elif target is None:
            target = arg
        else:
            print(USAGE, file=__import__("sys").stderr)
            return 2
        i += 1
    if target is None:
        print(USAGE, file=__import__("sys").stderr)
        return 2
    try:
        report = audit(target)
        if out_path is not None:
            fix(target, out_path)
            report["fixed_to"] = os.path.abspath(out_path)
            report["fixed"] = True
    except UsageError as exc:
        if as_json:
            print(json.dumps({"ok": False, "fatal": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print("error: %s" % exc, file=__import__("sys").stderr)
            print(USAGE, file=__import__("sys").stderr)
        return 2
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("%s: %d entries, %d sessions, %d messages, %d images, version %s"
              % (report["archive"], report["entries"], report["sessions"],
                 report["messages"], report["images"], report["format_version"]))
        for item in report["errors"]:
            print("ERROR %-24s %s — %s" % (item["code"], item["detail"], ERROR_TEXT.get(item["code"], "")))
        for item in report["warnings"]:
            print("WARN  %-24s %s" % (item["code"], item["detail"]))
        for item in report["repairs"]:
            print("FIXABLE %-22s %s" % (item["code"], item["detail"]))
        print("result: %s" % ("OK" if report["ok"] else "%d error(s)" % len(report["errors"])))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv))