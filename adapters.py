"""Seat execution adapters (script subprocess + stdlib OpenAI-compatible HTTP client). Standard library only."""
import http.server, json, os, pathlib, re, shutil, socketserver, subprocess, sys, tempfile, threading, urllib.error, urllib.parse, urllib.request
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
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError(f"refusing to send credentials over non-https endpoint: {url}")
    key = _load_key(key_env)
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    extra = {"reasoning": {"effort": "low"}} if "openrouter.ai" in url else {}
    u_txt = ("/no_think\n" + user_prompt) if "qwen" in model_id.casefold() else user_prompt
    body = json.dumps({"model": model_id, "temperature": 0.0, **extra, "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": u_txt}]}).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method="POST"), timeout=max(1.0, float(timeout))) as resp:
        raw, host = json.loads(resp.read().decode("utf-8")), urllib.parse.urlparse(resp.geturl()).hostname or ""
    txt = raw["choices"][0]["message"]["content"].strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", txt, re.S) or re.search(r"(\{.*\})", txt, re.S)
    return json.loads(m.group(1) if m else txt), host, raw


def sandbox_prefix(stype, jd, sb_p):
    """The sandbox argv prefix for one launch, shared by the launcher and the
    capability probe so the probe exercises the exact flag shape used at runtime.
    Masks are applied ONLY to paths that already exist: bwrap creates missing mount
    points on the host, and a freeze check must never write anything."""
    if stype == "sandbox-exec":
        profile = sb_p / "agent.sb"
        return ["sandbox-exec", "-f", str(profile)]
    if stype == "bwrap":
        # Read-only host (round-2 A): only the agent workspace is writable, and nothing
        # is ever created on the host. Own network namespace (round-2 E): the only way
        # out is the relay to the proxy's Unix socket. Every job's controls are masked
        # (round-2 B): control sets are reused across jobs under one jobs root.
        home = pathlib.Path.home()
        cmd = ["bwrap", "--die-with-parent", "--unshare-pid", "--unshare-net",
               "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
               "--tmpfs", str(jd)]
        for ho in sorted(jd.parent.glob("*/held_out")):  # every job's controls
            if ho.is_dir() and ho.parent != jd:
                cmd += ["--tmpfs", str(ho)]
        if (jd / "scripts").is_dir():
            cmd += ["--ro-bind", str(jd / "scripts"), str(jd / "scripts")]
        if (home / ".ssh").is_dir():
            cmd += ["--tmpfs", str(home / ".ssh")]
        if (home / ".hermes" / ".env").is_file():
            cmd += ["--ro-bind", "/dev/null", str(home / ".hermes" / ".env")]
        return cmd + ["--bind", str(sb_p), str(sb_p)]
    return None


def sandbox_usable(stype, jd=None, sb_p=None):
    """True when the declared sandbox can actually launch here, not merely when the
    binary exists (Ubuntu hosted runners ship bwrap but forbid user namespaces).
    Probes with the real flag shape via sandbox_prefix."""
    if stype == "sandbox-exec":
        return bool(shutil.which("sandbox-exec"))
    if stype == "bwrap":
        if not shutil.which("bwrap"):
            return False
        with tempfile.TemporaryDirectory(prefix="edi_bwrap_probe_") as pd:
            probe_jd, probe_sb = pathlib.Path(pd) / "job", pathlib.Path(pd) / "sb"
            probe_jd.mkdir(), probe_sb.mkdir()
            probe = subprocess.run(sandbox_prefix(stype, probe_jd, probe_sb) + ["true"],
                                   capture_output=True, timeout=30)
            return probe.returncode == 0
    return False


