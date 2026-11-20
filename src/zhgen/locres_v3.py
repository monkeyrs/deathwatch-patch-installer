#!/usr/bin/env python3
"""Native (version 3) UE4 LocRes reader/writer.

The zh_lex toolchain currently emits a *legacy* LocRes (the byte-for-byte shape
Deathwatch/UE4.6 shipped: no magic GUID, namespaces inline with their strings).
UE 4.27 accepts that (with a "failed the magic number check" warning), but the
engine's own generated locres use version 3:

  magic GUID (4 x u32) | u8 version=3 | i64 LocalizedStringArrayOffset
  u32 EntriesCount | u32 NamespaceCount
  per namespace: (u32 CityHash-of-namespace-name, FString name) u32 KeyCount
    per key: (u32 CityHash-of-key, FString key) u32 SourceStringHash i32 StringIndex
  at StringArrayOffset: TArray<FTextLocalizationResourceString>
    = i32 Num, i32 Max, then Num x (FString, i32 RefCount)

FString convention (same as legacy): i32 length; negative => UTF-16LE whose
count includes the trailing NUL, positive => ANSI/UTF-8 whose count also
includes the NUL.

Key/namespace hashes are UE4's TextKeyUtil::HashString = GetTypeHash(CityHash64
(utf16 bytes)) -- verified against Epic's own files by --selftest.

Usage:
    python locres_v3.py --selftest                 # parse Epic's own Engine locres
    python locres_v3.py --convert <legacy.locres> --out <v3.locres>
    python locres_v3.py --parse <any.locres>       # works for legacy and v3
"""
from __future__ import annotations

import argparse
import pathlib
import struct
import sys

MAGIC = (0x7574140E, 0xFC034A67, 0x9D90154A, 0x1B7F37C3)
MASK64 = (1 << 64) - 1

# Ported from Engine/Source/Runtime/Core/Private/Hash/CityHash.cpp
_K0 = 0xC3A5C85C97CB3127
_K1 = 0xB492B66FBE98F273
_K2 = 0x9AE16A3B2F90404F
_KMUL = 0x9DDFEA08EB382D69


def _bswap64(x: int) -> int:
    """BYTESWAP_ORDER64 -- a real 64-bit byte reversal (UE4 uses the intrinsic)."""
    return int.from_bytes(x.to_bytes(8, "big"), "little") & MASK64


def _rot(x: int, s: int) -> int:
    if s == 0:
        return x & MASK64
    return ((x >> s) | (x << (64 - s))) & MASK64


def _shiftmix(v: int) -> int:
    return (v ^ (v >> 47)) & MASK64


def _city128to64(u: int, v: int) -> int:
    a = ((u ^ v) * _KMUL) & MASK64
    a ^= a >> 47
    b = ((v ^ a) * _KMUL) & MASK64
    b ^= b >> 47
    return (b * _KMUL) & MASK64


def _hashlen16(u: int, v: int, mul: int) -> int:
    a = ((u ^ v) * mul) & MASK64
    a ^= a >> 47
    b = ((v ^ a) * mul) & MASK64
    b ^= b >> 47
    return (b * mul) & MASK64


def _f64(data: bytes, pos: int) -> int:
    return int.from_bytes(data[pos:pos + 8], "little")


def _f32(data: bytes, pos: int) -> int:
    return int.from_bytes(data[pos:pos + 4], "little")


def _hashlen0to16(s: bytes, n: int) -> int:
    if n >= 8:
        mul = (_K2 + n * 2) & MASK64
        a = (_f64(s, 0) + _K2) & MASK64
        b = _f64(s, n - 8)
        c = (_rot(b, 37) * mul + a) & MASK64
        d = ((_rot(a, 25) + b) * mul) & MASK64
        return _hashlen16(c, d, mul)
    if n >= 4:
        mul = (_K2 + n * 2) & MASK64
        a = _f32(s, 0)
        return _hashlen16((n + (a << 3)) & MASK64, _f32(s, n - 4), mul)
    if n > 0:
        a, b, c = s[0], s[n >> 1], s[n - 1]
        y = a + (b << 8)
        z = n + (c << 2)
        return (_shiftmix((y * _K2 ^ z * _K0) & MASK64) * _K2) & MASK64
    return _K2


