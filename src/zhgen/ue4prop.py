#!/usr/bin/env python3
"""UE4 cooked tagged-property reader for this build (FileVersion 413 = UE4.6, unversioned).

Layout used here was established byte-exactly from packages of THIS build and
cross-checked against MinshuG/pyUE4Parse's FPropertyTag field order (MIT).  See
reports/layout_notes.md and archive/mainline/.workbuddy/memory/2026-10-30.md.

  tag header      : Name FName(8) Type FName(8) Size int32(4) Flags uint32(4) = 24 B
  stream terminator: FName pointing at the name-table entry literally "None" (number 0)
                    followed by int32 0                                        = 12 B
  StructProperty  : base tag, then StructName FName(8), then Size bytes of content
  ArrayProperty   : base tag, then InnerType FName(8), then [int32 element count + elements]
  BoolProperty    : value inside the Flags word when Size == 0 (bit 31)
  SoftObjectProperty: FString AssetPath
  Map/Set/Text    : unmodelled for 413 -> kept as raw hex (auditable)

Table metadata (NameCount/ExportCount/ImportCount, per-export SerialOffset/SerialSize)
comes from UModel build 1590 `-list`, which parses every package in this build; the
property stream itself is what UModel cannot export for gameplay classes.
"""
from __future__ import annotations

import json
import pathlib
import re
import struct
import subprocess
import sys

WS = pathlib.Path(r"D:/3DS-DW")
GAME = WS / "refs/PC_EE/deathwatchgame/Content"
UM = WS / "tools/umodel/umodel_64.exe"

IMPORT_REC = 28      # 7 x int32, verified
EXPORT_REC = 68      # 17 x int32; proven by (DependsOffset-ExportOffset)/ExportCount==68
                     # (the older "76" figure wrongly included the depends/SAR tail)
BOOL_TAG_TRUE = 0x80000000
KEY_RE_32 = re.compile(r"^[0-9A-F]{32}$")


def parse_ftext(blob: bytes, strict: bool = True):
    """FText layout for THIS build, empirically validated on 553 TextProperty samples
    (all conformed once UTF-16 source strings are handled) - see dump_textprops.py:

        uint32 flags (=0) | uint32 source_flags (=0) | uint8 history_type (=0, ENone)
        FString localization key   (32 hex chars, culture-invariant)
        FString source string      (English display text; UTF-16LE when length is negative)

    Returns (fields, consumed_bytes, ok, why).  Keys are kept so the .locres table can be
    applied later (keys are exactly what Game.locres overrides).

    strict=True  -> the whole blob must be consumed by the FText (single-value property).
    strict=False -> partial consumption is allowed and reported as ok; the caller is
                    responsible for the boundary (used for TArray<FText> elements).
    """
    if len(blob) < 9:
        return dict(raw=blob.hex()), len(blob), False, "FText region shorter than 9"
    flags, src_flags = struct.unpack_from("<2I", blob, 0)
    htype = blob[8]
    if htype != 0:
        return dict(raw=blob.hex()), len(blob), False, f"unsupported history_type {htype}"
    key, p1 = read_fstring(blob, 9)
    if key is None:
        return dict(raw=blob.hex()), len(blob), False, "FText key FString unreadable"
    src, p2 = read_fstring(blob, p1)
    if src is None:
        return dict(raw=blob.hex()), len(blob), False, "FText source FString unreadable"
    out = dict(loc_key=key, source=src, flags=flags, source_flags=src_flags,
               history_type=htype)
    if not KEY_RE_32.match(key):
        out["key_shape"] = "not-32-hex"          # still decoded, flagged for review
    if p2 != len(blob):
        out["tail_hex"] = blob[p2:p2 + 32].hex()
        if not strict:
            return out, p2, True, None
        return out, p2, False, f"FText consumed {p2} of {len(blob)}"
    return out, p2, True, None


class ParseError(Exception):
    pass


# ------------------------------------------------------------------ tables
def umodel_list(rel_noext: str):
    # The packaged windowed installer must not let UModel open a console; if the tool is missing, return None directly,
    # so read_package keeps the self-table result instead of launching a visible subprocess.
    if not pathlib.Path(UM).is_file():
        return None
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run([str(UM), "-game=ue4.6", "-list", f"-path={GAME}", rel_noext],
                          capture_output=True, text=True, errors="replace",
                          creationflags=flags)
    txt = proc.stdout or ""
    m = re.search(r"Names: (\d+) Exports: (\d+) Imports: (\d+)", txt)
    if not m:
        return None
    exports = []
    for line in txt.splitlines():
        mm = re.match(r"^\s+(\d+)\s+([0-9A-Fa-f]{3,8})\s+([0-9A-Fa-f]{1,8})\s+(\S+)\s+(\S+)\s*$", line)
        if mm:
            exports.append(dict(index=int(mm.group(1)), off=int(mm.group(2), 16),
                                size=int(mm.group(3), 16), cls=mm.group(4),
                                name=mm.group(5)))
    return dict(name_count=int(m.group(1)), export_count=int(m.group(2)),
                import_count=int(m.group(3)), exports=exports)


def read_fstring(d: bytes, pos: int):
    """FString: int32 length (negative => UTF-16LE with NUL).  ANSI branch may hold
    UTF-8 (this build ships a 2026 zh patch, so cooked name tables contain multi-byte
    text); decode permissively with surrogateescape so the name-table chain never
    breaks on non-ASCII bytes."""
    if pos + 4 > len(d):
        return None, pos
    n = struct.unpack_from("<i", d, pos)[0]
    if n == 0:
        return "", pos + 4
    if 0 < n <= 8192:
        if pos + 4 + n > len(d) or d[pos + 4 + n - 1] != 0:
            return None, pos
        body = d[pos + 4:pos + 4 + n - 1]
        try:
            s = body.decode("utf-8")
        except UnicodeDecodeError:
            s = body.decode("latin1")
        return s, pos + 4 + n
    if -8192 <= n < 0:
        cnt = -n
        if pos + 4 + 2 * cnt > len(d) or d[pos + 4 + 2 * cnt - 2:pos + 4 + 2 * cnt] != b"\x00\x00":
            return None, pos
        try:
            return d[pos + 4:pos + 4 + 2 * cnt - 2].decode("utf-16-le"), pos + 4 + 2 * cnt
        except UnicodeDecodeError:
            return None, pos
    return None, pos