_INNER_PROXY_PORT = 18080  # the agent's view of the proxy inside its own network namespace (bwrap)
_RELAY = (
    "import socket, subprocess, sys, threading\n"
    "sock_path, port, cmd = sys.argv[1], int(sys.argv[2]), sys.argv[3:]\n"
    "srv = socket.socket()\n"
    "srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "srv.bind(('127.0.0.1', port))\n"
    "srv.listen(16)\n"
    "def pipe(a, b):\n"
    "    try:\n"
    "        while True:\n"
    "            d = a.recv(65536)\n"
    "            if not d:\n"
    "                break\n"
    "            b.sendall(d)\n"
    "    except OSError:\n"
    "        pass\n"
    "    finally:\n"
    "        for s in (a, b):\n"
    "            try:\n"
    "                s.shutdown(socket.SHUT_RDWR)\n"
    "            except OSError:\n"
    "                pass\n"
    "def serve():\n"
    "    while True:\n"
    "        c, _ = srv.accept()\n"
    "        u = socket.socket(socket.AF_UNIX)\n"
    "        u.connect(sock_path)\n"
    "        threading.Thread(target=pipe, args=(c, u), daemon=True).start()\n"
    "        threading.Thread(target=pipe, args=(u, c), daemon=True).start()\n"
    "threading.Thread(target=serve, daemon=True).start()\n"
    "sys.exit(subprocess.call(cmd))\n"
)


