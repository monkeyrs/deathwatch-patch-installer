# -*- coding: utf-8 -*-
"""Installer runtime asset recipes.

Each function reads the original .uasset from the user's game directory and returns the computed new bytes;
It does not depend on a full uasset in the payload. Backups and writes are handled by patcher.Session.
"""
from __future__ import annotations

import pathlib
import struct

import ue4prop
import uasset_mutate as um

CONTENT_PREFIX = "deathwatchgame/Content/"


def _sha(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b).hexdigest()


def _content_rel(rel: str) -> str:
    rel = str(rel).replace("\\", "/")
    if rel.startswith(CONTENT_PREFIX):
        return rel[len(CONTENT_PREFIX):]
    if rel.startswith("deathwatchgame/"):
        return rel.split("/", 1)[1]
    return rel


def _read(content: pathlib.Path, rel: str) -> bytes:
    p = content / _content_rel(rel)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    return p.read_bytes()


def apply_edits(data: bytes, edits: list[dict]) -> bytes:
    """balance_user_* / pure offset edits to int32 or float32 values."""
    buf = bytearray(data)
    for e in edits or []:
        off = int(e["off"])
        if e.get("slot") == "float" or e.get("kind") == "bytecode":
            old = struct.unpack_from("<f", buf, off)[0]
            if abs(old - float(e["old"])) > 1e-6:
                raise RuntimeError(f"float old value mismatch @{off}: {old} != {e['old']}")
            struct.pack_into("<f", buf, off, float(e["new"]))
        else:
            old = struct.unpack_from("<i", buf, off)[0]
            if old != int(e["old"]):
                raise RuntimeError(f"int old value mismatch @{off}: {old} != {e['old']}")
            struct.pack_into("<i", buf, off, int(e["new"]))
    if len(buf) != len(data):
        raise RuntimeError("edits changed the file length")
    return bytes(buf)


# ---------------------------------------------------------------- parry
PARRY_REL = "Characters/Buffs/BUFF_Parry"
PARRY_OLD = struct.pack("<f", 0.8)
PARRY_NEW = struct.pack("<f", 0.2)


def recipe_parry(data: bytes, rel: str) -> bytes:
    hits, i = [], data.find(PARRY_OLD)
    while i != -1:
        hits.append(i)
        i = data.find(PARRY_OLD, i + 1)
    if len(hits) != 1:
        raise RuntimeError(f"Parry 0.8f matched {len(hits)} occurrences")
    off = hits[0]
    if data.find(b"DamageReduction") < 0 or data.find(b"absorbing") < 0:
        raise RuntimeError("Parry anchor/description missing")
    buf = bytearray(data)
    buf[off:off + 4] = PARRY_NEW
    if abs(struct.unpack_from("<f", buf, off)[0] - 0.2) > 1e-6:
        raise RuntimeError("Parry readback is not 0.2")
    if len(buf) != len(data):
        raise RuntimeError("Parry length changed")
    return bytes(buf)


# ------------------------------------------------------------ maxap
MAXAP_REL = "Characters/StatSheets/StatModifiers/STAT_Plus1AP"
MAXAP_CTRL = "Characters/StatSheets/StatModifiers/STAT_Plus1APAnd1APPerTurn.uasset"
MAXAP_TAG = (struct.pack("<i", 0x19) + b"\x00" * 4 +
             struct.pack("<i", 0x11) + b"\x00" * 4 +
             struct.pack("<i", 4))


def _parse_maxap_elems(d: bytes) -> list[dict]:
    out, i = [], d.find(MAXAP_TAG)
    while i >= 0:
        idx, val = struct.unpack_from("<2i", d, i + len(MAXAP_TAG))
        out.append({"tag_off": i, "val_off": i + len(MAXAP_TAG) + 4,
                    "index": idx, "value": val})
        i = d.find(MAXAP_TAG, i + 1)
    return out


def recipe_maxap(data: bytes, rel: str, control: bytes) -> bytes:
    elems = _parse_maxap_elems(data)
    ctrl = _parse_maxap_elems(control)
    if len(elems) != 9 or [e["index"] for e in elems] != [e["index"] for e in ctrl]:
        raise RuntimeError("STAT_Plus1AP element layout mismatch")
    cur = {e["index"]: e["value"] for e in elems}
    if cur.get(3) != 1 or cur.get(2) != 0:
        raise RuntimeError(f"STAT_Plus1AP bug signature mismatch: {cur}")
    target = next(e for e in elems if e["index"] == 2)
    blk = data[target["tag_off"]:target["tag_off"] + 28]
    if len(blk) != 28 or data.count(blk) != 1:
        raise RuntimeError("STAT_Plus1AP target block is not unique")
    buf = bytearray(data)
    struct.pack_into("<i", buf, target["val_off"], 1)
    after = {e["index"]: e["value"] for e in _parse_maxap_elems(bytes(buf))}
    if after != {**cur, 2: 1}:
        raise RuntimeError(f"STAT_Plus1AP readback mismatch: {after}")
    if len(buf) != len(data):
        raise RuntimeError("STAT_Plus1AP length changed")
    return bytes(buf)