def read_name_table(d: bytes, off: int, count: int):
    names, pos = [], off
    for _ in range(count):
        s, npos = read_fstring(d, pos)
        if s is None:
            return None, off
        names.append(s)
        pos = npos
    return names, pos


def decode_imports(d: bytes, off: int, count: int, names):
    """UE4.6 cooked import record (28 B = 7 int32), order established from this build's
    bytes (STAT_BroodLord @351: 1,0,3,0,-2,9,0):
        ClassPackage FName pair (index, number)
        ClassName    FName pair (index, number)
        PackageIndex int32  (FPackageIndex: -N-1 -> import index N-1, >0 -> export)
        ObjectName   FName pair (index, number)
    Cross-checks: STAT_BroodLord import0 = (Class=Class, pkgidx=-2 -> import1,
    ObjectName=StatSheet) and import1 = (Class=Package, pkgidx=0, ObjectName
    =/Script/DeathwatchGame) - i.e. the class import and the native script package."""
    recs = []
    for i in range(count):
        base = off + i * IMPORT_REC
        if base + IMPORT_REC > len(d):
            return None
        cpkg_i, cpkg_n, cls_i, cls_n, pkg_idx, obj_i, obj_n = struct.unpack_from("<7i", d, base)
        if not (0 <= cpkg_i < len(names) and 0 <= cls_i < len(names) and 0 <= obj_i < len(names)):
            return None
        if cpkg_n > 0xFFFF or cls_n > 0xFFFF or obj_n > 0xFFFF:
            return None
        recs.append(dict(class_package=(names[cpkg_i] if not cpkg_n else f"{names[cpkg_i]}_{cpkg_n}"),
                         class_name=(names[cls_i] if not cls_n else f"{names[cls_i]}_{cls_n}"),
                         package_index=pkg_idx,
                         object_name=(names[obj_i] if not obj_n else f"{names[obj_i]}_{obj_n}")))
    return recs


def parse_summary(d: bytes):
    """FPackageFileSummary for this build's generation (FileVersion 413 = UE4.6,
    LegacyVersion = -3, unversioned).  Field order follows UModel's
    FPackageFileSummary::Serialize4 with the 413-era version gates:
      Tag, LegacyVersion, VersionUE3, Version, LicenseeVersion,
      CustomVersionContainer (present because LegacyVersion <= -2; count==0 here),
      HeadersSize, PackageGroup(FString), PackageFlags,
      NameCount/NameOffset, ExportCount/ExportOffset, ImportCount/ImportOffset,
      DependsOffset, StringAssetReferencesCount/Offset (413 >= 384),
      ThumbnailTableOffset, Guid(16), GenerationCount + FGenerationInfo(8 B each),
      FEngineVersion (413 >= 336), CompressionFlags, CompressedChunks, PackageSource,
      AdditionalPackagesToCook, NumTextureAllocations (413 > -7).
    LocalizationId (516) / GatherableTextData (459) / SearchableNames (510) /
    CompatibleVersion (444) / PersistentGuid are absent at 413, and these cooked
    packages carry no AssetRegistry/BulkData tail fields - validated byte-exactly:
    the header ends exactly at NameOffset for every package tested.
    HeadersSize doubles as BulkDataStartOffset (= first export SerialOffset)."""
    p = 0
    def u32():
        nonlocal p
        v = struct.unpack_from("<I", d, p)[0]; p += 4
        return v
    def i32():
        nonlocal p
        v = struct.unpack_from("<i", d, p)[0]; p += 4
        return v
    def fstr():
        nonlocal p
        n = struct.unpack_from("<i", d, p)[0]
        if n == 0:
            p += 4
            return ""
        if n > 0:
            s = d[p + 4:p + 4 + n - 1].decode("utf-8", "replace")
            p += 4 + n
            return s
        cnt = -n
        s = d[p + 4:p + 4 + 2 * cnt - 2].decode("utf-16-le", "replace")
        p += 4 + 2 * cnt
        return s

    tag = u32()
    if tag != 0x9E2A83C1:
        raise ParseError(f"bad package tag {tag:#08x}")
    legacy = i32()
    if not (-7 <= legacy <= -1):
        raise ParseError(f"unsupported LegacyVersion {legacy}")
    if legacy != -4:
        i32()                       # VersionUE3
    ver = u32(); lic = u32()        # unversioned => 0/0 (or 413 if versioned)
    if legacy <= -2:
        n = i32()
        p += n * 20                 # FGuid(16) + int32 version
    headers_size = u32()
    pkg_group = fstr()
    pkg_flags = u32()
    out = dict(tag=tag, legacy=legacy, version=ver, licensee=lic,
               headers_size=headers_size, package_group=pkg_group, package_flags=pkg_flags)
    out["name_count"] = u32(); out["name_offset"] = u32()
    out["export_count"] = u32(); out["export_offset"] = u32()
    out["import_count"] = u32(); out["import_offset"] = u32()
    out["depends_offset"] = u32()
    if 413 >= 384:
        out["sar_count"] = u32(); out["sar_offset"] = u32()
    out["thumbnail_offset"] = i32()
    out["guid"] = d[p:p + 16].hex(); p += 16
    gc = i32()
    gens = []
    for _ in range(gc):
        o = i32(); s = i32(); gens.append((o, s))
    out["generations"] = gens
    if 413 >= 336:
        # FEngineVersion: uint16 major/minor/patch + FString branch + uint32 changelist
        p += 6
        branch = fstr()
        out["engine_branch"] = branch
        p += 4                      # changelist
    out["compression_flags"] = u32()
    nchunks = u32()
    out["compressed_chunks"] = nchunks
    if nchunks:
        raise ParseError("compressed package: FCompressedChunk array present")
    out["package_source"] = u32()
    napc = u32()
    for _ in range(napc):
        fstr()
    out["num_texture_allocations"] = u32()
    out["header_end"] = p
    return out


