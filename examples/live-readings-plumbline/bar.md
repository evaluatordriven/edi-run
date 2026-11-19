# Bar — Plumbline v0.1.0 Public Documentation Census (`mode: "readings"`, Entry B)

This census reads the existing Plumbline public documentation (`README.md` and `custody-emission.md` from `origin/main` `04cd745`) against three conformity dimensions without numeric scales (`C29`, `D4.6`, `D5.outcome.census`).

## Dimensions

- **D1 (`stranger_quickstart`)**: The documentation states a single-command stranger test runner (`python3 tests/run_all.py`) with its no-corpus tally (`PLUMBLINE-TALLY pass=7 fail=0 skip=5`), and explicitly specifies the behavior and exit codes of `EDI_CORPUS`, `PLUMBLINE_REQUIRE_CORPUS`, and `CUSTODY_DIR`.
- **D2 (`disclosed_limitations`)**: The documentation honestly discloses known limitations without overclaiming coverage: hand-entered times are refused by default (exit 3, kind `hand-time`) and flagged without verification under `--allow-hand-times`, and Contracts v1.26.0 rules `C37–C40` are explicitly disclosed as unexercised gaps rather than covered rules.
- **D3 (`two_layer_custody_and_pins`)**: The documentation specifies the two-layer custody verification boundary (`checker/check.py` standard layer at `3e3ca58` ignoring `x-` keys, followed by `kit/plumbline_verify.py` enforcing `x-plumbline`) and pins the doctrine corpus commit (`f8bfb793`).
