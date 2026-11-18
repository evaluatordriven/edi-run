import pathlib
p = pathlib.Path("/Users/smoitozo/Code/edi-run/probes/run_probes.py")
s = p.read_text()
assert "def check_s2" not in s

# 1) proper P1 check + metered_spend/P1_PRICING helpers + wrong_model agent script
insert = '''

P1_PRICING = {"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}}


def metered_spend(job):
    st = json.loads((job / "state.json").read_text())
    return st.get("cost_usd", 0.0), st.get("terminal_state"), st.get("reason")


def check_p1():
    reset_upstream()
    job = make_job(ENV.jobs, "p1", maker_kind="agent", url=ENV.upstream_url,
                   pricing={"declared-model": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}},
                   agent_mode="wrong_model")
    sc = job / "scripts" / "maker.py"
    sc.write_text(sc.read_text().replace("'undeclared-model'", "'undeclared-" + uuid.uuid4().hex[:6] + "'"), encoding="utf-8")
    loop.freeze_job(job)
    loop.run_job(job)
    val = agent_leak(job)
    code = val.split("=", 1)[-1] if "=" in val else val
    saw_undeclared = [c for c in _FakeUpstream.calls if "undeclared-" in c["model"]]
    ok = saw_undeclared == [] and code.startswith("403")
    return ("PASS", "undeclared model 403'd by the proxy, never forwarded (agent saw %s)" % code[:24]) if ok else ("FAIL", "agent saw %s; upstream calls: %s" % (code, _FakeUpstream.calls[:2]))
'''
anchor = "\n\n# --- probe environment"
assert anchor in s
s = s.replace(anchor, insert + anchor)

wrong_model = (
    '    # asks the proxy for an UNDECLARED model; records what it saw\n'
    '    "wrong_model": PREAMBLE + (\n'
    '        "import urllib.request as _u, urllib.error as _e\\n"\n'
    '        "url = os.environ[\'AGENT_PROXY_URL\']\\n"\n'
    '        "req = _u.Request(url, data=json.dumps({\'model\': \'undeclared-model\', \'messages\': [{\'role\': \'user\', \'content\': \'x\'}]}).encode(), headers={\'Content-Type\': \'application/json\'})\\n"\n'
    '        "try:\\n"\n'
    '        "    with _u.urlopen(req, timeout=15) as r: code = str(r.status)\\n"\n'
    '        "except _e.HTTPError as e:\\n"\n'
    '        "    code = str(e.code)\\n"\n'
    '        "except Exception as e:\\n"\n'
    '        "    code = \'EXC:\' + repr(e)[:40]\\n"\n'
    '        + WRITE_MEMO +\n'
    '        "(art / \'leak.txt\').write_text(\'PROXY_STATUS=\' + code, encoding=\'utf-8\')\\n"\n'
    '    ),\n'
)
anchor2 = '    # reads a canary planted next to the JOBS ROOT'
assert anchor2 in s
s = s.replace(anchor2, wrong_model + anchor2)

# 2) append the checks body
body = pathlib.Path("/Users/smoitozo/Code/edi-run/probes/checks_body.py").read_text()
s = s + body
p.write_text(s)
import ast; ast.parse(s); print("appended, AST OK")
