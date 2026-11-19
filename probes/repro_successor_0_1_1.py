"""Does a raise-bounds successor job (seed = prior incumbent, authored by the same Maker) freeze?"""
import json, os, pathlib, shutil, subprocess, sys, tempfile
repo = pathlib.Path(sys.argv[1]).resolve()
tmp = pathlib.Path(tempfile.mkdtemp(prefix="succ-")); home = tmp / "home"; home.mkdir()
env = {**os.environ, "EDI_HOME": str(home), "EDI_AGENT_ID": "probe-operator", "EDI_REPO": str(repo)}
S = repo / "skills/edi-operator/scripts"; ex = repo / "examples/two-round-job"
def run(*a): return subprocess.run([sys.executable, *map(str, a)], env=env, capture_output=True, text=True, timeout=600)
def job(name, seed_author=None, seed_from=None):
    assert run(S / "new_job.py", "--name", name).returncode == 0
    j = home / name
    for f in ("bar.md", "rubric.md", "method.md", "runner.md"): shutil.copy(ex / f, j / f)
    shutil.copytree(ex / "scripts", j / "scripts", dirs_exist_ok=True); shutil.copytree(ex / "held_out", j / "held_out", dirs_exist_ok=True)
    cfg = json.loads((ex / "job.json").read_text()); cfg["disposition"] = {"state": None, "by": None, "acknowledgesUnobserved": []}
    cfg["bounds"]["max_rounds"] = 1
    if seed_from:
        shutil.copytree(seed_from, j / "seed", dirs_exist_ok=True)
        cfg["seed"] = {"dir": "seed", "author": seed_author, "origin": "incumbent of predecessor job"}
        auth = (j / "AUTHORSHIP.md").read_text().splitlines()
        (j / "AUTHORSHIP.md").write_text("\n".join(f"Seed author: {seed_author}" if l.startswith("Seed author:") else l for l in auth) + "\n")
    (j / "job.json").write_text(json.dumps(cfg, indent=2)); return j, cfg
# 1. predecessor: capped at one round -> needs_owner/cap_reached, recommendation raise-bounds
p, cfg = job("pred")
print("pred freeze", run(repo / "edi.py", "freeze", p).returncode, "run", run(repo / "edi.py", "run", p).returncode)
st = json.loads((p / "state.json").read_text()); esc = json.loads((p / "escalation.json").read_text())
print("pred:", st["terminal_state"], st["reason"], "| options:", [o.get("id") or o.get("option") or o for o in esc.get("options", [])][:4])
r1 = st["rounds"][str(st["incumbent_round"])]["dir"]
inc = pathlib.Path(r1) if os.path.isabs(r1) else p / r1
art = next((d for d in [inc / "artifact", inc] if d.is_dir() and any(d.iterdir())), inc)
maker = cfg["maker"]["name"]; print("maker name:", maker, "| incumbent dir:", art.relative_to(tmp))
# 2. successor: same Maker, incumbent as seed, seed author = the Maker that produced it
s, _ = job("succ", seed_author=maker, seed_from=art)
pf = run(S / "preflight.py", s); print("successor preflight:", pf.stdout.strip()[:400])
