"""Self-contained ELKO EP LARA protocol: TCP 61695 control + UDP 61695 discovery.

Byte layouts match the field-proven reverse-engineered library elkoep_lara 0.2.1
(github.com/exKAjFASH, Vyacheslav Anisimov) and ELKO's decompiled Finder/Configurator.
The 1024-byte XOR obfuscation table is embedded (base64). Pure stdlib — the HA base
image has no pip.

Pure protocol (no socket I/O): builders return bytes, parsers take bytes.
"""
from __future__ import annotations

import base64
import random

# 1024-byte XOR mask (identical in the lib and in ELKO's own tools; verified byte-for-byte).
_MASK = base64.b64decode(
    "L9hvnlNm5hF/dsfMEHpBhGzKyjx2u/4K9zI5qkADgkC1TzDELfp1nvZrPvTFN91i8woMD8pKN3LY"
    "ZaUgLDzQfPOHF79KfFdxVStFC30KQ79T1bY19nJ0ahi86sZUVHK6mcEAPiAdGHeymbmeBSv0zSLky1"
    "WFmoWAEqumjJGPHAC3iXC7CgSfNb0+gVy41GTsxhyjbocdNf48fyxzdYvlIn245nu5OcMeRZaXRhgox"
    "zxGucR06qx4mlRxfonXK78yWF/dAO3oMo1XhSnwhe7lE7fuLbvFcDbPP0smR7k6RhnVZey4SsZNXYY3"
    "HGWDhhVli2vR5/0Da+coDr+iwurSHT8QGiABLMjvoM40jVPQTLc2v7tNZ10a0vgFkDsHqpmbY/4lc2z"
    "zY16i4UYuzt2TVsEHawIvhLXg9Ecn+5uM6PU0mClUqthLQVtcqoUX6gMacDl5/j+XbN5JRH0kzGfCFf"
    "dQ+DazP6vouiml0dkvyQlDDIthE+i2nCtKCmZaJ3PzmZWMxvDMZTCNj0MV8ZkEGXp038DaYAIPmFwKa"
    "TREjZO0sDhDogIXdqVa04KloKqRRfDr5UayGTiSfS2Uwvv7SAKuX0Bhv3V3XGkagIx+aPexdyNDWKdN"
    "ApcAsCXUgrKpXouA9UFpL4R6dInbl5erFBQ14krXuO5ZYV2oYQ9Qodm2cCIB/XG1R0eex+MecGn+VJs"
    "Qn55n0KSbJpTjwM10KtGuJSd1qRWhfpoCOO8LOvDyqAxzPwI6Tp58ZNbc0YV5KZwSbktCiamFbiADZJ"
    "e0kOui7xjBAerOdNWH+EQ+w6sViA9T98ulrx5i7NDgNukGx9PqOuFttyixQvh0upBkR6l07duIYJcf/"
    "ED5XhQa7T7rgDBwU4nJp9DloLXRMPSN/aQmbEcOfGY8yjyhclgTl1S6iDlf8/MfqUYKob/gTcxKa2Bu"
    "8oD23QTJM4dNKDR40glhF6e7LQqwBsEAA9mmWn1vVgf3pKPSw6k0lb9r0YESkc61fjYkjj5Wveu99dk"
    "pI9gK3dAtfy1ljlP8hpdKRDRA7XEL79tGSsfHoeW2FczPC3LlYfOw4xGhUO/iJwilC3m3fQdoHjL7wp"
    "IJZN1GrXmFLXolY+JAjwlfaZ/qYyO/IZVs/pbFZ+xA6Z1gpn8zWqF2XPmMgKeWduz8CLSREV9vSUJH2"
    "a7hv8SM5HlE3ueJLA79dAYpMqDs1Yipx144rdc+8F086oDylJw0/psuUZYFc9qlkptYRRahWQMlOPZm"
    "VDagBu2JTECNC99p8zIEvoTwo1SCkhs75KpHmpB8zFylzapljUuoeiaaF4e77lDinJp+NJOh4sEaZCB"
    "CQrGuET2CwiaHnjE4SCku53qSfw=="
)
assert len(_MASK) == 1024, len(_MASK)

# --- Commands / sources (from the library enums) ---
CMD_PLAY = 3
CMD_STOP = 4
CMD_VOLUME = 5
CMD_MUTE = 9
CMD_NEXT = 10
CMD_PREVIOUS = 11
CMD_SOURCE = 12
CMD_SELECT_STATION = 14

SOURCE_RADIO = 1
SOURCE_AUX = 3
SOURCE_DLNA = 4

CONTROL_PORT = 61695
DISCOVERY_PORT = 61695
LARA_DEVICE_ID = 3  # discovery reply DeviceID that identifies a "LARA"

_MAGIC = (0xFF, 0xFA, 0xFA, 0xFF)


# --------------------------------------------------------------------------- crypto
def code(data: bytearray, datalen: int) -> bytearray:
    """Obfuscate data[0:datalen] in place and write the 2-byte start-key at [datalen:datalen+2]."""
    num = random.randrange(700)
    n = num
    for i in range(datalen):
        if n >= 1024:
            n = 0
        data[i] ^= _MASK[n]
        n += 1
    data[datalen] = (num >> 8) & 0xFF
    data[datalen + 1] = num & 0xFF
    return data


def recover_key(cipher: bytes) -> int | None:
    """Recover the XOR start-key from the first 4 magic bytes. Segmentation-proof, unique."""
    if len(cipher) < 4:
        return None
    for k in range(700):
        if all(cipher[i] ^ _MASK[(k + i) % 1024] == _MAGIC[i] for i in range(4)):
            return k
    return None


