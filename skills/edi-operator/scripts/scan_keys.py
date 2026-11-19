#!/usr/bin/env python3
"""Scan a tree for API-key-shaped strings WITHOUT touching any key file (§7.7).

Usage: scan_keys.py <dir> [<dir> ...]

Scans by the providers' PUBLISHED key formats — never by comparing against a
key file (never `grep -f <keyfile>`, `diff`, `cmp`, or a hash of the key: any
of those reads the key's contents into another process). On a match this tool
prints PATHS and COUNTS only, never the matching line.

Disclosed limit: split, encoded (base64/hex-wrapped) or truncated keys are not
caught. A clean scan is necessary, not sufficient.
"""
import pathlib
import re
import sys

# Published key formats per provider. Extend here, not in ad-hoc greps.
KEY_FORMATS = {
    "openrouter": re.compile(r"sk-or-[a-z0-9]+-[0-9a-f]{32,}"),
    "openai": re.compile(r"sk-(?!or-)[A-Za-z0-9_-]{20,}"),
    "anthropic": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "github": re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    "slack": re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    "aws": re.compile(r"AKIA[0-9A-Z]{16}"),
    "google": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "bearer": re.compile(r"[Bb]earer\s+[A-Za-z0-9_\-.=]{20,}"),
}
TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".py", ".sh", ".toml", ".cfg", ".log", ".csv"}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Scan trees for leaked keys by published format (§7.7); prints paths and counts only")
    ap.add_argument("dirs", nargs="+")
    a = ap.parse_args()
    findings = {}
    for d in a.dirs:
        root = pathlib.Path(d)
        if not root.is_dir():
            print(f"not a directory: {root}", file=sys.stderr)
            sys.exit(2)
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix not in TEXT_SUFFIXES:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for name, rx in KEY_FORMATS.items():
                n = len(rx.findall(text))
                if n:
                    findings[str(p)] = findings.get(str(p), {})
                    findings[str(p)][name] = findings[str(p)].get(name, 0) + n
    if findings:
        print("KEY-SHAPED STRINGS FOUND (paths and counts only — never print the match):")
        for path, per in sorted(findings.items()):
            for name, n in sorted(per.items()):
                print(f"  {path}: {name} x{n}")
        sys.exit(1)
    print("clean: no key-shaped strings found")
    sys.exit(0)


if __name__ == "__main__":
    main()
