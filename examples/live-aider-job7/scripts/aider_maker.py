"""Agent seat: run Aider inside the confined workspace.

The runtime invokes: python3 scripts/aider_maker.py <packet.json> <artifact_dir>
The task text comes from scripts/task.md (pinned at freeze).
The ONLY model path is the metering proxy (AGENT_PROXY_URL). The real API key
never enters this process. Streaming is refused by the proxy, so Aider runs
with --no-stream. Git is disabled: the workspace is not a repository.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

# derive from the interpreter running this wrapper (the venv python): HOME inside
# the sandbox is the workspace, so Path.home() cannot locate the venv (job6)
AIDER = os.environ.get("AIDER_BIN") or str(pathlib.Path(sys.executable).parent / "aider")


def main() -> int:
    packet_path, art_dir = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    # The task text is a pinned job file (scripts/task.md), frozen with the job --
    # the Maker must not be able to change its own instructions.
    task = (pathlib.Path(__file__).resolve().parent / "task.md").read_text(encoding="utf-8")
    pkt = json.loads(packet_path.read_text(encoding="utf-8"))
    feedback = ""
    if pkt.get("defects"):
        feedback = "\n\nJudge findings to address:\n" + "\n".join(f"- {d}" for d in pkt["defects"])
    if pkt.get("workable_solutions"):
        feedback += "\n\nWorkable solutions suggested by the judge:\n" + "\n".join(f"- {s}" for s in pkt["workable_solutions"])

    proxy = os.environ["AGENT_PROXY_URL"]  # .../v1/chat/completions
    base = proxy[: -len("/chat/completions")] if proxy.endswith("/chat/completions") else proxy
    model = os.environ.get("AGENT_MODEL", "")
    aider_model = "openai/" + (model[len("openai/"):] if model.startswith("openai/") else model)

    env = dict(os.environ)
    env["OPENAI_API_KEY"] = "metered-by-edi-run-proxy"  # the proxy holds the real key
    env["OPENAI_API_BASE"] = base
    env["AIDER_DARK_MODE"] = "1"

    os.chdir(art_dir)
    cmd = [
        AIDER, "--no-stream", "--yes-always", "--no-git", "--no-auto-commits",
        "--no-check-update", "--no-show-model-warnings", "--no-fancy-input",
        "--no-pretty", "--chat-mode", "code",
        "--openai-api-base", base, "--model", aider_model,
        "--message", task + feedback,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=540, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-4000:])
        return proc.returncode or 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
