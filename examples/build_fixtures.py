#!/usr/bin/env python3
"""Materialise example archives from the committed JSON specs.

No ZIP is committed: .zip files are build products, written under --out.
Each spec is {"comment": str, "entries": [{"name": str, "text": str} |
{"name": str, "b64": str}]}, in the exact order the entry is written.
"""
import argparse
import base64
import json
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))


def build(spec_path, out_dir):
    with open(spec_path, encoding="utf-8") as fh:
        spec = json.load(fh)
    stem = os.path.splitext(os.path.basename(spec_path))[0]
    out_path = os.path.join(out_dir, stem + ".zip")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as zf:
        for entry in spec["entries"]:
            data = entry["text"].encode("utf-8") if "text" in entry else base64.b64decode(entry["b64"])
            zf.writestr(entry["name"], data)
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--spec", default=os.path.join(HERE, "in"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    for name in sorted(os.listdir(args.spec)):
        if name.endswith(".json"):
            print(build(os.path.join(args.spec, name), args.out))


if __name__ == "__main__":
    main()