def run_maker(maker_cfg, packet_path, out_dir, subject_files, job_dir, timeout: float = 60):
    mk_usage = None
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="edi_maker_") as sb:
        sb_p, sb_art = pathlib.Path(sb).resolve(), pathlib.Path(sb).resolve() / "artifact"
        sb_art.mkdir()
        for sf in (sf for sf in subject_files if gates.safe_rel(sf, out_dir).is_file()):
            dst = gates.safe_rel(sf, sb_art)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(gates.safe_rel(sf, out_dir), dst)
        shutil.copy2(packet_path, sb_p / "packet.json")
        if maker_cfg.get("kind") == "agent":
            # Agent Maker: a blocking child in its own confined workspace. The agent
            # calls models ONLY through a local proxy that holds the key and logs every
            # upstream call (host, model, usage) -- the agent's environment carries no
            # credentials, and every token is metered toward the cost cap.
            usage_log = sb_p / "proxy-usage.jsonl"
            upstream = maker_cfg["url"]
            key = _load_key(maker_cfg.get("apiKeyEnv", "OPENROUTER_API_KEY"))
            calls = []

            class _Proxy(http.server.BaseHTTPRequestHandler):
                def log_message(self, format, *args):  # noqa: A002 -- stdlib signature
                    return

                def do_POST(self):
                    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    req = json.loads(body.decode("utf-8")) if body else {}
                    declared = (maker_cfg.get("model") or {}).get("identifier")
                    if req.get("model") != declared:
                        calls.append({"host": None, "model": req.get("model"), "usage": None, "refused": "undeclared model"})
                        self.send_response(403)
                        self.end_headers()
                        return
                    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
                    host, status, err = urllib.parse.urlparse(upstream).hostname or "", 200, None
                    try:
                        with urllib.request.urlopen(urllib.request.Request(upstream, data=body, headers=headers, method="POST"), timeout=max(1.0, float(timeout))) as resp:
                            out, host = resp.read(), urllib.parse.urlparse(resp.geturl()).hostname or ""
                    except urllib.error.HTTPError as e:
                        status, err = e.code, e.code
                        out = e.read() or json.dumps({"error": {"code": e.code}}).encode("utf-8")
                    except (urllib.error.URLError, OSError):
                        status, err = 502, "unreachable"
                        out = json.dumps({"error": {"code": 502, "message": "upstream unreachable"}}).encode("utf-8")
                    try:
                        usage = json.loads(out.decode("utf-8")).get("usage") if status == 200 else None
                    except (ValueError, AttributeError):
                        usage = None
                    calls.append({"host": host, "model": req.get("model"), "usage": usage, **({"upstream_error": err} if err else {})})
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)

            if (maker_cfg.get("sandbox") or {}).get("type") == "bwrap":
                class _UnixProxy(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
                    daemon_threads = True
                srv = _UnixProxy(str(sb_p / "proxy.sock"), _Proxy)
                proxy_url = f"http://127.0.0.1:{_INNER_PROXY_PORT}/v1/chat/completions"
            else:
                srv = http.server.HTTPServer(("127.0.0.1", 0), _Proxy)
                proxy_url = f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(sb_p), "LANG": os.environ.get("LANG", "en_US.UTF-8"),
                       "AGENT_PROXY_URL": proxy_url, "AGENT_MODEL": maker_cfg.get("model", {}).get("identifier", "")}
                cmd = _resolve_cmd(list(maker_cfg["command"]), job_dir)
                stype = (maker_cfg.get("sandbox") or {}).get("type")
                jd = pathlib.Path(job_dir).resolve()  # seatbelt matches canonical paths only
                home = pathlib.Path.home()
                if stype not in ("sandbox-exec", "bwrap"):
                    raise gates.GateError(f"unknown sandbox type {stype}; supported: sandbox-exec (macOS), bwrap (Linux)")
                if stype == "sandbox-exec":
                    q = chr(34)  # seatbelt profiles quote paths with double quotes
                    # Reads: default-allow except held-out material, the custody/evidence files
                    # (the Maker works from the packet, not the log), and secret files.
                    # Writes: confined to the agent workspace. Network stays open so the agent
                    # can reach the local proxy; it holds no key and the proxy rejects
                    # undeclared models (keyless-open-endpoint exfil is a disclosed limit).
                    (sb_p / "agent.sb").write_text(
                        "(version 1)(allow default)\n"
                        "(deny file-write*)\n"
                        f"(allow file-write* (subpath {q}{sb_p}{q}))\n"
                        f"(deny file-read* (subpath {q}{jd / 'held_out'}{q}))\n"
                        f"(deny file-read* (subpath {q}{jd / 'evidence'}{q}))\n"
                        f"(deny file-read* (literal {q}{jd / 'job.jsonl'}{q}))\n"
                        f"(deny file-read* (literal {q}{jd / 'scores.jsonl'}{q}))\n"
                        f"(deny file-read* (literal {q}{jd / 'job.json'}{q}))\n"
                        f"(deny file-read* (subpath {q}{home / '.ssh'}{q}))\n"
                        f"(deny file-read* (literal {q}{home / '.hermes' / '.env'}{q}))\n"
                        "(deny file-read* (regex #\"/held_out(/|$)\"))\n"
                        "(deny network*)\n"
                        f"(allow network-outbound (remote ip {q}localhost:{srv.server_port}{q}))\n",
                        encoding="utf-8")
                # bwrap: keep the network namespace (the agent must reach the loop-local
                # proxy); the whole job dir is masked with a tmpfs -- held_out, evidence,
                # and the custody files live inside it -- then the agent's own scripts
                # subtree is bound back read-only. The packet is read from the workspace copy.
                relay = [sys.executable, "-I", "-c", _RELAY, str(sb_p / "proxy.sock"), str(_INNER_PROXY_PORT)] if stype == "bwrap" else []
                cmd = (sandbox_prefix(stype, jd, sb_p) or []) + relay + cmd
                proc = subprocess.run(cmd + [str(sb_p / "packet.json"), str(sb_art)], cwd=str(sb_p), env=env, capture_output=True, text=True, timeout=max(1.0, float(timeout)))
                if proc.returncode != 0:
                    raise RuntimeError(f"Agent Maker failed ({proc.returncode}): {proc.stderr.strip()} | cmd[0:4]={cmd[:4]}")
                if not any(gates.safe_rel(sf, sb_art).is_file() for sf in subject_files):
                    raise RuntimeError("Agent Maker returned no subject files")
                mk_usage = {"agent_calls": calls}  # metered per-call by the loop
                round_dir = pathlib.Path(out_dir).resolve().parent.parent
                if round_dir.is_dir():
                    # accumulate, not clobber: every round's proxy log must survive for audit
                    with (round_dir / "proxy-usage.jsonl").open("a", encoding="utf-8") as f:
                        for c in calls:
                            f.write(json.dumps(c) + "\n")
            finally:
                srv.shutdown()
                srv.server_close()
        elif "command" in maker_cfg:
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
            res, _, raw = _chat_json(maker_cfg["url"], maker_cfg["model"]["identifier"], sys_p, usr_p, maker_cfg.get("apiKeyEnv", "OPENROUTER_API_KEY"), timeout)
            for sf, content in (res.get("files") or {}).items():
                dst = gates.safe_rel(sf, sb_art)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(str(content), encoding="utf-8")
            mk_usage = raw.get("usage")
        for sf in subject_files:
            cand = gates.safe_rel(sf, sb_art)
            if cand.is_symlink():
                raise gates.GateError(f"Maker produced symlink for {sf}")
            if cand.is_file():
                dst = gates.safe_rel(sf, out_dir)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cand, dst)
    return {"usage": mk_usage} if mk_usage else None


