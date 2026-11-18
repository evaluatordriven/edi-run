# Live Aider Maker Proof (`live-aider-job11`)

This directory holds the verified CL1 custody record (`record.json`, `job.jsonl`, `usage.jsonl`) for a live **Aider** agent Maker (`qwen/qwen3-32b` via the `edi-run` metering proxy inside `sandbox-exec`) repairing a seeded `median.py` implementation across two rounds:

- **Round 1 (seed)**: Seed draft (`seed/median.py`) scored `D1 = 1.0` (does not conform), `D2 = 5.0` (conforms).
- **Round 2 (Aider Maker)**: Aider repaired `median.py` inside the confined workspace via the metering proxy (`$0.002017` accrued across 2 calls); `D1 = 5.0`, `D2 = 5.0` -> `accepted` (`all_conform`).
- **Verification**: `python3 edi.py verify examples/live-aider-job11` -> exit `0` (`OK`, 27 custody events, chain intact).

## 10-Job Live-Agent Hardening Ladder (Historical Summary)

Each failed attempt during live-agent bring-up was finalized `declined` rather than edited post-freeze (per E-DI custody doctrine), and a fresh successor job was frozen with the fix:

| Attempt | Failure Mode Caught | Root Cause & Fix |
|---|---|---|
| `live-aider-job` | Evaluator `unobserved` (`IsADirectoryError`) | Evaluator script read `sys.argv[1]` as a file when target was an artifact directory; fixed script to resolve `target / "median.py"` when given a directory. |
| `live-aider-job2` | Freeze refused (`CL1-1` `bar.frozenAt` in future) | Stamped `bar.frozenAt` prior to running `freeze`. |
| `live-aider-job3` | Maker failed (`aider` prompted on stdin / git) | Added `--yes-always --no-git --no-auto-commits --no-check-update --no-fancy-input --no-pretty` and `stdin=DEVNULL`. |
| `live-aider-job4` | Maker failed (`IndexError` in prompt / task arg) | Moved task prompt into pinned `scripts/task.md` read by `scripts/aider_maker.py`. |
| `live-aider-job5` | Maker failed inside `sandbox-exec` (`PermissionError: /dev/null`) | Added `(allow file-write-data (literal "/dev/null"))` to the macOS seatbelt profile in `adapters.py`. |
| `live-aider-job6` | Maker failed (`FileNotFoundError` for `~/Library/Aider/...`) | Sandbox sets `HOME` to the temporary workspace; derived `AIDER` binary path from `pathlib.Path(sys.executable).parent / "aider"` instead of `Path.home()`. |
| `live-aider-job7` | Maker failed (`PYTHONPATH`/`PYTHONHOME` leak across interpreters) | Stripped `PYTHONPATH`, `PYTHONHOME`, and `PYTHONSTARTUP` in `scripts/aider_maker.py` before launching Aider. |
| `live-aider-job9` | Proxy refused streaming (`400 stream:true`) | Passed `--no-stream` to Aider so all completions are non-streaming and deterministically metered. |
| `live-aider-job10` | Dormant channel (`edit-format` mismatch on Qwen3) | Switched Aider to `--edit-format whole` and `/no_think` for `qwen/qwen3-32b`. |
| `live-aider-job11` | **Accepted (`all_conform`)** | Seed failed D1 in Round 1 (`1.0`); Aider repaired `median.py` in Round 2 (`5.0 / 5.0`); finalized `given` and verified `OK`. |
