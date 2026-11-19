"""Shared helpers for edi-operator scripts.

Python 3 stdlib only. Conventions (docs/2026-EDI-OPERATOR-SKILL-SPEC.md §6):
read-only over custody artifacts, JSON on stdout, exit 0 = success,
1 = refusal/defect, 2 = usage error, never prompt, never print secrets.
"""
import json
import os
import pathlib
import re
import subprocess
import sys

EXIT_OK, EXIT_REFUSE, EXIT_USAGE = 0, 1, 2

# key-shaped values and key-named fields are scrubbed before anything is printed
_SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|Bearer\s+\S+)")
_SECRET_KEY_NAMES = {"api_key", "apikey", "api-key", "secret", "secret_key",
                     "password", "authorization", "credentials", "access_key"}


def repo_root():
    """EDI_REPO env, else the directory containing edi.py relative to this file.

    The relative fallback only holds when this script runs from inside the repo
    tree (an installed skill copy has no edi.py above it): when nothing resolves,
    refuse with a clear message instead of guessing.
    """
    env = os.environ.get("EDI_REPO")
    if env:
        return pathlib.Path(env)
    rel = pathlib.Path(__file__).resolve().parents[3]
    if (rel / "edi.py").is_file():
        return rel
    print("refusing: cannot locate edi.py; set EDI_REPO to the edi-run checkout",
          file=sys.stderr)
    sys.exit(EXIT_REFUSE)


def edi_py():
    return str(repo_root() / "edi.py")


def require_agent_id():
    """EDI_AGENT_ID is required; scripts refuse (exit 1) without it."""
    agent = os.environ.get("EDI_AGENT_ID", "").strip()
    if not agent:
        print("refusing: EDI_AGENT_ID is required (declare the operating agent's identity)",
              file=sys.stderr)
        sys.exit(EXIT_REFUSE)
    return agent


def edi_home():
    return pathlib.Path(os.environ.get("EDI_HOME") or pathlib.Path.home() / "edi-jobs")


def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        print(f"error reading {path}: {e}", file=sys.stderr)
        sys.exit(EXIT_REFUSE)


def scrub(value):
    """Recursively scrub key-shaped values / key-named fields from a structure."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _SECRET_KEY_NAMES:
                out[k] = "[REDACTED]"
            else:
                out[k] = scrub(v)
        return out
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        m = _SECRET_VALUE_RE.search(value)
        if m:
            return value[:m.start()] + "[REDACTED]" + value[m.end():]
        return value
    return value


def emit(payload, code=EXIT_OK):
    print(json.dumps(scrub(payload), indent=2))
    sys.exit(code)


def run_edi(args, timeout=120):
    """Run `python3 EDI_REPO/edi.py <args...>` from the repo root."""
    return subprocess.run([sys.executable, edi_py(), *[str(a) for a in args]],
                          capture_output=True, text=True, cwd=str(repo_root()),
                          timeout=timeout)


def refuse(msg):
    print(f"refusing: {msg}", file=sys.stderr)
    sys.exit(EXIT_REFUSE)
