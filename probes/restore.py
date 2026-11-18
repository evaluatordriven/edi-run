import pathlib
p = pathlib.Path("/Users/smoitozo/Code/edi-run/probes/run_probes.py")
s = p.read_text()
assert '"read_held_out"' not in s

missing = (
    '    # reads a held_out file at an absolute path baked before freeze (S2)\n'
    '    "read_held_out": PREAMBLE + (\n'
    '        "try:\\n"\n'
    '        "    val = open(pkt[\'_probe_ho\']).read().strip()[:20]\\n"\n'
    '        "except Exception:\\n"\n'
    '        "    val = \'DENIED\'\\n"\n'
    '        + WRITE_MEMO +\n'
    '        "(art / \'leak.txt\').write_text(\'LEAK=\' + val, encoding=\'utf-8\')\\n"\n'
    '    ),\n'
    '    # counts visible pids in /proc (S3)\n'
    '    "read_proc": PREAMBLE + (\n'
    '        "try:\\n"\n'
    '        "    pids = len([q for q in os.listdir(\'/proc\') if q.isdigit()])\\n"\n'
    '        "except Exception:\\n"\n'
    '        "    pids = -1\\n"\n'
    '        + WRITE_MEMO +\n'
    '        "(art / \'leak.txt\').write_text(\'LEAK=PROC_PIDS=%d\' % pids, encoding=\'utf-8\')\\n"\n'
    '    ),\n'
    '    # reads /proc/1/environ for the parent secret (S4)\n'
    '    "read_proc_environ": PREAMBLE + (\n'
    '        "try:\\n"\n'
    '        "    env1 = open(\'/proc/1/environ\').read()\\n"\n'
    '        "    val = \'LEAKED\' if \'PROBE_PARENT_SECRET\' in env1 else \'CLEAN\'\\n"\n'
    '        "except Exception:\\n"\n'
    '        "    val = \'DENIED\'\\n"\n'
    '        + WRITE_MEMO +\n'
    '        "(art / \'leak.txt\').write_text(\'LEAK=\' + val, encoding=\'utf-8\')\\n"\n'
    '    ),\n'
    '    # appends to a code file outside the workspace (S5)\n'
    '    "write_code": PREAMBLE + (\n'
    '        "try:\\n"\n'
    '        "    open(PROBE_CODE_TARGET, \'a\').write(\'x\')\\n"\n'
    '        "    val = \'WROTE\'\\n"\n'
    '        "except Exception:\\n"\n'
    '        "    val = \'DENIED\'\\n"\n'
    '        + WRITE_MEMO +\n'
    '        "(art / \'leak.txt\').write_text(\'LEAK=\' + val, encoding=\'utf-8\')\\n"\n'
    '    ),\n'
    '    # fetches a non-proxy loopback URL (S6)\n'
    '    "net_probe": PREAMBLE + (\n'
    '        "import urllib.request as _u\\n"\n'
    '        "try:\\n"\n'
    '        "    _u.urlopen(PROBE_NET_TARGET, timeout=5)\\n"\n'
    '        "    val = \'REACHED\'\\n"\n'
    '        "except Exception:\\n"\n'
    '        "    val = \'BLOCKED\'\\n"\n'
    '        + WRITE_MEMO +\n'
    '        "(art / \'leak.txt\').write_text(\'LEAK=\' + val, encoding=\'utf-8\')\\n"\n'
    '    ),\n'
)
anchor = '    # reads a canary planted next to the JOBS ROOT'
assert anchor in s
s = s.replace(anchor, missing + anchor)
# leak_value/agent_leak read "LEAK=" in write_* but S1 uses LEAK= too; normalize keys:
s = s.replace("(art / 'leak.txt').write_text('CANARY2_VALUE='", "(art / 'leak.txt').write_text('LEAK='")
s = s.replace("(art / 'leak.txt').write_text('PROXY_STATUS='", "(art / 'leak.txt').write_text('LEAK=HTTP_")
# S4 check expects DENIED/CLEAN, S3 parses PROC_PIDS=, S6 BLOCKED, S5 DENIED, S2 DENIED -- all via LEAK= now
s = s.replace('return ("PASS", "parent /proc/1/environ unreachable") if val in ("DENIED", "CLEAN")',
              'return ("PASS", "parent /proc/1/environ unreachable") if val in ("DENIED", "CLEAN")')
p.write_text(s)
import ast; ast.parse(s); print("scripts restored, AST OK")
