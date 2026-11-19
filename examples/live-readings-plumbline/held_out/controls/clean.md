# Reference Control — Clean Conforming Plumbline Documentation

## Quickstart & Stranger Runner
Run every suite with one command (`python3 tests/run_all.py`). With no corpus it prints `SKIP[corpus] <id>` and ends with `PLUMBLINE-TALLY pass=7 fail=0 skip=5`.
- `EDI_CORPUS`: unset or `""` means not set; a non-corpus path exits 2 naming the path.
- `PLUMBLINE_REQUIRE_CORPUS`: only `1` turns it on (turning skips into `FAIL[corpus]`); any value other than unset/`""`/`0`/`1` exits 2.
- `CUSTODY_DIR`: points to a checkout of `evaluatordriven/custody` at `3e3ca58` for upstream drift and real-checker tests.

## Known Limitations
- **Hand-entered times:** The emitter refuses a hand-entered time by default (exit 3, kind `hand-time`). Under `--allow-hand-times` it records each one in `x-plumbline.hand_times[]` and never verifies it.
- **Rules not yet exercised:** Contracts v1.26.0 added `C37–C40`, the §4 roles table, and Instrument A2/I2. They are disclosed gaps, not covered rules.

## Two-Layer Custody Verification & Pins
Plumbline's schemas are derived from `evaluatordriven/doctrine` at `e-di@f8bfb793` (`f8bfb7934dbbefe3b32bc146b4481dba1744bc53`).
A custody record is verified at two layers: the standard layer is `checker/check.py` at `3e3ca58` (which ignores every key beginning with `x-`), and the Plumbline layer is `kit/plumbline_verify.py` (which runs `check.py` first and then enforces `x-plumbline`).