def _hashlen17to32(s: bytes, n: int) -> int:
    mul = (_K2 + n * 2) & MASK64
    a = (_f64(s, 0) * _K1) & MASK64
    b = _f64(s, 8)
    c = (_f64(s, n - 8) * mul) & MASK64
    d = (_f64(s, n - 16) * _K2) & MASK64
    return _hashlen16(((_rot(a + b, 43) + _rot(c, 30) + d) & MASK64),
                      (a + _rot((b + _K2) & MASK64, 18) + c) & MASK64, mul)


def _hashlen33to64(s: bytes, n: int) -> int:
    mul = (_K2 + n * 2) & MASK64
    a = (_f64(s, 0) * _K2) & MASK64
    b = _f64(s, 8)
    c = _f64(s, n - 24)
    d = _f64(s, n - 32)
    e = (_f64(s, 16) * _K2) & MASK64
    f = (_f64(s, 24) * 9) & MASK64
    g = _f64(s, n - 8)
    h = (_f64(s, n - 16) * mul) & MASK64
    u = (_rot((a + g) & MASK64, 43) + ((_rot(b, 30) + c) & MASK64) * 9) & MASK64
    v = (((a + g) ^ d) + f + 1) & MASK64
    w = (_bswap64(((u + v) & MASK64) * mul & MASK64) + h) & MASK64
    x = (_rot((e + f) & MASK64, 42) + c) & MASK64
    y = ((_bswap64(((v + w) & MASK64) * mul & MASK64) + g) * mul) & MASK64
    z = (e + f + c) & MASK64
    a = (_bswap64((((x + z) & MASK64) * mul + y) & MASK64) + b) & MASK64
    b = (_shiftmix((((z + a) * mul + d + h) & MASK64)) * mul) & MASK64
    return (b + x) & MASK64


def _weak32(s: bytes, a: int, b: int) -> tuple[int, int]:
    w = _f64(s, 0)
    x = _f64(s, 8)
    y = _f64(s, 16)
    z = _f64(s, 24)
    a = (a + w) & MASK64
    b = _rot((b + a + z) & MASK64, 21)
    c = a
    a = (a + x + y) & MASK64
    b = (b + _rot(a, 44)) & MASK64
    return (a + z) & MASK64, (b + c) & MASK64


def cityhash64(s: bytes) -> int:
    n = len(s)
    if n <= 32:
        return _hashlen0to16(s, n) if n <= 16 else _hashlen17to32(s, n)
    if n <= 64:
        return _hashlen33to64(s, n)

    x = _f64(s, n - 40)
    y = (_f64(s, n - 16) + _f64(s, n - 56)) & MASK64
    z = _city128to64((_f64(s, n - 48) + n) & MASK64, _f64(s, n - 24))
    v = _weak32(s[n - 64:], n & MASK64, z)
    w = _weak32(s[n - 32:], (y + _K1) & MASK64, x)
    x = (x * _K1 + _f64(s, 0)) & MASK64

    remaining = (n - 1) & ~63
    pos = 0
    while True:
        x = (_rot((x + y + v[0] + _f64(s, pos + 8)) & MASK64, 37) * _K1) & MASK64
        y = (_rot((y + v[1] + _f64(s, pos + 48)) & MASK64, 42) * _K1) & MASK64
        x ^= w[1]
        y = (y + v[0] + _f64(s, pos + 40)) & MASK64
        z = (_rot((z + w[0]) & MASK64, 33) * _K1) & MASK64
        v = _weak32(s[pos:], (v[1] * _K1) & MASK64, (x + w[0]) & MASK64)
        w = _weak32(s[pos + 32:], (z + w[1]) & MASK64, (y + _f64(s, pos + 16)) & MASK64)
        z, x = x, z
        pos += 64
        remaining -= 64
        if remaining == 0:
            break

    return _city128to64(
        ((_city128to64(v[0], w[0]) + _shiftmix(y) * _K1 + z) & MASK64),
        (_city128to64(v[1], w[1]) + x) & MASK64,
    )