def decode_exports(d: bytes, off: int, count: int, rec: int, names, imports):
    """413-era cooked export record = 17 int32 = 68 B (established from this build:
    (DependsOffset - ExportOffset) / ExportCount == 68, and SerialSize/SerialOffset land
    on the values UModel reports).  Field order per UModel FObjectExport::Serialize4:
      ClassIndex(FPkgIdx), SuperIndex(FPkgIdx), PackageIndex(FPkgIdx),
      ObjectName FName pair, ObjectFlags, SerialSize, SerialOffset,
      bForcedExport, bNotForClient, bNotForServer (each a 4-byte word here),
      Guid(4 words), PackageFlags, bNotForEditorGame.
    No TemplateIndex (508), no 64-bit sizes (511), no preload deps (507), no bIsAsset(485)."""
    if rec != 68:
        return None
    recs = []
    for i in range(count):
        base = off + i * rec
        if base + rec > len(d):
            return None
        w = struct.unpack_from("<17i", d, base)
        class_index, super_index, package_index = w[0], w[1], w[2]
        obj_i, obj_n = w[3], w[4]
        serial_size, serial_offset = w[6], w[7]
        if not (0 <= obj_i < len(names)) or obj_n < 0 or obj_n > 0xFFFF:
            return None
        if serial_size < 0 or serial_size > len(d) or serial_offset < 0 or serial_offset > len(d):
            return None
        recs.append(dict(class_index=class_index, super_index=super_index,
                         package_index=package_index,
                         object_name=(names[obj_i] if not obj_n else f"{names[obj_i]}_{obj_n}"),
                         object_flags=w[5], serial_size=serial_size, serial_offset=serial_offset,
                         package_flags=w[12 + 3], guid=d[base + 44:base + 60].hex()))
    return recs


def resolve_ref(idx, names, exports, imports):
    if idx == 0:
        return "NULL"
    if idx > 0:
        e = idx - 1
        if 0 <= e < len(exports):
            return exports[e]["object_name"]
        return f"export?{idx}"
    i = -idx - 1
    if 0 <= i < len(imports):
        return imports[i]["object_name"]
    return f"import?{idx}"


def tables_from_file(d: bytes, rel: str):
    """Authoritative tables straight from the package: summary offsets, name chain,
    import table, export table.  Validated by chain-end == ImportOffset and by integer
    record sizes.  Returns dict or raises ParseError."""
    s = parse_summary(d)
    nc, ic, ec = s["name_count"], s["import_count"], s["export_count"]
    no, io, eo, dep = s["name_offset"], s["import_offset"], s["export_offset"], s["depends_offset"]
    if not (0 < no < len(d)):
        raise ParseError(f"bad NameOffset {no}")
    names, nend = read_name_table(d, no, nc)
    if names is None:
        raise ParseError("name table chain failed")
    if nend != io:
        raise ParseError(f"name table ends {nend} != ImportOffset {io}")
    if ic:
        isz = (eo - io) / ic
    else:
        isz = 0
    if isz != 28:
        raise ParseError(f"import record size {isz} != 28")
    imps = decode_imports(d, io, ic, names) if ic else []
    if imps is None:
        raise ParseError("import table decode failed")
    if ec:
        esz = (dep - eo) / ec
        if esz != int(esz) or not (56 <= esz <= 96):
            raise ParseError(f"implausible export record size {esz}")
        exps = decode_exports(d, eo, ec, int(esz), names, imps)
        if exps is None:
            raise ParseError("export table decode failed")
    else:
        exps = []
        esz = 0
    return s, names, imps, exps, int(esz)


def calibrate(d: bytes, bulk: int, tables: dict, rel: str):
    """Locate the name table decisively.

    Strategy: scan candidate starts, decode the FString chain for NameCount names, and
    require the strongest available evidence:
      1. names[0] must be the package's own cooked path ("/Game/" + rel), and
      2. "None" must be present in the table (property-stream terminator), and
      3. the import records following the table must decode as FName triples whose
         indices are inside the name table, and
      4. the remaining gap (bulk - nend - imports) must be a plausible export-table size.
    UModel supplies bulk (first export SerialOffset) and per-export SerialSize, so we
    never have to guess export record width to find the stream.
    """
    nc, ic, ec = tables["name_count"], tables["import_count"], tables["export_count"]
    want = "/Game/" + rel
    out = []
    for no in range(0, bulk):
        names, nend = read_name_table(d, no, nc)
        if names is None or nend > bulk:
            continue
        if names[0] != want:
            continue
        if "None" not in names:
            continue
        imps = decode_imports(d, nend, ic, names) if ic else []
        if ic and imps is None:
            continue
        gap2 = bulk - nend - ic * IMPORT_REC
        if ec:
            if gap2 <= 0 or gap2 % ec:
                continue
            rec = gap2 // ec
            if not (52 <= rec <= 128):
                continue
        elif gap2:
            continue
        out.append((no, names, imps, gap2))
    return out


