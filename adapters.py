"""Seat execution adapters (script subprocess + stdlib HTTP model client). Standard library only."""
import json
import pathlib
import subprocess
import urllib.parse
import urllib.request


def run_maker(maker_cfg, packet_path, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = list(maker_cfg["command"]) + [str(packet_path), str(out_dir)]
    proc = subprocess.run(cmd, cwd=str(packet_path.resolve().parent.parent.parent), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Maker failed ({proc.returncode}): {proc.stderr.strip()}")


def run_evaluator(eval_cfg, target_path, dimension_id, harness_out=None, job_dir=None):
    if "command" in eval_cfg:
        cmd = list(eval_cfg["command"]) + [str(target_path), str(dimension_id)]
        proc = subprocess.run(cmd, cwd=str(job_dir) if job_dir else None, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return {"unobserved": {"channel": eval_cfg["id"], "reason": proc.stderr.strip() or "nonzero exit"}}
        res = json.loads(proc.stdout.strip())
        if harness_out:
            harness_out.write_text(json.dumps({"evaluator": eval_cfg["id"], "model": eval_cfg.get("model"),
                                               "raw": res}, indent=2) + "\n", encoding="utf-8")
        return res

    url = eval_cfg["url"]
    host = urllib.parse.urlparse(url).hostname or ""
    body = json.dumps({"dimension": dimension_id, "target": str(target_path)}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw_bytes = resp.read()
    payload = json.loads(raw_bytes.decode("utf-8"))
    if harness_out:
        harness_out.write_text(json.dumps({"evaluator": eval_cfg["id"], "endpoint": host,
                                           "response": payload}, indent=2) + "\n", encoding="utf-8")
    return payload