def run_evaluator(eval_cfg, target_path, dimension_id, job_dir=None, harness_out=None, timeout: float = 60, dim_cfg=None, rubric_txt=""):
    if "command" in eval_cfg:
        cmd = _resolve_cmd(list(eval_cfg["command"]), job_dir)
        try:
            with tempfile.TemporaryDirectory(prefix="edi_eval_") as sb:
                sb_t = pathlib.Path(sb) / "target"
                (shutil.copytree if target_path.is_dir() else shutil.copy2)(target_path, sb_t)
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": sb, "LANG": os.environ.get("LANG", "en_US.UTF-8")}
                proc = subprocess.run(cmd + [str(sb_t), str(dimension_id)], cwd=sb, env=env, capture_output=True, text=True, timeout=max(1.0, float(timeout)))
                if proc.returncode != 0:
                    return {"unobserved": {"channel": eval_cfg["id"], "reason": proc.stderr.strip() or "nonzero exit"}}
                try:
                    return json.loads(proc.stdout.strip())
                except (json.JSONDecodeError, ValueError):
                    return {"unobserved": {"channel": eval_cfg["id"], "reason": "evaluator printed non-JSON output"}}
        except subprocess.TimeoutExpired:
            return {"unobserved": {"channel": eval_cfg["id"], "reason": "evaluator timed out"}}
    txt = "\n---\n".join(f"FILE {p.name}:\n{p.read_text(encoding='utf-8')}" for p in sorted(target_path.rglob("*")) if p.is_file()) if target_path.is_dir() else target_path.read_text(encoding="utf-8")
    sys_p = "You are an independent E-DI Evaluator seat. Judge the artifact strictly against the dimension criterion and scale anchors. Return ONLY JSON: {\"score\": <number>, \"observation\": \"<exact quote from artifact>\", \"remedy\": \"<concise fix if below threshold, else empty>\"}"
    usr_p = f"RUBRIC:\n{rubric_txt}\n\nDIMENSION: {json.dumps(dim_cfg or {'id': dimension_id})}\n\nARTIFACT:\n{txt}"
    try:
        payload, ans_host, raw = _chat_json(eval_cfg["url"], eval_cfg["model"]["identifier"], sys_p, usr_p, eval_cfg.get("apiKeyEnv", "OPENROUTER_API_KEY"), timeout)
        if harness_out:
            harness_out.write_text(json.dumps({"evaluator": eval_cfg["id"], "endpoint": ans_host, "model": raw.get("model"), "usage": raw.get("usage"), "verdict": payload}, indent=2) + "\n", encoding="utf-8")
        return dict(payload, _endpoint=ans_host, _usage=raw.get("usage"))
    except Exception as e:
        if harness_out:
            harness_out.write_text(json.dumps({"evaluator": eval_cfg["id"], "error": str(e)}, indent=2) + "\n", encoding="utf-8")
        return {"unobserved": {"channel": eval_cfg["id"], "reason": str(e)}}
