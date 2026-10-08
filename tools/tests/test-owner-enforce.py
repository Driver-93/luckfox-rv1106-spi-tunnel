#!/usr/bin/env python3
"""Unit test for mtx_enforce() -- the mediamtx session-kicking backstop.

Runs ON THE BOARD. Stubs web_server._mtx_api so the decisions are tested
deterministically, without needing a real browser to create a WebRTC session.

Why this matters more than it looks: the first version of mtx_enforce() read
the session list as a BARE ARRAY. mediamtx v1.11.3 actually returns a paginated
ENVELOPE -- {"itemCount":0,"pageCount":0,"items":[]} -- so the parse silently
produced "no sessions" and the kick never happened. Nothing logged, nothing
failed: "takeover worked but the old video kept playing". This test pins both
shapes down.
"""
import json
import sys

sys.path.insert(0, "/userdata/car")
import web_server as ws

fails = []


def check(name, got, want):
    ok = (got == want)
    if not ok:
        fails.append(name)
    print("  %s %-58s got=%r want=%r" % ("OK " if ok else "FAIL", name, got, want))


class FakeMTX:
    """Stub for _mtx_api: serves a canned session list and records kicks."""

    def __init__(self, payload):
        self.payload = payload
        self.kicked = []

    def __call__(self, method, path):
        if path.endswith("/list"):
            return 200, json.dumps(self.payload).encode()
        if "/kick/" in path:
            self.kicked.append(path.rsplit("/", 1)[1])
            return 200, b"{}"
        return 404, b""


OWNER_IP = "192.168.3.65"
OTHER_IP = "192.168.3.70"


def sess(sid, ip, port=5000):
    return {"id": sid, "remoteAddr": "%s:%d" % (ip, port), "path": "car"}


def set_owner(page, ip, gen):
    ws._OWN.update(page=page, ip=ip, since=1.0, n=gen)


print("=== 1) paginated envelope shape (what v1.11.3 actually returns) ===")
# Snapshot already taken at claim time (gen matches) and it contained nothing,
# which is the normal case: the claim runs before the page pulls video.
ws._MTX.update(gen=2, condemned=set())
fake = FakeMTX({"itemCount": 2, "pageCount": 1,
                "items": [sess("s1", OTHER_IP), sess("s2", OWNER_IP)]})
ws._mtx_api = fake
set_owner("tabB", OWNER_IP, 2)
ws.mtx_enforce()
check("envelope parsed; only the foreign session kicked", fake.kicked, ["s1"])

print()
print("=== 2) bare-array shape (older mediamtx) still works ===")
ws._MTX.update(gen=3, condemned=set())
fake = FakeMTX([sess("s1", OTHER_IP)])
ws._mtx_api = fake
set_owner("tabB", OWNER_IP, 3)
ws.mtx_enforce()
check("bare array parsed; foreign session kicked", fake.kicked, ["s1"])

print()
print("=== 3) the real takeover flow: snapshot at claim, then a new session ===")
# Step 1: the old tab (same IP as the new owner) already has a session, and a
# second device is also watching. The new page claims ownership -> the snapshot
# must capture BOTH.
ws._MTX.update(gen=-1, condemned=set())
fake = FakeMTX({"items": [sess("sOld", OWNER_IP), sess("sForeign", OTHER_IP)]})
ws._mtx_api = fake
set_owner("tabB", OWNER_IP, 4)
ws._snapshot_live_sessions()
check("snapshot at claim captures both live sessions",
      sorted(ws._MTX["condemned"]), ["sForeign", "sOld"])

# Step 2: enforcement kicks them, and the new page then connects (sNew).
fake = FakeMTX({"items": [sess("sOld", OWNER_IP), sess("sForeign", OTHER_IP)]})
ws._mtx_api = fake
ws.mtx_enforce()
check("both condemned sessions kicked", sorted(fake.kicked), ["sForeign", "sOld"])

# Step 3: the new page's own session appears AFTER the snapshot -> must live.
fake = FakeMTX({"items": [sess("sNew", OWNER_IP)]})
ws._mtx_api = fake
ws.mtx_enforce()
check("new session from the owner survives", fake.kicked, [])
check("condemned set forgotten once gone", ws._MTX["condemned"], set())

# Step 4: steady state keeps working.
fake = FakeMTX({"items": [sess("sNew", OWNER_IP), sess("sLate", OTHER_IP)]})
ws._mtx_api = fake
ws.mtx_enforce()
check("a later foreign session is still kicked", fake.kicked, ["sLate"])

print()
print("=== 4) no owner -> never kick (fail safe: leave video alone) ===")
ws._MTX.update(gen=-1, condemned=set())
fake = FakeMTX({"items": [sess("s1", OTHER_IP)]})
ws._mtx_api = fake
set_owner(None, None, 5)
ws.mtx_enforce()
check("no owner -> no kicks", fake.kicked, [])

print()
print("=== 5) API unreachable -> silent no-op, never raises ===")
def boom(method, path):
    return 0, b""
ws._mtx_api = boom
set_owner("tabB", OWNER_IP, 6)
try:
    ws.mtx_enforce()
    check("unreachable API handled", True, True)
except Exception as e:
    check("unreachable API handled", "raised %r" % e, True)

print()
print("=== 6) garbage response -> silent no-op ===")
def junk(method, path):
    return 200, b"<html>not json</html>"
ws._mtx_api = junk
try:
    ws.mtx_enforce()
    check("garbage body handled", True, True)
except Exception as e:
    check("garbage body handled", "raised %r" % e, True)

print()
print("=== 7) snapshot must survive an unreachable mediamtx ===")
ws._MTX.update(gen=-1, condemned=set())
def boom2(method, path):
    return 0, b""
ws._mtx_api = boom2
set_owner("tabB", OWNER_IP, 7)
try:
    ws._snapshot_live_sessions()
    check("snapshot with dead API does not raise", True, True)
    check("snapshot with dead API leaves condemned empty",
          ws._MTX["condemned"], set())
except Exception as e:
    check("snapshot with dead API does not raise", "raised %r" % e, True)

print()
if fails:
    print("FAILURES: %s" % ", ".join(fails))
    sys.exit(1)
print("all mtx_enforce tests passed")
