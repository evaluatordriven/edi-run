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


def _require_https(url):
    """Credentials never travel over plain http; loopback is exempt (recheck H1)."""
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError(f"refusing to send credentials over non-https endpoint: {url}")


def _chat_json(url, model_id, sys_prompt, user_prompt, key_env="OPENROUTER_API_KEY", timeout=60, meter=None, who=None):
    _require_https(url)
    key = _load_key(key_env)
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
    extra = {"reasoning": {"effort": "low"}} if "openrouter.ai" in url else {}
    u_txt = ("/no_think\n" + user_prompt) if "qwen" in model_id.casefold() else user_prompt
    body = json.dumps({"model": model_id, "temperature": 0.0, **extra, "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": u_txt}]}).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method="POST"), timeout=max(1.0, float(timeout))) as resp:
        raw, host = json.loads(resp.read().decode("utf-8")), urllib.parse.urlparse(resp.geturl()).hostname or ""
    # C6/C7: the meter fires on the BILLED ENVELOPE, before any content parse —
    # a reply the seat cannot parse was still billed and must still accrue
    if meter is not None:
        meter.meter_call(who or "seat", model_id, url, None, usage=raw.get("usage"), round_no=getattr(meter, "_round", None))
    txt = raw["choices"][0]["message"]["content"].strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", txt, re.S) or re.search(r"(\{.*\})", txt, re.S)
    return json.loads(m.group(1) if m else txt), host, raw


# Home credential-dotfile family denied to the agent on BOTH platforms (one
# list, consumed by the seatbelt profile and the bwrap masks -- they must not
# drift; round-2 close-out #1).
_HOME_DOTFILE_DIRS = ("config", "aws", "gnupg", "kube", "docker")
_HOME_DOTFILE_FILES = ("netrc", "git-credentials", "zsh_history", "bash_history", "wget-hsts", "npmrc", "pypirc",
                       "gitcookies", "dockercfg", "yarnrc.yml", "pgpass", "my.cnf", "vault-token", "s3cfg", "boto", "hgrc")


def sandbox_prefix(stype, jd, sb_p, reads="denylist"):
    """The sandbox argv prefix for one launch, shared by the launcher and the
    capability probe so the probe exercises the exact flag shape used at runtime.
    Masks are applied ONLY to paths that already exist: bwrap creates missing mount
    points on the host, and a freeze check must never write anything.
    reads="allowlist" (S1): the seat sees ONLY the workspace, the resolved
    interpreter tree, and the system runtime — every other path simply does not
    exist inside the namespace. Default remains the deny-list profile until the
    S7-S9 probes prove the allowlist on every CI leg."""
    allowlist = reads == "allowlist"
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
        home = pathlib.Path.home().resolve()  # seatbelt/bwrap match canonical paths only
        # bwrap applies mounts in argv order: BINDS FIRST, MASKS AFTER — a later
        # --ro-bind / shadows earlier tmpfs mounts (the regression the CI legs caught)
        cmd = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-net",
               "--dev", "/dev", "--proc", "/proc"]
        if allowlist:
            # S1: bind ONLY the system runtime + the resolved interpreter tree; the
            # host's rest of the filesystem does not exist inside the namespace
            interp = pathlib.Path(sys.executable).resolve()
            for b in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt", "/var/lib"):
                if pathlib.Path(b).exists():
                    cmd += ["--ro-bind", b, b]
            cmd += ["--ro-bind", str(interp), str(interp)]  # interpreter may live outside /usr
            pref = pathlib.Path(getattr(sys, "base_prefix", sys.prefix) or sys.prefix)
            try:
                pref = pref.resolve()
            except OSError:
                pass
            if str(pref) != "/usr" and pref.is_dir():
                cmd += ["--ro-bind", str(pref), str(pref)]
        else:
            cmd += ["--ro-bind", "/", "/"]
        cmd += ["--tmpfs", "/tmp", "--tmpfs", str(jd)]
        for ho in sorted(set(jd.parent.glob("*/held_out")) | set(jd.parent.rglob("held_out"))):  # every job's controls, any depth
            if ho.is_dir() and ho.parent != jd:
                cmd += ["--tmpfs", str(ho)]
        if (jd / "scripts").is_dir():
            cmd += ["--ro-bind", str(jd / "scripts"), str(jd / "scripts")]
        if (home / ".ssh").is_dir():
            cmd += ["--tmpfs", str(home / ".ssh")]
        if (home / ".hermes" / ".env").is_file():
            cmd += ["--ro-bind", "/dev/null", str(home / ".hermes" / ".env")]
        # credential-dotfile parity with the macOS profile (round-2 close-out #1)
        for n in _HOME_DOTFILE_DIRS:
            d = home / ("." + n)
            if d.is_dir():
                cmd += ["--tmpfs", str(d)]
        for n in _HOME_DOTFILE_FILES:
            f = home / ("." + n)
            if f.is_file():
                cmd += ["--ro-bind", "/dev/null", str(f)]
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
            probe_cmd = sandbox_prefix(stype, probe_jd, probe_sb)
            assert probe_cmd is not None  # stype is checked above
            if stype == "bwrap":  # the probe must exercise the relay too
                probe_cmd = probe_cmd + [sys.executable, "-I", "-c", _RELAY, str(probe_sb / "proxy.sock"), str(_INNER_PROXY_PORT_BASE), "true"]
            else:
                probe_cmd = probe_cmd + ["true"]
            probe = subprocess.run(probe_cmd, capture_output=True, timeout=30)
            return probe.returncode == 0
    return False


