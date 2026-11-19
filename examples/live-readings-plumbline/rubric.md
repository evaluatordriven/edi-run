# Rubric — Plumbline v0.1.0 Public Documentation Census (`mode: "readings"`)

For each dimension (`D1`, `D2`, `D3`), return ONLY JSON in the readings slot shape:
`{"outcome": "conforms"|"does_not_conform"|"unobserved", "quotation": "<exact non-empty substring from the judged text>"}`

Do NOT include a numeric `score`. The `quotation` MUST be an exact, verbatim substring present in the judged text (`RD2`).

- **D1 (`stranger_quickstart`)**:
  - `conforms`: The text names `python3 tests/run_all.py`, the `PLUMBLINE-TALLY` line, and the exact rules for `EDI_CORPUS`, `PLUMBLINE_REQUIRE_CORPUS`, and `CUSTODY_DIR`. Quote the sentence or command line showing the tally or env-var contract.
  - `does_not_conform`: The text omits `tests/run_all.py` or omits the `EDI_CORPUS` / `PLUMBLINE_REQUIRE_CORPUS` contract. Quote the defective quickstart text.
- **D2 (`disclosed_limitations`)**:
  - `conforms`: The text explicitly discloses that hand-entered times are refused by default (`exit 3`, `hand-time`) and flagged under `--allow-hand-times`, and states that `C37–C40` are disclosed gaps rather than covered rules. Quote the sentence disclosing `C37–C40` or hand-time refusal.
  - `does_not_conform`: The text claims full coverage without disclosing unexercised rules (`C37–C40`) or claims hand-entered times are verified. Quote the overclaiming statement.
- **D3 (`two_layer_custody_and_pins`)**:
  - `conforms`: The text states the two verification layers (`checker/check.py` at `3e3ca58` ignoring `x-` keys, and `kit/plumbline_verify.py` enforcing `x-plumbline`) and cites the doctrine commit pin `f8bfb793`. Quote the sentence naming the two layers or the pin.
  - `does_not_conform`: The text conflates the standard checker with `x-plumbline` verification or omits the commit pins. Quote the defective verification description.
