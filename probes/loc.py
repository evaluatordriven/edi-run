#!/usr/bin/env python3
"""Core LOC gate for edi-run (enforces <= 1,500 code lines and <= 2,000 physical lines)."""
import io, pathlib, sys, tokenize

MAX_CODE = 1500
MAX_PHYSICAL = 2000
DEFAULT_FILES = ("edi.py", "gates.py", "loop.py", "adapters.py", "metering.py")


def count(p):
    src = pathlib.Path(p).read_text(encoding="utf-8")
    lines = src.splitlines()
    kind = ["code"] * len(lines)
    for i, ln in enumerate(lines):
        if not ln.strip():
            kind[i] = "blank"
    code_tok, com, doc = set(), set(), set()
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    for j, t in enumerate(toks):
        if t.type == tokenize.COMMENT:
            com.add(t.start[0])
        elif t.type == tokenize.STRING:
            k = j - 1
            while k >= 0 and toks[k].type in (tokenize.NL, tokenize.COMMENT):
                k -= 1
            nxt = j + 1
            while nxt < len(toks) and toks[nxt].type in (tokenize.NL, tokenize.COMMENT):
                nxt += 1
            if (k < 0 or toks[k].type in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)) and toks[nxt].type in (tokenize.NEWLINE, tokenize.ENDMARKER):
                for r in range(t.start[0], t.end[0] + 1):
                    doc.add(r)
            else:
                for r in range(t.start[0], t.end[0] + 1):
                    code_tok.add(r)
        elif t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
            for r in range(t.start[0], t.end[0] + 1):
                code_tok.add(r)
    c = cm = b = d = 0
    for i in range(1, len(lines) + 1):
        if kind[i - 1] == "blank":
            b += 1
        elif i in code_tok:
            c += 1
        elif i in doc:
            d += 1
        elif i in com:
            cm += 1
        else:
            c += 1
    return len(lines), c, cm + d, b, cm, d


def main():
    args = [a for a in sys.argv[1:] if a != "--check"]
    check = "--check" in sys.argv[1:] or not sys.argv[1:]
    files = args or list(DEFAULT_FILES)
    tot = [0] * 6
    print(f"{'file':<34}{'total':>7}{'code':>7}{'comment+doc':>13}{'blank':>7}")
    for p in files:
        r = count(p)
        tot = [a + b for a, b in zip(tot, r)]
        print(f"{p:<34}{r[0]:>7}{r[1]:>7}{r[2]:>13}{r[3]:>7}")
    print(f"{'TOTAL':<34}{tot[0]:>7}{tot[1]:>7}{tot[2]:>13}{tot[3]:>7}")
    if check and (tot[1] > MAX_CODE or tot[0] > MAX_PHYSICAL):
        print(f"LOC GATE FAILED: {tot[1]} code (max {MAX_CODE}) / {tot[0]} physical (max {MAX_PHYSICAL})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
