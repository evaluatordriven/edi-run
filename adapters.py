"""Seat execution adapters (script subprocess + stdlib OpenAI-compatible HTTP client). Standard library only."""
import json, os, pathlib, re, shutil, subprocess, tempfile, urllib.parse, urllib.request
import gates


def _resolve_cmd(cmd, job_dir):
    return [cmd[0]] + [str((job_dir / a).resolve()) if job_dir and not a.startswith("-") and (job_dir / a).exists() else a for a in cmd[1:]]


def _load_key(env_name="OPENROUTER_API_KEY"):
    if os.environ.get(env_name):
        return os.environ[env_name]
    env_p = pathlib.Path.home() / ".hermes" / ".env"
    for ln in (env_p.read_text(encoding="utf-8").splitlines() if env_p.is_file() else []):
        if ln.startswith(f"{env_name}="):
            return ln.split("=", 1)[1].strip().strip("\"'")
    return ""


def _chat_json(url, model_id, sys_prompt, user_prompt, key_env="OPENROUTER_API_KEY", timeout=60):
    key = _load_key(key_env)
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    body = json.dumps({"model": model_id, "temperature": 0.0, "reasoning": {"effort": "low"}, "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": "/no_think\n" + user_prompt}]}).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method="POST"), timeout=max(1.0, float(timeout))) as resp:
        raw, host = json.loads(resp.read().decode("utf-8")), urllib.parse.urlparse(resp.geturl()).hostname or ""
    txt = raw["choices"][0]["message"]["content"].strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", txt, re.S) or re.search(r"(\{.*\})", txt, re.S)
    return json.loads(m.group(1) if m else txt), host, raw


def run_maker(maker_cfg, packet_path, out_dir, subject_files, job_dir, timeout=60):
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="edi_maker_") as sb:
        sb_p, sb_art = pathlib.Path(sb), pathlib.Path(sb) / "artifact"
        sb_art.mkdir()
        for sf in (sf for sf in subject_files if gates.safe_rel(sf, out_dir).is_file()):
            dst = gates.safe_rel(sf, sb_art)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(gates.safe_rel(sf, out_dir), dst)
        shutil.copy2(packet_path, sb_p / "packet.json")
        if "command" in maker_cfg:
            cmd, env = _resolve_cmd(list(maker_cfg["command"]), job_dir), {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir()}
            proc = subprocess.run(cmd + [str(sb_p / "packet.json"), str(sb_art)], cwd=str(sb_p), env=env, capture_output=True, text=True, timeout=max(1.0, float(timeout)))
            if proc.returncode != 0:
                raise RuntimeError(f"Maker failed ({proc.returncode}): {proc.stderr.strip()}")
        else:
            pkt = json.loads((sb_p / "packet.json").read_text(encoding="utf-8"))
            method_txt = gates.safe_rel(maker_cfg["method"]["file"], job_dir).read_text(encoding="utf-8")
            cur_files = {sf: gates.safe_rel(sf, sb_art).read_text(encoding="utf-8") for sf in subject_files if gates.safe_rel(sf, sb_art).is_file()}
            sys_p = "You are the E-DI Maker seat. Return ONLY a JSON object mapping each filename to its full updated text: {\"files\": {\"<name>\": \"<content>\"}}. Address every reported defect without regressing passing dimensions."
            usr_p = f"METHOD:\n{method_txt}\n\nROUND PACKET:\n{json.dumps(pkt, indent=2)}\n\nINCUMBENT FILES:\n{json.dumps(cur_files, indent=2)}\n\nTARGET FILES: {subject_files}"
            res, _, _ = _chat_json(maker_cfg["url"], maker_cfg["model"]["identifier"], sys_p, usr_p, maker_cfg.get("apiKeyEnv", "OPENROUTER_API_KEY"), timeout)
            for sf, content in (res.get("files") or {}).items():
                dst = gates.safe_rel(sf, sb_art)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(str(content), encoding="utf-8")
        for sf in subject_files:
            cand = gates.safe_rel(sf, sb_art)
            if cand.is_symlink():
                raise gates.GateError(f"Maker produced symlink for {sf}")
            if cand.is_file():
                dst = gates.safe_rel(sf, out_dir)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cand, dst)


def run_evaluator(eval_cfg, target_path, dimension_id, job_dir=None, harness_out=None, timeout=60, dim_cfg=None, rubric_txt=""):
    if "command" in eval_cfg:
        cmd = _resolve_cmd(list(eval_cfg["command"]), job_dir)
        with tempfile.TemporaryDirectory(prefix="edi_eval_") as sb:
            sb_t = pathlib.Path(sb) / "target"
            (shutil.copytree if target_path.is_dir() else shutil.copy2)(target_path, sb_t)
            proc = subprocess.run(cmd + [str(sb_t), str(dimension_id)], cwd=sb, capture_output=True, text=True, timeout=max(1.0, float(timeout)))
        return {"unobserved": {"channel": eval_cfg["id"], "reason": proc.stderr.strip() or "nonzero exit"}} if proc.returncode != 0 else json.loads(proc.stdout.strip())
    txt = "\n---\n".join(f"FILE {p.name}:\n{p.read_text(encoding='utf-8')}" for p in sorted(target_path.rglob("*")) if p.is_file()) if target_path.is_dir() else target_path.read_text(encoding="utf-8")
    sys_p = "You are an independent E-DI Evaluator seat. Judge the artifact strictly against the dimension criterion and scale anchors. Return ONLY JSON: {\"score\": <number>, \"observation\": \"<exact quote from artifact>\", \"remedy\": \"<concise fix if below threshold, else empty>\"}"
    usr_p = f"RUBRIC:\n{rubric_txt}\n\nDIMENSION: {json.dumps(dim_cfg or {'id': dimension_id})}\n\nARTIFACT:\n{txt}"
    try:
        payload, ans_host, raw = _chat_json(eval_cfg["url"], eval_cfg["model"]["identifier"], sys_p, usr_p, eval_cfg.get("apiKeyEnv", "OPENROUTER_API_KEY"), timeout)
        if harness_out:
            harness_out.write_text(json.dumps({"evaluator": eval_cfg["id"], "endpoint": ans_host, "model": raw.get("model"), "usage": raw.get("usage"), "verdict": payload}, indent=2) + "\n", encoding="utf-8")
        return payload
    except Exception as e:
        if harness_out:
            harness_out.write_text(json.dumps({"evaluator": eval_cfg["id"], "error": str(e)}, indent=2) + "\n", encoding="utf-8")
        return {"unobserved": {"channel": eval_cfg["id"], "reason": str(e)}}