# -------------------------------------------------------- wargear material
WARGEAR_TARGETS = [
    "Wargear/GravGuns/GravGuns_T4/STM_T4_GravGun",
    "Wargear/Flamers/Flamers_T4/STM_T4_SW_Flamer",
    "Wargear/HandFlamers/HandFlamer_T4/STM_T4_SW_Hand_Flamer",
]


def _find_materials_array(data: bytes, rel: str) -> dict:
    rel = rel[:-7] if rel.endswith(".uasset") else rel
    s, names, imps, exps, esz = ue4prop.tables_from_file(data, rel)
    if "Materials" not in names or "ArrayProperty" not in names:
        raise RuntimeError(f"{rel}: name table missing Materials/ArrayProperty")
    mi, ai, oi = names.index("Materials"), names.index("ArrayProperty"), names.index("ObjectProperty")
    mesh = [e for e in exps if e["object_name"] == rel.split("/")[-1]]
    if len(mesh) != 1:
        raise RuntimeError(f"{rel}: main mesh export is not unique")
    start, size = mesh[0]["serial_offset"], mesh[0]["serial_size"]
    end = start + size
    hit = None
    for pos in range(start, end - 48, 4):
        if struct.unpack_from("<ii", data, pos) != (mi, 0):
            continue
        if struct.unpack_from("<ii", data, pos + 8) != (ai, 0):
            continue
        if struct.unpack_from("<ii", data, pos + 24) != (oi, 0):
            continue
        hit = pos
        break
    if hit is None:
        raise RuntimeError(f"{rel}: Materials ArrayProperty not found")
    arr_size = struct.unpack_from("<i", data, hit + 16)[0]
    count = struct.unpack_from("<i", data, hit + 32)[0]
    if arr_size != count * 4 + 4:
        raise RuntimeError(f"{rel}: Materials array Size/count inconsistent")
    voff = hit + 36
    values = list(struct.unpack_from("<%di" % count, data, voff))
    world = [i for i, im in enumerate(imps) if im.get("object_name") == "WorldGridMaterial"]
    lo = [i for i, im in enumerate(imps)
          if str(im.get("object_name", "")).endswith("_Lo")
          and im.get("class_name") == "MaterialInstanceConstant"]
    if len(world) != 1 or len(lo) != 1:
        raise RuntimeError(f"{rel}: WorldGrid/Lo import is not unique")
    return dict(values_off=voff, values=values,
                world_ref=-(world[0] + 1), lo_ref=-(lo[0] + 1))


def recipe_wargear(data: bytes, rel: str) -> bytes:
    info = _find_materials_array(data, rel)
    slots = [i for i, v in enumerate(info["values"]) if v == info["world_ref"]]
    if not slots:
        raise RuntimeError(f"{rel}: no WorldGrid slot to patch")
    buf = bytearray(data)
    for i in slots:
        struct.pack_into("<i", buf, info["values_off"] + i * 4, info["lo_ref"])
    if len(buf) != len(data):
        raise RuntimeError(f"{rel}: length changed")
    back = _find_materials_array(bytes(buf), rel)
    if back["values"] != [info["lo_ref"] if i in slots else v
                          for i, v in enumerate(info["values"])]:
        raise RuntimeError(f"{rel}: material slot readback mismatch")
    return bytes(buf)


# -------------------------------------------------------- chapter icons
CHAPTER_PAIR = [("UI/Icons/Chapters/Ch-DarkA-128", "UI/Icons/Chapters/Ch-blackTemp-128")]
PNG_BYTES = 4


def _pixel_layout(data: bytes, rel: str) -> dict:
    rel = rel[:-7] if rel.endswith(".uasset") else rel
    s, names, imps, exps, esz = ue4prop.tables_from_file(data, rel)
    ex = [e for e in exps if e["object_name"] == rel.split("/")[-1]]
    if len(ex) != 1:
        raise RuntimeError(f"{rel}: main export is not unique")
    start = ex[0]["serial_offset"] + ex[0]["serial_size"]
    region = len(data) - start
    for dim in (256, 128, 64, 32, 16):
        n, o = dim, 0
        while n >= 1:
            o += n * n * PNG_BYTES
            n //= 2
        if 0 <= region - o < 8:
            return dict(start=start, chain=o, tail=region - o,
                        tail_bytes=data[start + o:])
    raise RuntimeError(f"{rel}: unrecognized pixel layout")


