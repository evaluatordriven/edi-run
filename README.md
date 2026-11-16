# edi-run

Minimal Python 3.10+ standard-library reference runtime for [Evaluator-Driven Iteration (E-DI)](https://github.com/evaluatordriven/doctrine) emitting conforming [`custody/v1` records](https://github.com/evaluatordriven/custody).

## Architecture

See [`docs/2026-EDI-Runtime-Solution-Architecture.md`](docs/2026-EDI-Runtime-Solution-Architecture.md).

## Usage

```sh
python3 edi.py freeze examples/two-round-job
python3 edi.py run examples/two-round-job
python3 edi.py finalize examples/two-round-job --disposition given --by "Casey Park"
python3 edi.py verify examples/two-round-job
```

## Verification & Tests

```sh
python3 -m unittest discover -s tests -p "test_*.py" -v
python3 vendor/record.py verify examples/two-round-job/job.jsonl --files
python3 vendor/check.py examples/two-round-job/record.json --files examples/two-round-job/artifact --bar examples/two-round-job/bar.md --evidence examples/two-round-job/evidence
```