# ------------------------------------------------------------------ codecs
SCALAR = {
    "IntProperty": ("<i", 4), "Int64Property": ("<q", 8),
    "Int16Property": ("<h", 2), "Int8Property": ("<b", 1),
    "UIntProperty": ("<I", 4), "UInt32Property": ("<I", 4),
    "UInt64Property": ("<Q", 8), "UInt16Property": ("<H", 2),
    "UInt8Property": ("<B", 1), "FloatProperty": ("<f", 4),
    "DoubleProperty": ("<d", 8),
}
REF_TYPES = {"ObjectProperty", "ClassProperty", "WeakObjectProperty",
             "InterfaceProperty", "WeakInterfaceProperty"}
POD_STRUCTS = {
    "Vector": ("<3f", 12), "Vector2D": ("<2f", 8), "Vector4": ("<4f", 16),
    "Rotator": ("<4i", 16), "Quat": ("<4f", 16), "Quaternion": ("<4f", 16),
    "IntPoint": ("<2i", 8), "IntVector": ("<3i", 12), "IntRect": ("<4i", 16),
    "LinearColor": ("<4f", 16), "Color": ("<4B", 4), "Box": ("<7f", 28),
    "Box2D": ("<4f", 16), "Plane": ("<4f", 16), "Timespan": ("<q", 8),
    "DateTime": ("<q", 8), "Guid": ("<4I", 16), "Guid4": ("<4I", 16),
    "TimerHandle": ("<Q", 8), "Transform": ("<10f", 40),
    "ScaleVector": ("<3f", 12),
}
POD_NAMES = {
    "Vector": ("x", "y", "z"), "Vector2D": ("x", "y"), "Vector4": ("x", "y", "z", "w"),
    "IntPoint": ("x", "y"), "IntVector": ("x", "y", "z"),
    "IntRect": ("min_x", "min_y", "max_x", "max_y"), "Box2D": ("min_x", "min_y", "max_x", "max_y"),
    "LinearColor": ("r", "g", "b", "a"), "Color": ("r", "g", "b", "a"),
    "Plane": ("x", "y", "z", "w"), "Quat": ("x", "y", "z", "w"),
    "Quaternion": ("x", "y", "z", "w"), "Rotator": ("pitch", "yaw", "roll", "roll_pct"),
    "ScaleVector": ("x", "y", "z"),
}