def ue_text_key_hash(s: str) -> int:
    """TextKeyUtil::HashString = GetTypeHash(CityHash64(utf16 bytes))
    with GetTypeHash(uint64) = lo + hi*23."""
    h = cityhash64(s.encode("utf-16-le"))
    return ((h & 0xFFFFFFFF) + ((h >> 32) * 23)) & 0xFFFFFFFF


def read_i32(data: bytes, pos: int) -> int:
    return struct.unpack("<i", data[pos:pos + 4])[0]


def read_u32(data: bytes, pos: int) -> int:
    return struct.unpack("<I", data[pos:pos + 4])[0]


def read_fstring(data: bytes, pos: int) -> tuple[str, int]:
    """Returns (value, new_pos). UE4 FString: signed int32 length incl. NUL."""
    n = read_i32(data, pos)
    pos += 4
    if n < 0:
        count = -n
        raw = data[pos:pos + count * 2]
        pos += count * 2
        text = raw.decode("utf-16-le")
        return text.rstrip("\x00"), pos
    raw = data[pos:pos + n]
    pos += n
    return raw.decode("utf-8", errors="replace").rstrip("\x00"), pos


def write_fstring(value: str) -> bytes:
    if all(ord(c) < 128 for c in value):
        raw = value.encode("ascii") + b"\x00"
        return struct.pack("<i", len(raw)) + raw
    raw = value.encode("utf-16-le") + b"\x00\x00"
    return struct.pack("<i", -len(raw) // 2) + raw


class LocRes:
    def __init__(self):
        self.entries: list[tuple[str, str, int, str]] = []  # namespace, key, sourcehash, value

    def add(self, namespace: str, key: str, source_hash: int, value: str) -> None:
        self.entries.append((namespace, key, source_hash, value))

    def counts(self) -> dict:
        ns: dict[str, int] = {}
        for namespace, _, _, _ in self.entries:
            ns[namespace] = ns.get(namespace, 0) + 1
        return {"entries": len(self.entries), "namespaces": ns}


def parse_any(path: pathlib.Path) -> LocRes:
    data = path.read_bytes()
    res = LocRes()
    magic = struct.unpack("<4I", data[:16])
    if magic == MAGIC:
        version = data[16]
        pos = 17
        str_array_offset = struct.unpack("<q", data[pos:pos + 8])[0]
        pos += 8
        if version >= 2:
            entries_count = read_u32(data, pos) if version >= 2 else None
            pos += 4
        strings = string_array_at(data, str_array_offset) if str_array_offset != -1 else []
        namespace_count = read_u32(data, pos)
        pos += 4
        for _ in range(namespace_count):
            if version >= 3:
                _hash = read_u32(data, pos)
                pos += 4
            elif version == 2:
                pos += 4
            namespace, pos = read_fstring(data, pos)
            key_count = read_u32(data, pos)
            pos += 4
            for _ in range(key_count):
                if version >= 3:
                    _hash = read_u32(data, pos)
                    pos += 4
                elif version == 2:
                    pos += 4
                key, pos = read_fstring(data, pos)
                source_hash = read_u32(data, pos)
                pos += 4
                if version >= 1:
                    idx = read_i32(data, pos)
                    pos += 4
                    value = strings[idx] if idx >= 0 else ""
                else:
                    value, pos = read_fstring(data, pos)
                res.add(namespace, key, source_hash, value)
        return res

    # legacy branch: namespace count, names inline
    pos = 0
    namespace_count = read_u32(data, pos)
    pos += 4
    for _ in range(namespace_count):
        namespace, pos = read_fstring(data, pos)
        key_count = read_u32(data, pos)
        pos += 4
        for _ in range(key_count):
            key, pos = read_fstring(data, pos)
            source_hash = read_u32(data, pos)
            pos += 4
            value, pos = read_fstring(data, pos)
            res.add(namespace, key, source_hash, value)
    return res


def string_array_at(data: bytes, offset: int) -> list[str]:
    """UE4 writes the array as i32 Num followed by Num (FString, i32 RefCount)
    records -- no capacity field."""
    num = read_i32(data, offset)
    pos = offset + 4
    out = []
    for _ in range(num):
        value, pos = read_fstring(data, pos)
        _refcount = read_i32(data, pos)
        pos += 4
        out.append(value)
    return out


def parse_v3(path: pathlib.Path) -> LocRes:
    data = path.read_bytes()
    magic = struct.unpack("<4I", data[:16])
    if magic != MAGIC:
        raise ValueError(f"{path}: not a v3/v2 magic file (magic={magic})")
    version = data[16]
    pos = 17
    str_array_offset = struct.unpack("<q", data[pos:pos + 8])[0]
    pos += 8
    entries_count = read_u32(data, pos) if version >= 2 else None
    if version >= 2:
        pos += 4
    namespace_count = read_u32(data, pos)
    pos += 4

    strings = string_array_at(data, str_array_offset) if str_array_offset != -1 else []
    res = LocRes()
    hash_ok = 0
    hash_bad = 0
    for _ in range(namespace_count):
        ns_hash = read_u32(data, pos)
        pos += 4
        namespace, pos = read_fstring(data, pos)
        if namespace:
            if ue_text_key_hash(namespace) == ns_hash:
                hash_ok += 1
            else:
                hash_bad += 1
        key_count = read_u32(data, pos)
        pos += 4
        for _ in range(key_count):
            key_hash = read_u32(data, pos)
            pos += 4
            key, pos = read_fstring(data, pos)
            if ue_text_key_hash(key) == key_hash:
                hash_ok += 1
            else:
                hash_bad += 1
            source_hash = read_u32(data, pos)
            pos += 4
            idx = read_i32(data, pos)
            pos += 4
            res.add(namespace, key, source_hash, strings[idx] if idx >= 0 else "")
    res.counts_extra = {"version": version, "entries_declared": entries_count,
                        "string_array_len": len(strings), "hash_ok": hash_ok, "hash_bad": hash_bad}
    return res


def write_v3(entries: list[tuple[str, str, int, str]], out_path: pathlib.Path) -> dict:
    """Write a version-3 LocRes matching SaveToArchive's layout."""
    out = bytearray()
    out += struct.pack("<4I", *MAGIC)
    out.append(3)
    offset_placeholder_pos = len(out)
    out += struct.pack("<q", -1)

    # namespace -> {key: (sourcehash, value)} preserving insertion order
    namespaces: dict[str, dict[str, tuple[int, str]]] = {}
    for namespace, key, source_hash, value in entries:
        namespaces.setdefault(namespace, {})[key] = (source_hash, value)

    out += struct.pack("<I", len(entries))
    out += struct.pack("<I", len(namespaces))

    string_index: dict[str, int] = {}
    strings: list[str] = []
    refcounts: list[int] = []

    def get_index(value: str) -> int:
        if value in string_index:
            refcounts[string_index[value]] += 1
            return string_index[value]
        idx = len(strings)
        strings.append(value)
        refcounts.append(1)
        string_index[value] = idx
        return idx

    for namespace, keys in namespaces.items():
        out += struct.pack("<I", ue_text_key_hash(namespace) if namespace else 0)
        out += write_fstring(namespace)
        out += struct.pack("<I", len(keys))
        for key, (source_hash, value) in keys.items():
            out += struct.pack("<I", ue_text_key_hash(key))
            out += write_fstring(key)
            out += struct.pack("<I", source_hash & 0xFFFFFFFF)
            out += struct.pack("<i", get_index(value))

    str_array_offset = len(out)
    struct.pack_into("<q", out, offset_placeholder_pos, str_array_offset)

    out += struct.pack("<i", len(strings))
    for value, refcount in zip(strings, refcounts):
        out += write_fstring(value)
        out += struct.pack("<i", refcount)

    out_path.write_bytes(bytes(out))
    return {"bytes": len(out), "entries": len(entries), "namespaces": len(namespaces),
            "unique_strings": len(strings)}


def write_legacy(entries: list[tuple[str, str, int, str]], out_path: pathlib.Path) -> dict:
    """Write a LEGACY LocRes (the byte shape Deathwatch 4.6 / UE4.16 ships and
    reads). Layout mirror of parse_any's legacy branch:

      u32 NamespaceCount
      per namespace:
        FString NamespaceName
        u32 KeyCount
        per key:
          FString Key
          u32 SourceStringHash
          FString Value

    No magic GUID, no shared string array -- values are inline. This is exactly
    what the 4.6 game's loader expects; write_v3's UE4.27 format is NOT read by it.
    """
    # preserve first-appearance order of namespaces
    order: list[str] = []
    groups: dict[str, list[tuple[str, int, str]]] = {}
    for namespace, key, source_hash, value in entries:
        if namespace not in groups:
            groups[namespace] = []
            order.append(namespace)
        groups[namespace].append((key, source_hash & 0xFFFFFFFF, value))

    out = bytearray()
    out += struct.pack("<I", len(order))
    for ns in order:
        out += write_fstring(ns)
        out += struct.pack("<I", len(groups[ns]))
        for key, source_hash, value in groups[ns]:
            out += write_fstring(key)
            out += struct.pack("<I", source_hash)
            out += write_fstring(value)

    out_path.write_bytes(bytes(out))
    return {"bytes": len(out), "entries": len(entries), "namespaces": len(order)}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="UE4 native LocRes (v3) tool")
    ap.add_argument("--selftest", action="store_true", help="parse Epic's own locres")
    ap.add_argument("--parse", help="parse any locres file (legacy or v3) and print counts")
    ap.add_argument("--convert", help="legacy locres to convert")
    ap.add_argument("--out", help="output v3 locres path")
    args = ap.parse_args()

    engine_loc = pathlib.Path(r"C:\Program Files\Epic Games\UE_4.27\Engine\Content\Localization\Engine")

    if args.selftest:
        for culture in ("zh-Hans", "en"):
            p = engine_loc / culture / "Engine.locres"
            if not p.exists():
                print(f"[skip] {p}")
                continue
            res = parse_v3(p)
            extra = res.counts_extra
            counts = res.counts()
            print(f"{culture}: version={extra['version']} declared_entries={extra['entries_declared']} "
                  f"parsed={counts['entries']} namespaces={counts['namespaces']} "
                  f"string_array={extra['string_array_len']} hash_ok={extra['hash_ok']} hash_bad={extra['hash_bad']}")
        return 0

    if args.parse:
        p = pathlib.Path(args.parse)
        res = parse_any(p)
        print(f"{p}: {res.counts()}")
        return 0

    if args.convert:
        legacy = parse_any(pathlib.Path(args.convert))
        out = pathlib.Path(args.out)
        info = write_v3(legacy.entries, out)
        print(f"wrote {out} -> {info}")
        check = parse_v3(out)
        c = check.counts()
        print(f"self-check parse: entries={c['entries']} namespaces={len(c['namespaces'])} "
            f"hash_ok={check.counts_extra['hash_ok']} hash_bad={check.counts_extra['hash_bad']}")
        same = sum(1 for a, b in zip(legacy.entries, check.entries) if a == b)
        print(f"value-exact round trip: {same}/{len(legacy.entries)} entries identical")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
