"""Seat execution adapters with temp-dir isolation and bounded timeouts. Standard library only."""
import json, os, pathlib, shutil, subprocess, tempfile, urllib.parse, urllib.request
import gates


def _resolve_cmd(cmd, job_dir):
    return [cmd[0]] + [str((job_dir / a).resolve()) if job_dir and not a.startswith("-") and (job_dir / a).exists() else a for a in cmd[1:]]


def run_maker(maker_cfg, packet_path, out_dir, subject_files, job_dir, timeout=60):
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd, env = _resolve_cmd(list(maker_cfg["command"]), job_dir), {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir()}
    with tempfile.TemporaryDirectory(prefix="edi_maker_") as sb:
        sb_p, sb_art = pathlib.Path(sb), pathlib.Path(sb) / "artifact"
        sb_art.mkdir()
        for sf in (sf for sf in subject_files if gates.safe_rel(sf, out_dir).is_file()):
            dst = gates.safe_rel(sf, sb_art)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(gates.safe_rel(sf, out_dir), dst)
        shutil.copy2(packet_path, sb_p / "packet.json")
        proc = subprocess.run(cmd + [str(sb_p / "packet.json"), str(sb_art)], cwd=str(sb_p), env=env, capture_output=True, text=True, timeout=max(1.0, float(timeout)))
        if proc.returncode != 0:
            raise RuntimeError(f"Maker failed ({proc.returncode}): {proc.stderr.strip()}")
        for sf in subject_files:
            cand = gates.safe_rel(sf, sb_art)
            if cand.is_symlink():
                raise gates.GateError(f"Maker produced symlink for {sf}")
            if cand.is_file():
                dst = gates.safe_rel(sf, out_dir)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cand, dst)


def run_evaluator(eval_cfg, target_path, dimension_id, job_dir=None, harness_out=None, timeout=60):
    if "command" in eval_cfg:
        cmd = _resolve_cmd(list(eval_cfg["command"]), job_dir)
        with tempfile.TemporaryDirectory(prefix="edi_eval_") as sb:
            sb_t = pathlib.Path(sb) / "target"
            (shutil.copytree if target_path.is_dir() else shutil.copy2)(target_path, sb_t)
            proc = subprocess.run(cmd + [str(sb_t), str(dimension_id)], cwd=sb, capture_output=True, text=True, timeout=max(1.0, float(timeout)))
        return {"unobserved": {"channel": eval_cfg["id"], "reason": proc.stderr.strip() or "nonzero exit"}} if proc.returncode != 0 else json.loads(proc.stdout.strip())
    req = urllib.request.Request(eval_cfg["url"], data=json.dumps({"dimension": dimension_id}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=max(1.0, float(timeout))) as resp:
        payload, ans_host = json.loads(resp.read().decode("utf-8")), urllib.parse.urlparse(resp.geturl()).hostname or ""
    if harness_out:
        harness_out.write_text(json.dumps({"evaluator": eval_cfg["id"], "endpoint": ans_host, "response": payload}, indent=2) + "\n", encoding="utf-8")
    return payload