# ------------------------------------------------------------------ stream reader
class PropStream:
    def __init__(self, d: bytes, names, exports, imports):
        self.d = d
        self.names = names
        self.exports = exports
        self.imports = imports
        self.none_idx = None
        for i, nm in enumerate(names):
            if nm == "None":
                self.none_idx = i
        if self.none_idx is None:
            raise ParseError('name table has no literal "None" entry')

    def fname(self, pos: int):
        idx, num = struct.unpack_from("<ii", self.d, pos)
        if not (0 <= idx < len(self.names)):
            raise ParseError(f"FName index {idx} out of range at {pos}")
        if num < 0 or num > 0xFFFF:
            raise ParseError(f"implausible FName number {num} at {pos}")
        s = self.names[idx]
        return idx, num, (s if not num else f"{s}_{num}")

    def obj_ref(self, idx: int):
        if idx == 0:
            return "NULL"
        if idx > 0:
            e = idx - 1
            if 0 <= e < len(self.exports):
                return f"export:{self.exports[e]['cls']}/{self.exports[e]['name']}"
            return f"export?{idx}"
        i = -idx - 1
        if 0 <= i < len(self.imports):
            ir = self.imports[i]
            return f"import:{ir['class_name']}/{ir['object_name']}"
        return f"import?{idx}"

    # ---------------- tag header (24 B) / terminator (12 B)
    def read_tag(self, pos: int, end: int):
        idx, num = struct.unpack_from("<ii", self.d, pos)
        if idx == self.none_idx and num == 0:
            if pos + 12 > end:
                raise ParseError("terminator overruns region")
            if struct.unpack_from("<i", self.d, pos + 8)[0] != 0:
                raise ParseError("terminator trailing int32 != 0")
            return dict(terminator=True, pos=pos, consumed=12)
        if pos + 24 > end:
            raise ParseError("no room for 24-byte tag header")
        ni, nnum, name = self.fname(pos)
        ti, tnum, tname = self.fname(pos + 8)
        size, flags = struct.unpack_from("<iI", self.d, pos + 16)
        if size < 0 or size > 0x8000000:
            raise ParseError(f"implausible Size {size} at {pos}")
        return dict(terminator=False, pos=pos, consumed=24, name=name, name_idx=ni,
                    name_num=nnum, type=tname, type_idx=ti, type_num=tnum,
                    size=size, flags=flags)

    # ---------------- scalars
    def read_scalar(self, ptype, size, pos, end, flags):
        d = self.d
        if ptype == "BoolProperty":
            if size == 0:
                return bool(flags & BOOL_TAG_TRUE), 0
            if size == 1:
                if pos + 1 > end:
                    raise ParseError("BoolProperty byte overrun")
                return bool(d[pos]), 1
            if size == 4:
                if pos + 4 > end:
                    raise ParseError("BoolProperty u32 overrun")
                return bool(struct.unpack_from("<I", d, pos)[0]), 4
            raise ParseError(f"BoolProperty size {size}")
        if ptype in SCALAR:
            fmt, sz = SCALAR[ptype]
            if size != sz:
                raise ParseError(f"{ptype} Size {size} != {sz}")
            if pos + sz > end:
                raise ParseError(f"{ptype} overrun at {pos}")
            v = struct.unpack_from(fmt, d, pos)[0]
            return (round(v, 6) if isinstance(v, float) else v), sz
        if ptype == "NameProperty":
            if size != 8:
                raise ParseError(f"NameProperty Size {size} != 8")
            _, _, s = self.fname(pos)
            return s, 8
        if ptype == "StrProperty":
            s, npos = read_fstring(d, pos)
            if s is None:
                raise ParseError(f"bad StrProperty FString at {pos}")
            return s, npos - pos
        if ptype in REF_TYPES:
            if size != 4:
                raise ParseError(f"{ptype} Size {size} != 4")
            return self.obj_ref(struct.unpack_from("<i", d, pos)[0]), 4
        if ptype == "ByteProperty":
            if size == 1:
                if pos + 1 > end:
                    raise ParseError("ByteProperty overrun")
                return d[pos], 1
            raise ParseError(f"ByteProperty size {size}")
        raise ParseError(f"unsupported type {ptype}")

    # ---------------- ArrayProperty
    def read_array(self, pos, region_end, size, type_num, depth):
        """pos = just past base tag; Size covers [int32 count + elements]."""
        if size == 0:
            return dict(inner=None, count=0, elements=[]), pos, True, "empty array"
        if type_num != 0:
            return dict(raw=self.d[pos:region_end].hex()), region_end, False, \
                f"Array Type number {type_num}"
        if pos + 12 > region_end:
            return dict(raw=self.d[pos:region_end].hex()), region_end, False, \
            "no room for InnerType FName + count"
        iidx, inum, inner = self.fname(pos)
        pos += 8
        if inum != 0:
            return dict(raw=self.d[pos:region_end].hex()), region_end, False, \
                f"unexpected InnerType number {inum}"
        count = struct.unpack_from("<i", self.d, pos)[0]
        pos += 4
        payload_end = (pos - 4) + size          # size accounts count(4)+elements
        if payload_end > region_end:
            return dict(inner=inner, count=count, raw=self.d[pos:region_end].hex()), \
                region_end, False, "array payload exceeds region"
        if count < 0 or count > 4_000_000:
            return dict(inner=inner, raw=self.d[pos:payload_end].hex()), payload_end, \
                False, f"bad element count {count}"
        avail = payload_end - pos
        if inner in SCALAR:
            fmt, esz = SCALAR[inner]
            if avail < count * esz:
                return dict(inner=inner, count=count, raw=self.d[pos:payload_end].hex()), \
                    payload_end, False, f"not enough bytes for {count} x {esz}"
            vals = (list(struct.unpack_from("<%d%s" % (count, fmt[1]), self.d, pos))
                    if count else [])
            vals = [round(v, 6) if isinstance(v, float) else v for v in vals]
            return dict(inner=inner, count=count, elements=vals), payload_end, True, None
        if inner in REF_TYPES:
            refs = [self.obj_ref(struct.unpack_from("<i", self.d, pos + 4 * k)[0])
                    for k in range(count)]
            return dict(inner=inner, count=count, elements=refs), payload_end, \
                avail >= 4 * count, None
        if inner in ("AssetObjectProperty", "AssetPtrProperty", "AssetClassProperty",
                     "SoftObjectProperty", "SoftClassProperty"):
            elems, ok = [], True
            for _ in range(count):
                s, npos = read_fstring(self.d, pos)
                if s is None:
                    ok = False
                    break
                elems.append(s)
                pos = npos
            return dict(inner=inner, count=count, elements=elems), payload_end, ok, \
                None if ok else "asset-path FString element decode stopped"
        if inner == "StructProperty":
            return self.read_struct_array(pos, payload_end, count, depth)
        if inner == "StrProperty":
            elems, ok = [], True
            for _ in range(count):
                s, npos = read_fstring(self.d, pos)
                if s is None:
                    ok = False
                    break
                elems.append(s)
                pos = npos
            return dict(inner=inner, count=count, elements=elems), payload_end, ok, \
                None if ok else "FString element decode stopped"
        if inner == "NameProperty":
            elems, ok = [], True
            for _ in range(count):
                try:
                    _, _, s = self.fname(pos)
                except ParseError:
                    ok = False
                    break
                elems.append(s)
                pos += 8
            return dict(inner=inner, count=count, elements=elems), payload_end, ok, \
                None if ok else "FName element decode stopped"
        if inner == "TextProperty":
            # each element is a self-delimiting FText (variable length) - walk serially
            elems, ok = [], True
            epos = pos
            for _ in range(count):
                if epos >= payload_end:
                    ok = False
                    break
                fields, consumed, eok, ewhy = parse_ftext(self.d[epos:payload_end],
                                                          strict=False)
                if not eok or consumed == 0:
                    ok = False
                    break
                elems.append(fields)
                epos += consumed
            if ok and epos != payload_end:
                ok = False
            return dict(inner=inner, count=count, elements=elems), payload_end, ok, \
                None if ok else "FText element decode stopped"
        if inner == "BoolProperty":
            n = min(count, avail)
            elems = [bool(b) for b in self.d[pos:pos + n]]
            return dict(inner=inner, count=count, elements=elems,
                        note="bool array: assumed 1 byte/elem, unverified"), payload_end, \
                False, "BoolProperty array layout unverified"
        return dict(inner=inner, count=count, raw=self.d[pos:payload_end].hex()), \
            payload_end, False, f"unmodelled Array inner type {inner}"

    def read_struct_array(self, pos, payload_end, count, depth):
        """TArray<struct> in cooked 413 has no per-element tag header and no struct name.
        Strategy: (1) if region divides evenly by count, assume fixed element size and
        require each slice to be a self-consistent stream; (2) else walk self-delimiting
        streams; (3) else keep raw."""
        avail = payload_end - pos
        if count <= 0:
            return dict(inner="StructProperty", count=0, elements=[]), payload_end, True, None
        # (2) sequential self-delimiting streams
        epos, elems, clean = pos, [], True
        while epos < payload_end and len(elems) < count:
            try:
                fields, after, ok, why = self.read_stream(epos, payload_end, depth + 1)
            except ParseError:
                clean = False
                break
            if not ok or after <= epos:
                clean = False
                break
            elems.append(fields)
            epos = after
        if clean and epos == payload_end and len(elems) == count:
            return dict(inner="StructProperty", count=count, elements=elems), \
                payload_end, True, "sequential tagged structs"
        # (1) fixed size
        if avail % count == 0:
            esz = avail // count
            if 0 < esz <= 4096:
                elems, ok_all, sizes = [], True, set()
                for k in range(count):
                    start = pos + k * esz
                    fields, after, ok, why = self.read_stream(start, start + esz, depth + 1)
                    if not ok or after != start + esz:
                        ok_all = False
                        break
                    elems.append(fields)
                if ok_all:
                    return dict(inner="StructProperty", count=count, elem_size=esz,
                                elements=elems), payload_end, True, \
                        f"fixed {esz}-byte tagged structs"
        return dict(inner="StructProperty", count=count, raw=self.d[pos:payload_end].hex()), \
            payload_end, False, "struct array unresolved (no struct name available in 413)"

    # ---------------- StructProperty
    def read_struct(self, pos, region_end, size, type_num, depth):
        if type_num != 0:
            return dict(raw=self.d[pos:region_end].hex()), region_end, False, \
                f"Struct Type number {type_num}", None
        if pos + 8 > region_end:
            return dict(raw=self.d[pos:region_end].hex()), region_end, False, \
                "no room for StructName FName", None
        _, snum, stype = self.fname(pos)
        pos += 8
        if snum != 0:
            return dict(raw=self.d[pos:region_end].hex()), region_end, False, \
                f"unexpected StructName number {snum}", stype
        content_end = pos + size
        if content_end > region_end:
            return dict(raw=self.d[pos:region_end].hex()), region_end, False, \
                "struct content overruns region", stype
        if size == 0:
            return {}, content_end, True, "empty struct", stype
        if stype in POD_STRUCTS:
            fmt, psz = POD_STRUCTS[stype]
            blob = self.d[pos:content_end]
            if len(blob) != psz:
                return dict(raw=blob.hex()), content_end, False, \
                f"{stype} needs {psz} bytes, got {len(blob)}", stype
            vals = struct.unpack(fmt, blob)
            keys = POD_NAMES.get(stype)
            if keys and len(keys) == len(vals):
                val = {k: (round(v, 6) if isinstance(v, float) else v)
                       for k, v in zip(keys, vals)}
            else:
                val = [round(v, 6) if isinstance(v, float) else v for v in vals]
            return val, content_end, True, None, stype
        fields, after, ok, why = self.read_stream(pos, content_end, depth + 1)
        return fields, content_end, (ok and after == content_end), why, stype

    # ---------------- Map/Set (kept raw for 413)
    def read_container(self, ptype, pos, size):
        blob = self.d[pos:pos + size]
        out = dict(size=size, raw=blob.hex())
        if len(blob) >= 8:
            kc, vc = struct.unpack_from("<ii", blob, 0)
            if 0 <= kc <= 2_000_000 and 0 <= vc <= 2_000_000:
                out["key_count"], out["value_count"] = kc, vc
        return out, f"{ptype}: unmodelled for 413 (SET_MAP_SUPPORT absent) - raw kept"

    # ---------------- EnumProperty / SoftObjectProperty / FText
    def read_byte_enum(self, pos, region_end, size):
        """ByteProperty carrying a legacy byte-enum: tag has an EnumType FName that is
        NOT counted in Size; the value (Size bytes) is an FName pair (Size==8), a single
        byte (Size==1) or a uint32 (Size==4).  Verified on WT_T1_BoltPistol:
        WargearSlot/ByteProperty size=8 -> EnumType 'EWargearSlot' + value FName
        'EWargearSlot::AssaultRangedWeapon'."""
        if pos + 8 > region_end:
            raise ParseError("ByteProperty enum: no room for EnumType FName")
        _, _, etype = self.fname(pos)
        p = pos + 8
        if size == 8:
            if p + 8 > region_end:
                raise ParseError("ByteProperty enum value FName overrun")
            _, vnum, vname = self.fname(p)
            return dict(enum_type=etype, value=vname, value_number=vnum), region_end
        if size == 1:
            if p + 1 > region_end:
                raise ParseError("ByteProperty enum byte overrun")
            return dict(enum_type=etype, value=self.d[p]), region_end
        if size == 4:
            if p + 4 > region_end:
                raise ParseError("ByteProperty enum u32 overrun")
            return dict(enum_type=etype, value=struct.unpack_from("<I", self.d, p)[0]), region_end
        raise ParseError(f"ByteProperty enum value size {size}")

    def read_asset_ref(self, pos, region_end, size):
        """AssetObjectProperty / AssetPtrProperty / SoftObjectProperty / SoftClassProperty
        in this build: value serialized as an FString asset path
        ("/Game/.../Name.Name").  Verified on WT_T1_BoltPistol
        (DropgameTemplate size=85 -> int32 81 + 80 chars + NUL) and LT_Based1 arrays."""
        s, npos = read_fstring(self.d, pos)
        used = npos - pos
        if s is None:
            return dict(raw=self.d[pos:pos + size].hex()), region_end, False, \
                "asset-path FString decode failed"
        if used > size:
            return dict(asset_path=s, note="FString longer than Size"), region_end, False, \
                f"FString {used} > Size {size}"
        out = dict(asset_path=s)
        if used < size:
            out["tail_hex"] = self.d[npos:pos + size].hex()
        return out, region_end, used == size, None if used == size else "FString shorter than Size"

    def read_enum(self, pos, region_end, size):
        if pos + 8 > region_end:
            raise ParseError("EnumProperty: no room for EnumType FName")
        _, _, etype = self.fname(pos)
        vsize = size - 8
        if vsize < 0:
            raise ParseError("EnumProperty Size < 8")
        fmt = {8: "<q", 4: "<i", 2: "<h", 1: "<B"}.get(vsize)
        if fmt is None:
            raise ParseError(f"EnumProperty value size {vsize}")
        if pos + 8 + vsize > region_end:
            raise ParseError("EnumProperty value overrun")
        return dict(enum_type=etype, value=struct.unpack_from(fmt, self.d, pos + 8)[0]), \
            pos + size

    def read_soft(self, pos, region_end, size):
        s, npos = read_fstring(self.d, pos)
        used = npos - pos
        if s is not None and used <= size:
            out = dict(asset_path=s)
            if size - used:
                out["tail_hex"] = self.d[npos:pos + size].hex()
            return out, pos + size
        return dict(raw=self.d[pos:pos + size].hex()), pos + size

    def read_text(self, pos, region_end, size):
        """Delegate to module-level parse_ftext (validated on 553 samples)."""
        blob = self.d[pos:pos + size]
        fields, consumed, ok, why = parse_ftext(blob)
        return fields, region_end, ok, why

    # ---------------- walk
    def read_stream(self, pos: int, end: int, depth: int = 0):
        props = []
        while True:
            if pos + 8 > end:
                return props, pos, False, f"truncated: no room for tag at {pos} (end {end})"
            try:
                tag = self.read_tag(pos, end)
            except ParseError as e:
                return props, pos, False, f"tag error at {pos}: {e}"
            if tag["terminator"]:
                return props, tag["pos"] + tag["consumed"], True, f"terminator@{tag['pos']}"
            name, ptype, size = tag["name"], tag["type"], tag["size"]
            hdr = pos + 24
            # In the 413 generation several property types serialize an EXTRA FName
            # after the 24-byte header that is NOT counted in Size:
            #   StructProperty -> StructName FName      ArrayProperty -> InnerType FName
            #   ByteProperty   -> EnumType FName (only when Size >= 8, i.e. an enum)
            #   EnumProperty   -> EnumType FName (Size counts the value only)
            #   InterfaceProperty -> InterfaceClassName FName
            # Verified byte-exactly on STAT_BroodLord, BL_Based1, WT_T1_BoltPistol,
            # LT_Based1 (see reports/layout_notes.md).
            ext = 0
            if tag["type_num"] == 0:
                if ptype in ("StructProperty", "ArrayProperty", "EnumProperty"):
                    ext = 8
                elif ptype == "ByteProperty" and size >= 8:
                    ext = 8
            region_end = hdr + ext + size
            entry = dict(name=name, type=ptype, size=size, tag_off=pos,
                         value_off=hdr, ext_name_fname=bool(ext), region_end=region_end)
            if region_end > end:
                entry.update(size=size, raw=self.d[hdr:end].hex(), ok=False,
                             why=f"region {hdr}+{ext}+{size} exceeds export end {end}")
                props.append(entry)
                return props, end, False, f"{name} ({ptype}) region overruns export end"
            if tag["type_num"] != 0:
                entry["type_number"] = tag["type_num"]
            try:
                if ptype == "ArrayProperty":
                    payload, npos, ok, why = self.read_array(hdr, region_end, size,
                                                             tag["type_num"], depth)
                    entry.update(value=payload, flags=tag["flags"], ok=ok, why=why)
                elif ptype == "StructProperty":
                    value, npos, ok, why, stype = self.read_struct(hdr, region_end, size,
                                                                   tag["type_num"], depth)
                    entry.update(struct_type=stype, value=value, flags=tag["flags"],
                                 ok=ok, why=why)
                elif ptype in ("MapProperty", "SetProperty"):
                    payload, why = self.read_container(ptype, hdr, size)
                    entry.update(value=payload, flags=tag["flags"], ok=False, why=why)
                elif ptype == "EnumProperty":
                    value, npos = self.read_enum(hdr, region_end, size)
                    entry.update(value=value, flags=tag["flags"], ok=True)
                elif ptype == "ByteProperty" and ext == 8:
                    # legacy byte-enum: EnumType FName in tag (not counted in Size) + value
                    value, npos = self.read_byte_enum(hdr, region_end, size)
                    entry.update(value=value, flags=tag["flags"], ok=True)
                elif ptype in ("AssetObjectProperty", "AssetPtrProperty",
                               "AssetClassProperty", "AssetClassPtrProperty",
                               "SoftObjectProperty", "SoftClassProperty",
                               "LazyObjectProperty"):
                    # UE4 < SET_MAP_SUPPORT / pre-5.x: these carry an FString asset path
                    value, npos, ok, why = self.read_asset_ref(hdr, region_end, size)
                    entry.update(value=value, flags=tag["flags"], ok=ok, why=why)
                elif ptype == "TextProperty":
                    value, npos, ok, why = self.read_text(hdr, region_end, size)
                    entry.update(value=value, flags=tag["flags"], ok=ok, why=why)
                else:
                    try:
                        val, used = self.read_scalar(ptype, size, hdr, region_end, tag["flags"])
                        entry.update(value=val, ok=True)
                    except ParseError as e:
                        entry.update(size=size, raw=self.d[hdr:region_end].hex(), ok=False,
                                     why=str(e))
                    npos = region_end
            except ParseError as e:
                entry.update(size=size, raw=self.d[hdr:region_end].hex(), ok=False,
                             why=f"{ptype}: {e}")
                npos = region_end
            props.append(entry)
            if npos <= pos:
                return props, pos, False, f"no progress after {name} ({ptype})"
            pos = npos
            if pos > end:
                return props, pos, False, f"overflow {pos} > {end}"


