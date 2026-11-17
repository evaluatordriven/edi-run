# edi-run

Minimal Python 3.10+ standard-library reference runtime for [Evaluator-Driven Iteration (E-DI)](https://github.com/evaluatordriven/doctrine) emitting conforming [`custody/v1` records](https://github.com/evaluatordriven/custody).

## Architecture

See [`docs/2026-EDI-Runtime-Solution-Architecture.md`](docs/2026-EDI-Runtime-Solution-Architecture.md).

## Examples

- `examples/two-round-job/` — Offline deterministic script Maker + script Evaluator (`CL1 · process`).
- `examples/live-llm-job/` — Live LLM Maker (`qwen/qwen3-32b`) + dual Evaluators (`StructGate` script + `anthropic/claude-opus-5.5` model) converging in 2 rounds (`CL1 · process`).
- `examples/live-cl2-job/` — Live multi-operator `CL2 · process` loop (`qwen/qwen3-32b` on `openrouter.ai` + `StructGate` script + `gemini-3-flash-preview` on `generativelanguage.googleapis.com`, `R=2` runs per dimension with `spread[]`).

## Usage

```sh
python3 edi.py freeze <job_dir>
python3 edi.py run <job_dir>
python3 edi.py finalize <job_dir> --disposition given --by "Casey Park"
python3 edi.py verify <job_dir>
```

## Verification & Tests

```sh
python3 -m unittest discover -s tests -p "test_*.py" -v
for ex in two-round-job live-llm-job live-cl2-job; do
  python3 vendor/record.py verify examples/$ex/job.jsonl --files
  python3 vendor/check.py examples/$ex/record.json --files examples/$ex/artifact --bar examples/$ex/bar.md --evidence examples/$ex/evidence
done
```