def recipe_chapter(a: bytes, b: bytes) -> tuple[bytes, bytes]:
    ra, rb = CHAPTER_PAIR[0]
    la, lb = _pixel_layout(a, ra), _pixel_layout(b, rb)
    if (la["chain"], la["tail"]) != (lb["chain"], lb["tail"]):
        raise RuntimeError("chapter texture layouts differ")
    if la["tail_bytes"] != lb["tail_bytes"]:
        raise RuntimeError("chapter texture tails differ")
    sa, ca, sb, cb = la["start"], la["chain"], lb["start"], lb["chain"]
    new_a = a[:sa] + b[sb:sb + cb] + a[sa + ca:]
    new_b = b[:sb] + a[sa:sa + ca] + b[sb + cb:]
    if len(new_a) != len(a) or len(new_b) != len(b):
        raise RuntimeError("chapter length changed")
    if new_a[sa:sa + ca] != b[sb:sb + cb] or new_b[sb:sb + cb] != a[sa:sa + ca]:
        raise RuntimeError("chapter pixel-swap readback mismatch")
    return new_a, new_b


# -------------------------------------------------------- alacrity
ALACRITY_A = "Characters/Buffs/BUFF_25PercentGain1APOnBeginTurn"
ALACRITY_B = "Characters/Buffs/BUFF_Plus1AP_1Turn"
AND_ANCHOR = (bytes.fromhex("1ce9ffffff") + bytes([0x01]) +
              struct.pack("<i", 5) + bytes([0x01]) + struct.pack("<i", 3))


def _alacrity_a(data: bytes) -> bytes:
    hits, i = [], data.find(AND_ANCHOR)
    while i != -1:
        hits.append(i)
        i = data.find(AND_ANCHOR, i + 1)
    if len(hits) != 1:
        raise RuntimeError(f"Alacrity AND anchor matched {len(hits)} occurrences")
    off_p = hits[0] + 6
    if struct.unpack_from("<i", data, off_p)[0] != 5:
        raise RuntimeError("Alacrity AND1 first input index is not 5")
    buf = bytearray(data)
    buf[off_p] = 3
    if len(buf) != len(data):
        raise RuntimeError("Alacrity A length changed")
    return bytes(buf)


def recipe_alacrity(content: pathlib.Path, a: bytes) -> tuple[bytes, bytes]:
    um.GAME = content
    ue4prop.GAME = content
    rb = um.insert_cdo_int_property(ALACRITY_B, "TurnsLeft", 1)
    return _alacrity_a(a), rb["dst"]


# -------------------------------------------------------- praetorian
PRAETORIAN_REL = "Characters/Buffs/BUFF_Plus20DamageMitgation_NonStacking"


def recipe_praetorian(content: pathlib.Path, data: bytes) -> bytes:
    um.GAME = content
    ue4prop.GAME = content
    return um.retarget_import(
        PRAETORIAN_REL, "BUFF_Plus20Damage_1Turn_C",
        "BUFF_20DamageMitigation_1Turn")["dst"]


# -------------------------------------------------------- visual ini
VISUAL_FRAME = ["r.VSync=0", "t.MaxFPS=144"]
VISUAL_TIERS = {
    "tier_igpu": [
        "r.MaxAnisotropy=16", "r.Streaming.MipBias=-1",
        "r.DefaultFeature.MotionBlur=0", "r.DepthOfFieldQuality=0",
        "r.SceneColorFringeQuality=0", "r.TonemapperQuality=2",
        "r.EyeAdaptationQuality=2", "r.ShadowQuality=4",
        "r.Shadow.MaxResolution=1024", "r.Shadow.CSM.MaxCascades=2",
        "r.Shadow.DistanceScale=1.0", "r.DefaultFeature.Bloom=1",
        "r.BloomQuality=2", "r.DefaultFeature.LensFlare=1",
        "r.LensFlareQuality=1", "r.LightShaftQuality=1",
        "r.Color.Mid=0.625", "r.Color.Max=1.00",
    ],
    "tier_dgpu": [
        "r.MaxAnisotropy=16", "r.Streaming.MipBias=-1",
        "r.DefaultFeature.MotionBlur=0", "r.DepthOfFieldQuality=0",
        "r.SceneColorFringeQuality=0", "r.ScreenPercentage=150",
        "r.ShadowQuality=5", "r.Shadow.MaxResolution=2048",
        "r.Shadow.CSM.MaxCascades=3", "r.Shadow.DistanceScale=1.5",
        "r.PostProcessAAQuality=4", "r.AmbientOcclusionLevels=3",
        "r.DefaultFeature.Bloom=1", "r.BloomQuality=5",
        "r.DefaultFeature.LensFlare=1", "r.LensFlareQuality=3",
        "r.LightShaftQuality=1", "r.SSR.Quality=3",
        "r.TonemapperQuality=2", "r.EyeAdaptationQuality=2",
        "r.Color.Mid=0.625", "r.Color.Max=1.00",
        "r.TranslucencyLightingVolumeDim=128",
    ],
    "tier_max": [
        "r.MaxAnisotropy=16", "r.Streaming.MipBias=-2",
        "r.DefaultFeature.MotionBlur=0", "r.DepthOfFieldQuality=0",
        "r.SceneColorFringeQuality=0", "r.TonemapperQuality=2",
        "r.EyeAdaptationQuality=2", "r.ScreenPercentage=200",
        "r.ShadowQuality=5", "r.Shadow.MaxResolution=4096",
        "r.Shadow.CSM.MaxCascades=4", "r.Shadow.DistanceScale=2",
        "r.Shadow.RadiusThreshold=0.01", "r.PostProcessAAQuality=4",
        "r.MSAA.CompositingSampleCount=8", "r.AmbientOcclusionLevels=3",
        "r.AmbientOcclusionRadiusScale=1.0", "r.DefaultFeature.Bloom=1",
        "r.BloomQuality=5", "r.DefaultFeature.LensFlare=1",
        "r.LensFlareQuality=3", "r.LightShaftQuality=1",
        "r.SSR.Quality=3", "r.TranslucencyLightingVolumeDim=128",
        "r.Color.Mid=0.625", "r.Color.Max=1.00",
    ],
}
VISUAL_STREAM = (
    ("AsyncLoadingTimeLimit=5.000000", "AsyncLoadingTimeLimit=10.0"),
    ("PriorityAsyncLoadingExtraTime=20.000000", "PriorityAsyncLoadingExtraTime=30.0"),
)