# ------------------------------------------------------------------ package level
def normalize_exports(exps, names, imports):
    """Map decoded export records into the shape used by PropStream/walk_package."""
    out = []
    for e in exps:
        cls = resolve_ref(e["class_index"], names, [], imports)
        out.append(dict(name=e["object_name"], cls=cls, off=e["serial_offset"],
                        size=e["serial_size"], super=resolve_ref(e["super_index"], names,
                                                                 exps, imports),
                        package_index=e["package_index"], flags=e["object_flags"],
                        guid=e["guid"]))
    return out


def walk_package(d: bytes, names, exports, imports):
    live = [e for e in exports if e["size"]]
    if not live:
        return [], None, None
    bulk = min(e["off"] for e in live)
    end = max(e["off"] + e["size"] for e in live)
    walks = []
    for exp in sorted(live, key=lambda e: e["off"]):
        pr = PropStream(d, names, exports, imports)
        try:
            props, after, ok, why = pr.read_stream(exp["off"], exp["off"] + exp["size"])
        except ParseError as e:
            props, after, ok, why = [], exp["off"], False, str(e)
        tail = (exp["off"] + exp["size"]) - after
        entry = dict(name=exp["name"], cls=exp["cls"], off=exp["off"], size=exp["size"],
                     ok=ok, why=why, after=after, props=props, trailing=tail)
        # A class with its own virtual Serialize() leaves a compact binary block after the
        # tagged-property terminator (GameBoardLayoutSaveData does exactly this: 4120 B on
        # BL_Based1).  Capture it verbatim - it is the real payload, not noise.
        if 0 < tail <= 262144:
            entry["tail_hex"] = d[after:after + tail].hex()
        walks.append(entry)
    return walks, bulk, end


