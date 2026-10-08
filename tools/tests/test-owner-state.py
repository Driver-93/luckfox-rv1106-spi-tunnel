#!/usr/bin/env python3
"""Unit test for the single-client takeover state machine in web_server.py.

Runs ON THE BOARD (it imports web_server, which pulls in car_motor/cam_ctl/
video_ctl from /userdata/car). Importing is safe: main() is guarded by
__name__, so no server starts.

What it pins down:
  * a page that has never been seen does NOT get ownership stolen from it
    just by polling (owner_view is read-only -- claiming only happens on
    /api/pageinfo and /api/takeover)
  * a new page takes over from the old one, and the old one is told
  * re-claiming with the SAME page id is not a takeover (a refresh must not
    count as "someone else took over", or every self-reload would fire the
    banner and a stop)
  * clients with no page id (old pages, curl, the latency probe) are reported
    as non-owners but never crash anything
"""
import sys
import time

sys.path.insert(0, "/userdata/car")
import web_server as ws

fails = []


def check(name, got, want):
    ok = (got == want)
    if not ok:
        fails.append(name)
    print("  %s %-52s got=%r want=%r" % ("OK " if ok else "FAIL", name, got, want))


print("=== owner_view / owner_claim state machine ===")

# Nobody has claimed anything yet.
ws._OWN.update(page=None, ip=None, since=0.0, n=0)
check("no owner yet, no page id  -> not owner",
      ws.owner_view("")["owner"], False)
check("no owner yet, page A     -> usable (pending, read-only)",
      (ws.owner_view("A")["owner"], ws.owner_view("A").get("pending")), (True, True))
check("owner_view did NOT claim A",
      ws._OWN["page"], None)

ok, took = ws.owner_claim("A", "192.168.3.65")
check("claim A -> ok", ok, True)
check("claim A -> is a takeover", took, True)
check("A is owner", ws.owner_view("A")["owner"], True)
check("B is NOT owner", ws.owner_view("B")["owner"], False)
check("empty page id is NOT owner", ws.owner_view("")["owner"], False)
check("empty page id flagged no_id", ws.owner_view("").get("no_id"), True)

# Same page re-claiming (a refresh) must NOT count as a takeover.
ok, took = ws.owner_claim("A", "192.168.3.65")
check("A re-claims -> ok", ok, True)
check("A re-claims -> NOT a takeover", took, False)

# B takes over from A.
ok, took = ws.owner_claim("B", "192.168.3.70")
check("claim B -> is a takeover", took, True)
check("B is owner now", ws.owner_view("B")["owner"], True)
check("A was revoked", ws.owner_view("A")["owner"], False)
check("takeover counter incremented", ws._OWN["n"] >= 2, True)

# Empty page id can never claim.
ok, took = ws.owner_claim("", "192.168.3.99")
check("claim with empty id refused", ok, False)

print()
print("=== _stop_now must not explode with no motor ===")
try:
    ws._stop_now("unit test")
    check("_stop_now survived", True, True)
    check("STATE forced to stop", ws.STATE["dir"], "stop")
    check("STATE velocity zeroed",
          (ws.STATE["vx"], ws.STATE["vy"], ws.STATE["w"]), (0.0, 0.0, 0.0))
except Exception as e:
    check("_stop_now survived", "raised %r" % e, True)

print()
print("=== mtx_enforce must be a no-op when nobody owns / API is down ===")
ws._OWN.update(page=None, ip=None, since=0.0, n=0)
try:
    ws.mtx_enforce()
    check("mtx_enforce with no owner returned cleanly", True, True)
except Exception as e:
    check("mtx_enforce with no owner returned cleanly", "raised %r" % e, True)

print()
if fails:
    print("FAILURES: %s" % ", ".join(fails))
    sys.exit(1)
print("all owner state-machine tests passed")
