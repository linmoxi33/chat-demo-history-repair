# chat-demo-history-repair

Audit — and layout-repair — a `deepseek_data-*.zip` history archive offline, before any importer touches it.

Why now: `deepseek-ai/chat-demo` climbed to ~4,700 stars this week, and the only thing that can tell you whether an archive is importable is the app itself (browser TS + IndexedDB). There is no way to check a downloaded archive on a laptop, in CI, or on a server, and no way to fix the two layout defects that are safe to fix without touching user content.

Upstream: https://github.com/deepseek-ai/chat-demo

## What the upstream is, and what it does not provide

`deepseek-ai/chat-demo` exports all conversations as a ZIP described by `export_format_version: 3`, and re-imports it by replacing every local conversation in one transaction. Its own docs pin the archive shape: `manifest.json` must be the first entry, entry names are compared after NFC normalisation, images must live under `chat_sessions/{session_id}_files/`, and only ZIP32 STORE/DEFLATE with UTF-8 names is accepted.

The gap, in the project's own words and code (verified against commit `5d5dd754ebc1948e977ddb880a9eb5aa25f37b4c`):

- The validator is browser-only: `src/services/chat/history/zipReader.ts#L66` normalises names and rejects unsupported ZIP features while reading from a `File`, so nothing outside the app can preflight an archive. README: "ZIP64、加密、分卷及非 UTF-8 的非 ASCII 文件名不支持".
- Image identity is compared on NFC-normalised paths on both sides (`paths.ts#L24`, `toLocal.ts#L301-L310`), and one entry may not map to two file ids — rules a third-party checker has to re-implement, not guess.
- Nothing repairs an archive. A file whose manifest landed last, or whose names are NFD (a common macOS artefact), is simply rejected, even though both defects are layout-level and content-preserving.

A second implementation of these rules is what this tool is. It is not a README restatement: the checks are run against real ZIP bytes and the repair rewrites bytes.

## What this tool does

1. Opens the archive and reads every entry with the Python stdlib (`zipfile`), then checks CRC/size integrity and the ZIP framing the app requires.
2. Applies the documented import contract: manifest first, version 3, no extra manifest keys, NFC entry names, safe path segments, no duplicates, known entry roles.
3. Checks sessions against the index one-to-one, session filename vs `id`, UTC `Z` timestamps, and the message tree (parent id below child id, parent exists, exactly one USER root, no repeated role along an edge, `current_message_id` present).
4. Checks image references: `FILE.export_path` must exist as an entry, must live under its own session's `_files/` directory, and may not be shared by two file ids; unreferenced images are a warning, not an error.
5. `--fix` rewrites only the two layout-level defects — manifest order and NFC names — with `FILE.export_path` normalised in the same pass. It refuses, with exit code 2 and no output file, as soon as any content-level error exists.

It does **not** convert archives to another app's format, import into IndexedDB, re-sign, decrypt, or repair content-level damage (CRC, broken trees, missing originals). It does not decide whether the app's own semantics are correct; it encodes the rules the app documents and implements, and cites them.

## Install

Python 3.8+. No third-party packages, no network, nothing to install:

```bash
git clone git@github-linmoxi33:linmoxi33/chat-demo-history-repair.git
cd chat-demo-history-repair
python3 examples/build_fixtures.py --out /tmp/cdhr-fixtures
python3 bin/chat-demo-history-repair /tmp/cdhr-fixtures/clean-v3.zip --json
```

`examples/build_fixtures.py` materialises the archives from the committed JSON specs in `examples/in/`; no ZIP is committed.

## Usage

```bash
python3 bin/chat-demo-history-repair <archive.zip>            # human report, exit 0/1
python3 bin/chat-demo-history-repair <archive.zip> --json     # machine report
python3 bin/chat-demo-history-repair <archive.zip> --fix out.zip
```

`--json` prints one object:

```json
{
  "archive": "nfd-image.zip",
  "ok": false,
  "format_version": 3,
  "entries": 4,
  "sessions": 1,
  "messages": 2,
  "images": 1,
  "errors": [{"code": "E_NAME_NOT_NFC", "detail": "entry name is not NFC: 'chat_sessions/s1_files/f1_cafe\u0301 photo.png'"}],
  "warnings": [],
  "repairs": [{"code": "E_NAME_NOT_NFC", "detail": "NFC-normalise 1 entry name(s)"}]
}
```

Exit codes: `0` clean archive, `1` the archive has findings, `2` usage error, unreadable file, or a `--fix` refused because content-level errors exist. `--fix` still reports the input's findings, so a repairable archive exits `1`.

## Limits

- The two repairs are deliberately narrow. A duplicate entry name, a CRC mismatch, or a dangling image reference is reported and never rewritten; silently "fixing" them would change what the user's archive means.
- `--fix` re-serialises session JSON compactly and rewrites `FILE.export_path`; it does not touch any other field. Field order inside session JSON can change.
- It checks the documented subset of `fragments` (only `FILE` image references). Fragment payloads such as search results, thinking blocks, and read-link signatures are not validated.
- It does not enforce ZIP size or browser resource limits, and does not emulate the app's IndexedDB staging transaction; it cannot prove an archive will commit successfully, only that it satisfies the documented contract.
- Timestamps are checked for the `Z`-suffixed shape, not for calendar validity beyond that.
- The example archives are generated from the documented format, not captured from a real user's browser; the README says so instead of pretending they are real exports.

## 与同类项目的差异

- `deepseek-ai/deepseek-harness` ships `packages/experimental/deepseek-chat-import` (283k-star repo), which **consumes** a `deepseek_data-*.zip` and converts it into DSH sessions in the browser. It is an importer, not a standalone checker; it has no CLI, no `--json`, no exit codes, and no repair pass. This tool is the preflight and repair layer that runs before any importer.
- `gh search repos "chat-demo history"`, `"deepseek export archive"`, and `"chat history zip validator"` returned no dedicated validator (see the search log in the round record). `gh search code "chat_session_index.json"` only finds that importer.
- No difference in format support is claimed beyond the documented ZIP32/UTF-8 scope; the point is that the contract is executable offline, and that the two layout defects are repairable.

## 本轮创新

B 档：第一次把 chat-demo 的 v3 导入契约做成**可离线执行的第二实现 + 字节级布局修复器**（manifest 前置、条目名与 `FILE.export_path` 同步 NFC 归一化），并把缺陷分成「布局可修」与「内容不可修」两类分别处置 —— 同类只有浏览器内的导入器，既不能预检也不能修。

## Tests

```bash
python3 -m unittest discover -s tests -v
```

15 tests: 6 audit cases, 3 repair cases (including the refusal path), 6 CLI-contract cases (exit 0/1/2, JSON parseability and byte-stability, no network modules).

MIT. Copyright (c) 2026 linmoxi33.