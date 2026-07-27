#!/usr/bin/env python3
"""Read-only crawl of the LARA web UI (HTTP Digest) to locate the slim-server / "Audio zone function"
config so you can point the LARA at the HA. No POSTs, no config changes.
   python web_explore.py <lara-ip> [user] [pass]     (defaults admin / elkoep)

The LARA web UI is an SPA ("LARA configurator", gzipped index.html/index.js). The slim server lives
under "Audio zone function": checkbox controll_bit_az (audio_zone_enabled) + IP fields slim_ip_1..4
(audio_zone_ip). Set it there in a browser + Save; do NOT write config over 61695.
"""
import sys, hashlib, re, gzip, urllib.request as U, urllib.error

IP = sys.argv[1] if len(sys.argv) > 1 else "10.0.0.98"
USER = sys.argv[2] if len(sys.argv) > 2 else "admin"
PASS = sys.argv[3] if len(sys.argv) > 3 else "elkoep"
BASE = f"http://{IP}"
def md5(s): return hashlib.md5(s.encode()).hexdigest()

def challenge():
    try:
        U.urlopen(U.Request(BASE + "/"), timeout=6)
    except urllib.error.HTTPError as e:
        m = dict(re.findall(r'(\w+)="?([^",]+)"?', e.headers.get("WWW-Authenticate", "")))
        return m.get("realm", "LARA"), m.get("nonce", "")
    except Exception as e:
        print("no HTTP on :80?", e); sys.exit(1)
    return "LARA", ""
REALM, NONCE = challenge()
print(f"auth: Digest realm={REALM!r} nonce={NONCE!r}")

def get(path):
    ha1 = md5(f"{USER}:{REALM}:{PASS}"); ha2 = md5(f"GET:{path}")
    resp = md5(f"{ha1}:{NONCE}:{ha2}")
    req = U.Request(BASE + path)
    req.add_header("Authorization",
                   f'Digest username="{USER}", realm="{REALM}", nonce="{NONCE}", uri="{path}", response="{resp}"')
    req.add_header("User-Agent", "curl/8")
    try:
        with U.urlopen(req, timeout=8) as r: b = r.read(); st = r.status
    except urllib.error.HTTPError as e: b = e.read() or b""; st = e.code
    if b[:2] == b"\x1f\x8b":
        try: b = gzip.decompress(b)
        except Exception: pass
    return st, b

st, body = get("/")
print(f"GET / -> {st} ({len(body)} B)")
if st == 401:
    print("Digest with %s/**** rejected — wrong web password (it can differ from the 61695 password)." % USER)
    sys.exit(1)
if st != 200:
    print("unexpected status"); sys.exit(1)

text = body.decode("utf-8", "replace")
t = re.search(r"(?is)<title>(.*?)</title>", text)
print("title:", t.group(1).strip() if t else "(none)")
refs = sorted(set(re.findall(r'(?i)(?:src|href|action)\s*=\s*["\']?([^"\'>\s]+)', text)))
js = [r for r in refs if r.lower().endswith(".js")]
blob = text + "".join(get("/" + r.lstrip("/"))[1].decode("utf-8", "replace") for r in js)

print("\n=== slim / audio-zone config found in the web app ===")
for field in ("slim_ip_1", "slim_ip_2", "slim_ip_3", "slim_ip_4", "controll_bit_az",
              "audio_zone_ip", "audio_zone_enabled", "9595", "slim", "lms"):
    hits = [m.start() for m in re.finditer(re.escape(field), blob, re.I)]
    if hits:
        s = max(0, hits[0] - 30); e = min(len(blob), hits[0] + 50)
        print(f"  {field:18} x{len(hits):<3} ~ ...{re.sub(chr(10),' ',blob[s:e])}...")
print("\n-> In a browser: open http://%s, log in, section 'Audio zone function':" % IP)
print("   check 'Activate audio zone function' + set IP = <HA ip>, then Save.")
