"""Deterministic evaluator E1: correctness + interface for median.py.

Contract: argv = <target_dir> <dimension_id>; prints ONE JSON object
{"score": <num>, "observation": <str>, "remedy": <str>}.
"""
import importlib.util
import json
import math
import pathlib
import sys


def main():
    art = pathlib.Path(sys.argv[1])
    # controls arrive as a single FILE (the subject file itself); round artifacts as a dir
    src_path = art if art.is_file() else (art / "median.py")
    dim = sys.argv[2]
    if dim == "D1":
        score, obs, remedy = 1, "", "implement the standard weighted median; raise ValueError on empty or mismatched input"
        try:
            spec = importlib.util.spec_from_file_location("median", src_path)
            if spec is None or spec.loader is None:
                raise ImportError("cannot load median.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cases = [
                ([1, 2, 3], [1, 1, 1], 2),
                ([3, 1, 2], [1, 1, 1], 2),
                ([1, 2, 3, 4], [1, 1, 1, 1], 2),
                ([1, 2, 3], [5, 1, 1], 1),
                ([10, -5, 7], [1, 3, 1], -5),
                ([42], [9], 42),
            ]
            ok = True
            for vals, ws, want in cases:
                got = mod.weighted_median(list(vals), list(ws))
                if not math.isclose(float(got), float(want), rel_tol=1e-9, abs_tol=1e-9):
                    ok = False
                    break
            if ok:
                for bad in (lambda: mod.weighted_median([], []), lambda: mod.weighted_median([1, 2], [1])):
                    try:
                        bad()
                        ok = False
                        break
                    except ValueError:
                        continue
                    except Exception:
                        ok = False
                        break
            score = 5 if ok else 1
            obs = "all public cases pass and error handling raises ValueError" if ok else "a public case or error contract failed"
        except Exception as e:
            score, obs = 1, f"artifact could not be evaluated: {type(e).__name__}: {e}"
        print(json.dumps({"score": score, "observation": obs, "remedy": remedy if score < 4 else ""}))
        return 0

    if dim == "D2":
        src = src_path.read_text(encoding="utf-8") if src_path.is_file() else ""
        score, problems = 5, []
        after_def = src.split("def weighted_median", 1)[-1][:600]
        if chr(34)*3 not in after_def:
            problems.append("function docstring missing")
        header = src[:200]
        if chr(34)*3 not in header:
            problems.append("module docstring missing")
        forbidden = [ln for ln in src.splitlines() if ln.strip().startswith(("import ", "from ")) and "typing" not in ln]
        if forbidden:
            problems.append("imports beyond typing: " + ", ".join(forbidden[:3]))
        if "def weighted_median(values, weights)" not in src:
            problems.append("signature is not def weighted_median(values, weights)")
        score = max(1, 5 - 2 * len(problems))
        print(json.dumps({
            "score": score,
            "observation": "; ".join(problems) if problems else "docstrings present, imports clean, signature exact",
            "remedy": "; ".join(problems) if score < 4 else "",
        }))
        return 0

    print(json.dumps({"score": 1, "observation": f"unknown dimension {dim}", "remedy": ""}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
