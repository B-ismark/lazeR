"""TEMPORARY Windows diagnostic: which in-process COM probes fail, and why.
Logs every child process the server starts, with the line that started it."""
import os
import subprocess
import sys
import traceback

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

_orig_init = subprocess.Popen.__init__


def _spy(self, args, *a, **kw):
    where = [f"{os.path.basename(f.filename)}:{f.lineno} {f.name}"
             for f in traceback.extract_stack()[:-1] if "remote_server" in f.filename]
    print(f"  !! SPAWN {args if isinstance(args, str) else ' '.join(map(str, args))[:120]}"
          f"  <- {' / '.join(where[-3:])}", flush=True)
    return _orig_init(self, args, *a, **kw)


subprocess.Popen.__init__ = _spy

print("frozen:", getattr(sys, "frozen", False), "python", sys.version, flush=True)
print("== import remote_server", flush=True)
import remote_server as rs  # noqa: E402

print("BRIGHTNESS_BACKEND", rs.BRIGHTNESS_BACKEND, "panel_known", rs.BRIGHTNESS_PANEL_KNOWN)


def probe(label, fn):
    print(f"== {label}", flush=True)
    try:
        print("  ->", repr(fn()), flush=True)
    except Exception:
        traceback.print_exc(file=sys.stdout)


def raw_wmi():
    import comtypes
    import comtypes.client
    try:
        comtypes.CoInitialize()
    except Exception as e:
        print("  CoInitialize:", repr(e))
    loc = comtypes.client.CreateObject("WbemScripting.SWbemLocator", dynamic=True)
    print("  locator:", type(loc))
    svc = loc.ConnectServer(".", r"root\cimv2")
    print("  service:", type(svc))
    res = svc.ExecQuery("SELECT Caption FROM Win32_OperatingSystem")
    print("  result set:", type(res))
    for obj in res:
        print("  item:", type(obj))
        return obj.Properties_.Item("Caption").Value
    return "EMPTY"


def raw_fw():
    pol = rs._fw_policy()
    print("  policy:", type(pol))
    print("  profiles:", pol.CurrentProfileTypes)
    rules = pol.Rules
    print("  rules:", type(rules), "count", rules.Count)
    try:
        rules.Item("definitely-not-a-rule")
    except Exception as e:
        print("  missing-rule error:", type(e), repr(e), "hresult", getattr(e, "hresult", None))
    n = 0
    for r in rules:
        n += 1
        if n == 1:
            print("  first rule:", type(r), r.Name, r.Direction, r.Action, r.Enabled, r.ApplicationName)
    return n


probe("raw WMI", raw_wmi)
probe("_wmi_com_works", rs._wmi_com_works)
probe("_wmi_is_laptop", rs._wmi_is_laptop)
probe("_wmi_brightness_get", rs._wmi_brightness_get)
probe("get_brightness", rs.get_brightness)
probe("raw firewall", raw_fw)
probe("_fw_current_profile_types", rs._fw_current_profile_types)
probe("_fw_rule_named_com", lambda: rs._fw_rule_named_com(rs.FW_RULE_NAME))
probe("firewall_rule_exists", rs.firewall_rule_exists)
probe("_fw_policy_rules_present", rs._fw_policy_rules_present)
probe("_fw_block_rules_com", rs._fw_block_rules_com)
probe("firewall_block_rules", rs.firewall_block_rules)
probe("firewall_rule_is_inert", rs.firewall_rule_is_inert)
print("== done", flush=True)
