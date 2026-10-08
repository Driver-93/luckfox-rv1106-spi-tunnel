import sys
sys.path.insert(0, "/userdata/car")
import spinet

cases = [
    ("192.168.3.64", True),
    ("192.168.3.65", True),
    ("10.77.0.1", True),
    ("10.0.0.5", True),
    ("172.16.4.9", True),
    ("172.32.9.9", False),
    ("119.28.183.184", False),
    ("8.8.8.8", False),
    ("not.an.ip", False),
]
bad = 0
for ip, want in cases:
    got = spinet.is_lan_addr(ip)
    ok = "OK " if got == want else "FAIL"
    if got != want:
        bad += 1
    print("  %s is_lan_addr(%-16s) = %-5s (want %s)" % (ok, ip, got, want))
print("PEER_REASSERT =", spinet.PEER_REASSERT)
print("failures:", bad)