def decode(cipher: bytes) -> bytearray | None:
    """Decode a full obfuscated packet using a key recovered from the magic header.

    Robust to short/segmented reads and trailing bytes (does not trust the last 2 bytes).
    Returns None if the magic is not recoverable (i.e. not a LARA packet).
    """
    k = recover_key(cipher)
    if k is None:
        return None
    out = bytearray(cipher)
    n = k
    for i in range(len(out)):
        if n >= 1024:
            n = 0
        out[i] ^= _MASK[n]
        n += 1
    return out


# --------------------------------------------------------------------------- builders
def _auth_req(flag: int, len_byte: int, subcmd: int, user: str, passwd: str, total: int) -> bytearray:
    a = bytearray(total)
    a[0], a[1], a[2], a[3] = _MAGIC
    a[4] = random.randrange(255)
    a[5] = flag
    a[6] = len_byte
    a[7] = 0x81
    a[8] = 0xC0
    a[9] = subcmd
    a[10] = 0x11  # 17 = string field width marker
    ub = user.encode("utf8")[:17]
    pb = passwd.encode("utf8")[:17]
    a[11 : 11 + len(ub)] = ub
    a[28 : 28 + len(pb)] = pb
    return a


def build_test_packet() -> bytes:
    """Unauthenticated 11-byte handshake (TCP): reveals firmware/hardware version."""
    a = bytearray(11)
    a[0], a[1], a[2], a[3] = _MAGIC
    a[4] = random.randrange(255)
    a[5] = 7
    a[6] = 9
    a[7] = 0x80
    a[8] = 0
    return bytes(code(a, 9))


def build_discovery_probe(seq: int = 0) -> bytes:
    """UDP broadcast probe ("Search all"): FF FA FA FF <rand> <seq> 09 80 02, obfuscated."""
    a = bytearray(11)
    a[0], a[1], a[2], a[3] = _MAGIC
    a[4] = random.randrange(255)
    a[5] = seq & 0xFF
    a[6] = 9
    a[7] = 0x80
    a[8] = 2  # UDP-broadcast variant (TCP handshake uses 0)
    return bytes(code(a, 9))


def build_remote(user: str, passwd: str, command: int, attribute: int = 0) -> bytes:
    """Remote-control command (play/stop/volume/source/select_station)."""
    a = _auth_req(flag=1, len_byte=47, subcmd=0, user=user, passwd=passwd, total=51)
    a[45] = command
    a[46] = attribute & 0xFF
    return bytes(code(a, 49))


def build_status(user: str, passwd: str) -> bytes:
    a = _auth_req(flag=7, len_byte=49, subcmd=0, user=user, passwd=passwd, total=49)
    return bytes(code(a, 47))


def build_stations(user: str, passwd: str, page: int) -> bytes:
    pagemap = {0: 6, 1: 12, 2: 13, 3: 14}
    a = _auth_req(flag=1, len_byte=45, subcmd=pagemap[page], user=user, passwd=passwd, total=47)
    return bytes(code(a, 45))


# --------------------------------------------------------------------------- parsers
def _win1250(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("windows-1250", "replace").strip()


def parse_test_reply(cipher: bytes):
    """(fw, hw) from a TCP test-packet reply, or None."""
    d = decode(cipher)
    if d is None or len(d) < 15:
        return None
    if d[8] != 1 or d[9] != 0 or d[10] != 3:
        return None
    fw = (d[11] << 16) | (d[12] << 8) | d[13]
    hw = d[14]
    return fw, hw


def parse_discovery_reply(cipher: bytes):
    """Parse a UDP discovery reply into a dict, or None if it is not a LARA.

    Layout verified from ELKO Finder's UDPReceiveMsgTask (device type "LARA", index 3).
    """
    d = decode(cipher)
    if d is None or len(d) < 42:
        return None
    if len(d) != d[6] + 2:  # integrity: body length must match
        return None
    device_id = (d[9] << 8) | d[10]
    if device_id != LARA_DEVICE_ID:
        return None
    fw = (d[11] << 16) | (d[12] << 8) | d[13]
    hw = d[14]
    ip = ".".join(str(b) for b in d[15:19])
    name = _win1250(bytes(d[19:36]))
    mac = ":".join("%02x" % b for b in d[36:42])
    return {"ip": ip, "name": name, "mac": mac, "fw": fw, "hw": hw, "device_id": device_id}


def parse_status_reply(cipher: bytes):
    """Current source/station/volume/playing from a status reply, or None."""
    d = decode(cipher)
    if d is None or len(d) < 16:
        return None
    # d[10] is 0 in the reference lib but 1 on LARA fw 3.7.001 (payload offsets unchanged),
    # so it is intentionally not part of the match.
    if d[7] != 0 or d[8] != 0xC1 or d[9] != 1:
        return None
    return {"source": d[11], "station": d[12], "volume": d[13], "playing": d[15] != 0}


def parse_stations_reply(cipher: bytes):
    """(page, total_count, [names]) for one page of up to 10 presets, or None."""
    d = decode(cipher)
    if d is None or len(d) < 26:
        return None
    if d[7] != 0 or d[8] != 0xC1 or d[9] != 7:  # d[10] varies by fw (0 ref / 1 on 3.7.001)
        return None
    stride = 139
    page = d[11]
    count = d[12]
    names = []
    for st in range(10):
        base = 13 + st * stride
        if base + 13 > len(d):
            break
        names.append(_win1250(bytes(d[base : base + 13])))
    return page, count, names