def recipe_visual_ini(data: bytes, tier: str) -> bytes:
    lines = VISUAL_TIERS.get(tier)
    if lines is None:
        raise RuntimeError(f"unknown visual tier: {tier!r}")
    text = data.decode("ascii")
    for old, new in VISUAL_STREAM:
        if text.count(old) != 1:
            raise RuntimeError(f"visual streaming key is not unique: {old} count={text.count(old)}")
        text = text.replace(old, new)
    block = "[SystemSettings]\r\n" + "\r\n".join(lines + VISUAL_FRAME) + "\r\n"
    block += "[/Script/Engine.Engine]\r\nbSmoothFrameRate=False\r\n"
    return text.encode("ascii") + block.encode("ascii")


# -------------------------------------------------------- package dispatch
def compute_package(content: pathlib.Path, doc: dict) -> dict[str, bytes]:
    """Compute the full package output from manifest.recipe; returns {relative_path: new_bytes}."""
    recipe = doc.get("recipe")
    entries = doc.get("entries") or []
    rels = [e.get("relative_path") for e in entries if e.get("relative_path")]
    content = pathlib.Path(content)
    out: dict[str, bytes] = {}

    if recipe == "asset_edits":
        for e in entries:
            rel = e.get("relative_path")
            out[rel] = apply_edits(_read(content, rel), e.get("edits") or [])
        return out

    if recipe == "visual_ini":
        rel = rels[0]
        # DefaultEngine.ini lives in <game>/deathwatchgame/Config, not under Content.
        p = content.parents[1] / rel
        out[rel] = recipe_visual_ini(p.read_bytes(), doc.get("tier") or "")
        return out

    if recipe == "parry":
        rel = rels[0]
        out[rel] = recipe_parry(_read(content, rel), _content_rel(rel))
        return out

    if recipe == "maxap":
        rel = rels[0]
        ctrl = _read(content, MAXAP_CTRL)
        out[rel] = recipe_maxap(_read(content, rel), _content_rel(rel), ctrl)
        return out

    if recipe == "wargear_materials":
        for rel in rels:
            out[rel] = recipe_wargear(_read(content, rel), _content_rel(rel))
        return out

    if recipe == "chapter_icons":
        rel_a = next(r for r in rels if r.endswith("Ch-DarkA-128.uasset"))
        rel_b = next(r for r in rels if r.endswith("Ch-blackTemp-128.uasset"))
        na, nb = recipe_chapter(_read(content, rel_a), _read(content, rel_b))
        out[rel_a], out[rel_b] = na, nb
        return out

    if recipe == "alacrity_ap":
        rel_a = next(r for r in rels if ALACRITY_A in r)
        rel_b = next(r for r in rels if ALACRITY_B in r)
        na, nb = recipe_alacrity(content, _read(content, rel_a))
        out[rel_a], out[rel_b] = na, nb
        return out

    if recipe == "praetorian":
        rel = rels[0]
        out[rel] = recipe_praetorian(content, _read(content, rel))
        return out

    raise RuntimeError(f"unknown recipe: {recipe!r}")
