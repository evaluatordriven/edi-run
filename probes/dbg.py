import sys, json, pathlib
src = open("probes/run_probes.py").read().split("CHECKS = [")[0]
src = src.replace('REPO = pathlib.Path(__file__).resolve().parent.parent', 'REPO = pathlib.Path("probes").resolve().parent')
ns = {"__file__": str(pathlib.Path("probes/run_probes.py").resolve())}
exec(compile(src, "rp", "exec"), ns)
ns["ENV"] = ns["ProbeEnv"]()
try:
    job = ns["agent_job"]("dbg", "read_jobsroot_canary", url=ns["ENV"].upstream_url,
                           pricing={"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}})
    print("---- maker.py ----")
    print((job / "scripts" / "maker.py").read_text())
    rc = ns["loop"].run_job(job)
    print("rc", rc)
    st = json.loads((job / "state.json").read_text())
    print("term:", st.get("terminal_state"), st.get("reason"))
    print("maker_error:", st.get("maker_error", "")[:400])
    art = sorted((job / "rounds").glob("round-*"))[-1] / "artifact"
    for f in art.iterdir():
        print("artifact:", f.name, f.read_text()[:80])
finally:
    ns["ENV"].close()