_INNER_PROXY_PORT_BASE = 18080  # agent's view of the proxy inside its own netns (bwrap); offset per launch
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
    "        for _ in range(60):\n"  # the proxy socket may not exist yet at first accept
    "            try:\n"
    "                u.connect(sock_path)\n"
    "                break\n"
    "            except OSError:\n"
    "                import time; time.sleep(0.05)\n"
    "        else:\n"
    "            c.close(); u.close(); continue\n"  # never kill the serve loop; no FD leak (review LOW #3)
    "        threading.Thread(target=pipe, args=(c, u), daemon=True).start()\n"
    "        threading.Thread(target=pipe, args=(u, c), daemon=True).start()\n"
    "threading.Thread(target=serve, daemon=True).start()\n"
    "sys.exit(subprocess.call(cmd))\n"
)


class _NoCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """The proxy holds the API key; a redirect to a different host must not receive it."""

    def __init__(self, upstream_host):
        self.upstream_host = upstream_host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlparse(newurl)
        if parts.hostname != self.upstream_host or parts.scheme != urllib.parse.urlparse(req.full_url).scheme:
            raise urllib.error.HTTPError(req.full_url, code, f"redirect to {newurl} refused (cross-host or scheme downgrade)", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def run_maker(maker_cfg, packet_path, out_dir, subject_files, job_dir, timeout: float = 60, meter=None, round_no=None):
    mk_usage = None
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="edi_maker_") as sb:
        sb_p, sb_art = pathlib.Path(sb).resolve(), pathlib.Path(sb).resolve() / "artifact"
        sb_art.mkdir()
        (sb_p / "tmp").mkdir()  # the agent's only writable temp root (writes are workspace-confined)
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
            cap_flag = [] if meter is None else [None]
            upstream = maker_cfg["url"]
            _require_https(upstream)  # the proxy holds the key: no plain http (recheck H1)
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
                    # C4: the cap fires BEFORE each paid call — a trip mid-round
                    # halts with no unbillable spend (metering design)
                    if meter is not None:
                        try:
                            meter.check_cap(body)
                        except Exception as cap_err:
                            cap_flag[0] = cap_err
                            calls.append({"host": None, "model": req.get("model"), "usage": None, "refused": "cost cap"})
                            out = json.dumps({"error": {"code": 402, "message": "cost cap reached"}}).encode("utf-8")
                            self.send_response(402)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(out)))
                            self.end_headers()
                            self.wfile.write(out)
                            return
                    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
                    host, status, err = urllib.parse.urlparse(upstream).hostname or "", 200, None
                    upstream_host = urllib.parse.urlparse(upstream).hostname or ""
                    opener = urllib.request.build_opener(_NoCrossHostRedirect(upstream_host))
                    try:
                        with opener.open(urllib.request.Request(upstream, data=body, headers=headers, method="POST"), timeout=max(1.0, float(timeout))) as resp:
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
                    # C1/C2: the row is durable (fsynced) BEFORE the response reaches
                    # the agent — a crash or kill after the upstream bill cannot lose it
                    if meter is not None and status == 200:
                        try:
                            meter.meter_call("maker", req.get("model"), upstream, body, usage=usage, round_no=round_no)
                        except Exception as meter_err:
                            # a meter failure must NOT serve an unmetered billed call (fsync discipline)
                            out = json.dumps({"error": {"code": 503, "message": "meter unavailable"}}).encode("utf-8")
                            self.send_response(503)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(out)))
                            self.end_headers()
                            calls.append({"host": None, "model": req.get("model"), "usage": None, "refused": "meter failure"})
                            self.wfile.write(out)
                            return
                    calls.append({"host": host, "model": req.get("model"), "usage": usage, **({"upstream_error": err} if err else {})})
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)

            inner_port = _INNER_PROXY_PORT_BASE + (os.getpid() % 2000)  # per-launch offset; concurrent jobs must not collide
            if (maker_cfg.get("sandbox") or {}).get("type") == "bwrap":
                class _UnixProxy(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
                    daemon_threads = True
                srv = _UnixProxy(str(sb_p / "proxy.sock"), _Proxy)
                proxy_url = f"http://127.0.0.1:{inner_port}/v1/chat/completions"
            else:
                srv = http.server.HTTPServer(("127.0.0.1", 0), _Proxy)
                proxy_url = f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(sb_p), "LANG": os.environ.get("LANG", "en_US.UTF-8"),
                       "TMPDIR": str(sb_p / "tmp"),
                       "AGENT_PROXY_URL": proxy_url, "AGENT_MODEL": maker_cfg.get("model", {}).get("identifier", "")}
                cmd = _resolve_cmd(list(maker_cfg["command"]), job_dir)
                stype = (maker_cfg.get("sandbox") or {}).get("type")
                reads = (maker_cfg.get("sandbox") or {}).get("reads") or "denylist"
                jd = pathlib.Path(job_dir).resolve()  # seatbelt matches canonical paths only
                home = pathlib.Path.home().resolve()  # seatbelt matches canonical paths only
                if stype not in ("sandbox-exec", "bwrap"):
                    raise gates.GateError(f"unknown sandbox type {stype}; supported: sandbox-exec (macOS), bwrap (Linux)")
                if stype == "sandbox-exec":
                    q = chr(34)  # seatbelt profiles quote paths with double quotes
                    allow = (maker_cfg.get("sandbox") or {}).get("reads") == "allowlist"
                    if allow:
                        # S1 on macOS: seatbelt CANNOT express the allowlist on this
                        # OS build. Two walls, both verified by execution:
                        # (1) deny-precedence — a (deny file-read*) rule beats a
                        #     (allow file-read*) rule regardless of order, so
                        #     "deny everything except the allowlist" is inexpressible;
                        # (2) deny-default profiles — (deny default) with a NARROW
                        #     file-read allowlist abort sandbox-exec itself
                        #     (Abort trap 6, 5/5 reproducible; broad file-read*
                        #     allow runs fine — the crash is the denied startup
                        #     reads surfacing as a hard abort, not a clean EPERM).
                        # The allowlist therefore lands on bwrap (Linux) only, where
                        # selective binds make unlisted paths literally nonexistent.
                        # macOS keeps the deny-list profile and the S1 disclosure.
                        raise gates.GateError(
                            'sandbox.reads="allowlist" is not supported for sandbox-exec on this macOS build: '
                            'seatbelt deny-precedence defeats narrow exceptions and (deny default) profiles abort '
                            '(reproducible Abort trap 6). The allowlist is a bwrap/Linux capability (S1).')
                    else:
                        # Reads: default-allow except held-out material, the custody/evidence files
                        # (the Maker works from the packet, not the log), and secret files.
                        # Writes: confined to the agent workspace. Network is denied except
                        # loopback to the local proxy; the agent holds no key and the proxy
                        # rejects undeclared models (see README for the disclosed limits).
                        profile = (
                        "(version 1)(allow default)\n"
                        "(deny file-write*)\n"
                        f"(allow file-write* (subpath {q}{sb_p}{q}))\n"
                        f"(deny file-read* (subpath {q}{jd / 'held_out'}{q}))\n"
                        f"(deny file-read* (subpath {q}{jd / 'evidence'}{q}))\n"
                        f"(deny file-read* (literal {q}{jd / 'job.jsonl'}{q}))\n"
                        f"(deny file-read* (literal {q}{jd / 'scores.jsonl'}{q}))\n"
                        f"(deny file-read* (literal {q}{jd / 'usage.jsonl'}{q}))\n"  # spend telemetry never leaks into the seat
                        f"(deny file-read* (literal {q}{jd / 'job.json'}{q}))\n"
                        f"(deny file-read* (subpath {q}{home / '.ssh'}{q}))\n"
                        f"(deny file-read* (literal {q}{home / '.hermes' / '.env'}{q}))\n"
                        # credential-bearing home dotfiles beyond .ssh: the agent must not
                        # read what the deny list does not name (round-2 adversarial #7)
                        + "".join(
                            f"(deny file-read* (subpath {q}{home / ('.' + n)}{q}))\n"
                            for n in _HOME_DOTFILE_DIRS
                        )
                        + "".join(
                            f"(deny file-read* (literal {q}{home / ('.' + n)}{q}))\n"
                            for n in _HOME_DOTFILE_FILES
                        )
                        + "(deny file-read* (regex #\"/held_out(\\.|/|$)\"))\n"
                        "(deny network*)\n"
                        f"(allow network-outbound (remote ip {q}localhost:{srv.server_port}{q}))\n")
                    (sb_p / "agent.sb").write_text(profile, encoding="utf-8")
                # bwrap: fresh network namespace (round-2 E) -- the only network path is
                # the relay to the proxy's Unix socket; the whole job dir is masked with
                # a tmpfs -- held_out, evidence,
                # and the custody files live inside it -- then the agent's own scripts
                # subtree is bound back read-only. The packet is read from the workspace copy.
                relay = [sys.executable, "-I", "-c", _RELAY, str(sb_p / "proxy.sock"), str(inner_port)] if stype == "bwrap" else []
                cmd = (sandbox_prefix(stype, jd, sb_p, reads=reads) or []) + relay + cmd
                proc = subprocess.run(cmd + [str(sb_p / "packet.json"), str(sb_art)], cwd=str(sb_p), env=env, capture_output=True, text=True, timeout=max(1.0, float(timeout)), stdin=subprocess.DEVNULL)  # no inherited tty: no TIOCSTI keystroke injection (recheck #5)
                mk_usage = {"agent_calls": calls}  # metered per-call by the loop
                round_dir = pathlib.Path(out_dir).resolve().parent.parent
                if round_dir.is_dir():
                    # accumulate, not clobber: every round's proxy log must survive for
                    # audit — INCLUDING the cap-trip raise below (refused calls are audit)
                    with (round_dir / "proxy-usage.jsonl").open("a", encoding="utf-8") as f:
                        for c in calls:
                            f.write(json.dumps(c) + "\n")
                if cap_flag and cap_flag[0] is not None:
                    raise cap_flag[0]  # a mid-round cap trip halts as cap_reached, never as maker_failed
                if proc.returncode != 0:
                    raise RuntimeError(f"Agent Maker failed ({proc.returncode}): {proc.stderr.strip()} | cmd[0:4]={cmd[:4]}")
                if not any(gates.safe_rel(sf, sb_art).is_file() for sf in subject_files):
                    raise RuntimeError("Agent Maker returned no subject files")
            finally:
                srv.shutdown()
                srv.server_close()
        elif "command" in maker_cfg:
            cmd, env = _resolve_cmd(list(maker_cfg["command"]), job_dir), {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir()}
            proc = subprocess.run(cmd + [str(sb_p / "packet.json"), str(sb_art)], cwd=str(sb_p), env=env, capture_output=True, text=True, timeout=max(1.0, float(timeout)), stdin=subprocess.DEVNULL)  # no inherited tty: no TIOCSTI keystroke injection (recheck #5)
            if proc.returncode != 0:
                raise RuntimeError(f"Maker failed ({proc.returncode}): {proc.stderr.strip()}")
        else:
            pkt = json.loads((sb_p / "packet.json").read_text(encoding="utf-8"))
            method_txt = gates.safe_rel(maker_cfg["method"]["file"], job_dir).read_text(encoding="utf-8")
            cur_files = {sf: gates.safe_rel(sf, sb_art).read_text(encoding="utf-8") for sf in subject_files if gates.safe_rel(sf, sb_art).is_file()}
            sys_p = "You are the E-DI Maker seat. Return ONLY a JSON object mapping each filename to its full updated text: {\"files\": {\"<name>\": \"<content>\"}}. Address every reported defect without regressing passing dimensions."
            usr_p = f"METHOD:\n{method_txt}\n\nROUND PACKET:\n{json.dumps(pkt, indent=2)}\n\nINCUMBENT FILES:\n{json.dumps(cur_files, indent=2)}\n\nTARGET FILES: {subject_files}"
            if meter is not None:
                meter.check_cap(json.dumps({"messages": [{"role": "system", "content": sys_p}, {"role": "user", "content": usr_p}]}))
            res, _, raw = _chat_json(maker_cfg["url"], maker_cfg["model"]["identifier"], sys_p, usr_p, maker_cfg.get("apiKeyEnv", "OPENROUTER_API_KEY"), timeout, meter=meter, who="maker")
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


def run_evaluator(eval_cfg, target_path, dimension_id, job_dir=None, harness_out=None, timeout: float = 60, dim_cfg=None, rubric_txt="", meter=None):
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
        if meter is not None:
            meter.check_cap(json.dumps({"messages": [{"role": "system", "content": sys_p}, {"role": "user", "content": usr_p}]}))
        payload, ans_host, raw = _chat_json(eval_cfg["url"], eval_cfg["model"]["identifier"], sys_p, usr_p, eval_cfg.get("apiKeyEnv", "OPENROUTER_API_KEY"), timeout, meter=meter, who="evaluator:" + str(eval_cfg.get("id")))
        if harness_out:
            harness_out.write_text(json.dumps({"evaluator": eval_cfg["id"], "endpoint": ans_host, "model": raw.get("model"), "usage": raw.get("usage"), "verdict": payload}, indent=2) + "\n", encoding="utf-8")
        return dict(payload, _endpoint=ans_host, _usage=raw.get("usage"))
    except Exception as e:
        if harness_out:
            harness_out.write_text(json.dumps({"evaluator": eval_cfg["id"], "error": str(e)}, indent=2) + "\n", encoding="utf-8")
        return {"unobserved": {"channel": eval_cfg["id"], "reason": str(e)}}
