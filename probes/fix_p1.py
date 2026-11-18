import pathlib
g = pathlib.Path("/Users/smoitozo/Code/edi-run/gates.py")
s = g.read_text()
old = '''    if cfg.get("levelClaimed") == "baseline" and not isinstance(cfg.get("readsBeforeFreeze"), list):
        raise GateError("baseline jobs must record readsBeforeFreeze (BL-1)")'''
new = '''    if cfg.get("levelClaimed") == "baseline" and not isinstance(cfg.get("readsBeforeFreeze"), list):
        raise GateError("baseline jobs must record readsBeforeFreeze (BL-1)")
    # frozenAt must be parseable and, for CL1+, strictly in the past: both are
    # refusals, so both run before the first log write (the ordering predicate
    # after the appends is then construction-guaranteed: the work-started event
    # is emitted after freeze, so a past frozenAt can never satisfy f_dt >= s_dt)
    f_raw = bar.get("frozenAt")
    if cfg.get("levelClaimed") != "baseline":
        if not f_raw:
            raise GateError("CL1+ jobs must carry bar.frozenAt (CL1-1)")
        try:
            f_dt = when(f_raw)
        except Exception as e:
            raise GateError(f"bar.frozenAt is not a parseable timestamp: {f_raw!r}") from e
        if f_dt >= datetime.now(timezone.utc):
            raise GateError(f"bar.frozenAt {f_raw} is not in the past; a bar frozen after work began is not a frozen bar (CL1-1)")'''
assert old in s
s = s.replace(old, new)
g.write_text(s)
import ast; ast.parse(s); print("gates OK")