def package_path(rel: str):
    for ext in (".uasset", ".umap"):
        cand = GAME / (rel + ext)
        if cand.exists():
            return cand
    return None


def read_package(rel: str):
    path = package_path(rel)
    if path is None:
        return dict(rel=rel, ok=False, why="package file not found")
    d = path.read_bytes()
    out_base = dict(rel=rel, package=path.name, file_size=len(d))

    # ---- path 1: the package's own summary offsets (authoritative, no guessing)
    try:
        s, names, imps, raw_exps, esz = tables_from_file(d, rel)
    except ParseError as e:
        return dict(out_base, ok=False, why=f"summary/tables: {e}")
    exports = normalize_exports(raw_exps, names, imps)
    walks, bulk, end = walk_package(d, names, exports, imps)
    allok = bool(walks) and all(w["ok"] and w["trailing"] == 0 for w in walks)
    out = dict(out_base, ok=allok, tables="self", name_off=s["name_offset"],
               headers_size=s["headers_size"], package_flags=s["package_flags"],
               import_rec=IMPORT_REC, export_rec=esz, summary=s,
               names=names, imports=imps,
               exports=[dict(name=e["name"], cls=e["cls"], off=e["off"], size=e["size"],
                             super=e["super"]) for e in exports],
               walks=walks, bulk=bulk, region_end=end)
    if allok:
        return out

    # ---- path 2: cross-check against UModel's table (catches patched/re-cooked files)
    info = umodel_list(rel)
    if not info:
        return dict(out, ok=False, why="own tables parsed but stream failed; umodel failed too")
    if not info["exports"]:
        return dict(out, ok=False, why="stream failed; umodel: no supported objects")
    um_exps = [dict(name=e["name"], cls=e["cls"], off=e["off"], size=e["size"], super=None)
               for e in info["exports"]]
    um_walks, um_bulk, um_end = walk_package(d, names, um_exps, imps)
    um_ok = bool(um_walks) and all(w["ok"] and w["trailing"] == 0 for w in um_walks)
    out["umodel"] = dict(name_count=info["name_count"], import_count=info["import_count"],
                         export_count=info["export_count"], bulk=um_bulk,
                         exports=um_exps, walks=um_walks, ok=um_ok)
    out["why"] = "self-table stream incomplete; umodel cross-check recorded"
    if um_ok:
        out["exports"] = um_exps
        out["walks"] = um_walks
        out["bulk"] = um_bulk
        out["region_end"] = um_end
        out["ok"] = True
        out["why"] = "walked using umodel export map (own summary used for names/imports)"
    return out


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    if argv[1] == "--batch":
        out_path = pathlib.Path(argv[2])
        rels = argv[3:]
        recs = [read_package(r) for r in rels]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
        full = sum(1 for r in recs if r.get("ok"))
        print(f"wrote {out_path}: {len(recs)} packages, fully-decoded {full}")
        return 0
    if argv[1] == "--json":
        for rel in argv[2:]:
            print(json.dumps(read_package(rel), ensure_ascii=False))
        return 0
    for rel in argv[1:]:
        r = read_package(rel)
        print("=" * 110)
        meta = dict(rel=r.get("rel"), package=r.get("package"), ok=r.get("ok"),
                    why=r.get("why"), name_off=r.get("name_off"),
                    import_rec=r.get("import_rec"), export_rec=r.get("export_rec"),
                    file_size=r.get("file_size"), bulk=r.get("bulk"),
                    region_end=r.get("region_end"), names=r.get("names"),
                    imports=r.get("imports"),
                    exports=[dict(name=e["name"], cls=e["cls"], off=hex(e["off"]),
                                  size=e["size"]) for e in r.get("exports", [])])
        print(json.dumps(meta, ensure_ascii=False, indent=1))
        for w in r.get("walks", []):
            print("--- walk %s %s @%s+%d ok=%s trailing=%s why=%s"
                  % (w["cls"], w["name"], hex(w["off"]), w["size"], w["ok"],
                     w["trailing"], w["why"]))
            print(json.dumps(w["props"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
