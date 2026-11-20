#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""uasset_mutate.py —— cooked UE4.6 uasset 变长编辑器（OB-13 线工具）。

原理（本世代 413/UE4.6 布局，ue4prop.parse_summary 已验证）：
  [summary（恰好止于 NameOffset）][name 表（FString 链，无 hash）][import 表 28B/条]
  [export 表 68B/条][depends…][export 数据…]；HeadersSize = 首个 export SerialOffset。
变长操作只在 name 表内增删字符串 ⇒ 其后所有区域整体平移，只需补丁：
  summary 的 import/export/depends/sar/thumbnail 偏移与 HeadersSize、name_count，
  以及每条 export 记录的 SerialOffset 字段。其余字节原样复制。

支持操作：
  retarget-import <rel> <old_object_name> <new_base_name>
    把 import 表里 object_name == old_object_name 的条目（及其 package 路径条目）
    指向 new_base_name（自动派生 "<new>_C" 与 "/Game/<原目录>/<new>" 两个新 name 条目；
    若 name 表已有同名条目则直接复用 ⇒ delta=0 纯重指向）。
    字节码 EX_ObjectConst 按 import 索引引用，无需改动 ⇒ AddBuff/HasBuff 自动跟随。

验证（L0）：
  ① 重建后用 ue4prop.tables_from_file 复析：name 数 +N、目标 import 条目已改、
     其余 import 条目逐条相等；
  ② 每条 export：SerialOffset_new = SerialOffset_old + delta、SerialSize 不变；
  ③ export 数据逐字节不变（tail 复制 + 偏移断言）；
  ④ decode_bp 在补丁文件上重解：函数清单与字节码长度不变。
"""
import hashlib
import json
import pathlib
import struct
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
STEAM_CONTENT = (ROOT / "427" / "assets_drop" / "steam_original" / "extracted" / "steamapps"
                 / "common" / "Warhammer 40,000 Deathwatch - Tyranid Invasion" / "deathwatchgame" / "Content")
sys.path.insert(0, str(ROOT / "tools" / "cooked_reader"))
import ue4prop  # noqa: E402

GAME = STEAM_CONTENT


def parse_summary_pos(d):
    """同 ue4prop.parse_summary 的字段序，但记录每个可补丁字段的绝对偏移。"""
    p = 0
    pos = {}
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
    assert tag == 0x9E2A83C1, "bad package tag"
    legacy = i32()
    assert -7 <= legacy <= -1
    if legacy != -4:
        i32()
    ver = u32(); lic = u32()
    if legacy <= -2:
        n = i32()
        p += n * 20
    pos["headers_size"] = p; hs = u32()
    fstr()
    pos["package_flags"] = p; pf = u32()
    pos["name_count"] = p; nc = u32()
    pos["name_offset"] = p; no = u32()
    pos["export_count"] = p; ec = u32()
    pos["export_offset"] = p; eo = u32()
    pos["import_count"] = p; ic = u32()
    pos["import_offset"] = p; io = u32()
    pos["depends_offset"] = p; dep = u32()
    pos["sar_count"] = p; sarc = u32()
    pos["sar_offset"] = p; saro = u32()
    pos["thumbnail_offset"] = p; thumb = i32()
    assert 413 >= 384
    p += 16                       # guid
    gc = i32()
    p += gc * 8                   # generations: ExportCount+NameCount（无文件偏移）
    p += 6                        # FEngineVersion major/minor/patch
    fstr()                        # branch
    p += 4                        # changelist
    u32()                         # compression flags
    nch = u32()
    assert nch == 0, "compressed package"
    u32()                         # package source
    napc = u32()
    for _ in range(napc):
        fstr()
    u32()                         # num texture allocations
    # 尾块（常见 20B）：[sar_offset 副本][文件长-4（trailer 偏移）][0][0][0]
    # 变体（如 WT_T2_Poweraxe.uasset）：首字段不是 sar_offset，但第二字段仍是 len(d)-4。
    # 真正需要随变长更新的是文件长字段（t1）；t0 只作诊断，不作硬断言，否则合法变体被拒。
    # 写后仍由 asset_text_lexicon.verify 严格回读校验。
    tail_start = p
    tail = d[p:no] if no > p else b""
    tail1_pos = None
    if len(tail) == 20:
        t0, t1 = struct.unpack_from("<i", tail, 0)[0], struct.unpack_from("<i", tail, 4)[0]
        assert t1 == len(d) - 4, \
            "summary 尾块文件长字段不符（tail1=%s size=%d）" % (t1, len(d))
        tail1_pos = tail_start + 4
    return dict(pos=pos, tail1_pos=tail1_pos, headers_size=hs, package_flags=pf, name_count=nc, name_offset=no,
                export_count=ec, export_offset=eo, import_count=ic, import_offset=io,
                depends_offset=dep, sar_count=sarc, sar_offset=saro, thumbnail_offset=thumb)


def read_names(d, off, count):
    names, ends, pos = [], [], off
    for _ in range(count):
        n = struct.unpack_from("<i", d, pos)[0]
        if n == 0:
            s = ""
            pos += 4
        elif n > 0:
            s = d[pos + 4:pos + 4 + n - 1].decode("utf-8")
            pos += 4 + n
        else:
            raise RuntimeError("unexpected UTF-16 name entry")
        names.append(s)
        ends.append((pos, n))     # (entry end, stored len)
    return names, pos


def enc_name(s):
    b = s.encode("utf-8") + b"\x00"
    return struct.pack("<i", len(b)) + b


def _shift_summary_tables(out, s, delta):
    """name 表增删后，summary 里的表偏移/HeadersSize/尾块同步平移（retarget 共用）。"""
    P = s["pos"]
    for k in ("import_offset", "export_offset", "depends_offset", "sar_offset"):
        if s[k] >= s["name_offset"]:
            struct.pack_into("<i", out, P[k], s[k] + delta)
    if s["thumbnail_offset"] >= s["name_offset"]:
        struct.pack_into("<i", out, P["thumbnail_offset"], s["thumbnail_offset"] + delta)
    struct.pack_into("<i", out, P["headers_size"], s["headers_size"] + delta)
    if s.get("tail1_pos"):
        struct.pack_into("<i", out, s["tail1_pos"], len(out) - 4)
        t0p = s["name_offset"] - 20
        if struct.unpack_from("<i", out, t0p)[0] == s["sar_offset"]:
            struct.pack_into("<i", out, t0p, s["sar_offset"] + delta)


def insert_cdo_int_property(rel, prop_name, value):
    """向 export `Default__<Base>_C` 的属性流末尾（None 终止符前）插入
    `<prop_name> = <value> (IntProperty)`，并按需向 name 表追加 prop_name。

    413-era cooked 布局（实测 BUFF_Plus2AP_1Turn 的 TurnsLeft tag）：
      tag = name(idx,0) type(idx,0) size(int32=4) arrayidx(int32=0) value(int32) = 28B
      CDO 数据 = [props...][None 终止符 8B]；None 终止符 = (names['None'], 0)。
    平移规则：name 增长 ⇒ 其后全部表平移；CDO 数据内插入 ⇒ SerialOffset > 插入点的
    export 各 +28，CDO 自身 SerialSize +28；尾块 trailer 偏移 +总增量。
    """
    src = GAME / (rel + ".uasset")
    d = src.read_bytes()
    s = parse_summary_pos(d)
    names, nend = read_names(d, s["name_offset"], s["name_count"])
    assert nend == s["import_offset"], "name chain end != ImportOffset"
    assert s["import_offset"] + 28 * s["import_count"] == s["export_offset"], "import chain end != ExportOffset"

    cdo_name = "Default__" + rel.rsplit("/", 1)[1] + "_C"
    cdo_idx, cdo_soff, cdo_ssize = None, None, None
    for i in range(s["export_count"]):
        w = struct.unpack_from("<17i", d, s["export_offset"] + i * 68)
        if w[4] == 0 and 0 <= w[3] < len(names) and names[w[3]] == cdo_name:
            cdo_idx, cdo_soff, cdo_ssize = i, w[7], w[6]
            break
    assert cdo_idx is not None, "找不到 CDO export " + cdo_name

    none_idx = names.index("None")
    term_pos = cdo_soff + cdo_ssize - 8
    assert struct.unpack_from("<ii", d, term_pos) == (none_idx, 0), \
        "CDO 数据尾 8B 不是 None 终止符（布局假设不成立）"

    assert "IntProperty" in names, "IntProperty 不在 name 表"
    add = [prop_name] if prop_name not in names else []
    new_names = names + add
    idx_prop = new_names.index(prop_name)
    idx_ip = names.index("IntProperty")

    old_nt = d[s["name_offset"]:s["import_offset"]]
    new_nt = b"".join(enc_name(x) for x in new_names)
    delta_nt = len(new_nt) - len(old_nt)

    tag = struct.pack("<6i", idx_prop, 0, idx_ip, 0, 4, 0) + struct.pack("<i", value)

    out = bytearray(d[:s["name_offset"]] + new_nt + d[s["import_offset"]:])

    # stage-1：name 增长平移
    P = s["pos"]
    struct.pack_into("<i", out, P["name_count"], len(new_names))
    _shift_summary_tables(out, s, delta_nt)

    # stage-2：CDO 数据内插入 tag
    ins_abs = term_pos + delta_nt
    assert struct.unpack_from("<ii", out, ins_abs) == (none_idx, 0), "stage-1 后插入点错位"
    out = bytearray(bytes(out[:ins_abs]) + tag + bytes(out[ins_abs:]))
    total = delta_nt + len(tag)

    # export 记录补丁：stage-1 name 增长平移所有 export 数据起点；
    # stage-2 数据内插入再平移起点在插入点之后的 export，CDO 自身 SerialSize 增长
    new_eo = s["export_offset"] + delta_nt
    for i in range(s["export_count"]):
        base = new_eo + i * 68
        so = struct.unpack_from("<i", out, base + 28)[0]     # 仍为旧值
        if i == cdo_idx:
            struct.pack_into("<i", out, base + 24, cdo_ssize + len(tag))
            struct.pack_into("<i", out, base + 28, so + delta_nt)
        elif so > term_pos:
            struct.pack_into("<i", out, base + 28, so + delta_nt + len(tag))
        else:
            struct.pack_into("<i", out, base + 28, so + delta_nt)
    # HeadersSize = 首 export SerialOffset，按最终值重写
    first_so = struct.unpack_from("<i", out, new_eo + 28)[0]
    struct.pack_into("<i", out, P["headers_size"], first_so)
    # 尾块 trailer 偏移 = 文件长 - 4（含 stage-2 增量）
    if s.get("tail1_pos"):
        struct.pack_into("<i", out, s["tail1_pos"], len(d) - 4 + total)

    return dict(src=d, dst=bytes(out), delta_nt=delta_nt, tag_len=len(tag),
                total=total, cdo_idx=cdo_idx, cdo_name=cdo_name,
                add_names=add, prop=prop_name, value=value,
                src_sha=hashlib.sha256(d).hexdigest(),
                dst_sha=hashlib.sha256(bytes(out)).hexdigest(),
                name_count_old=s["name_count"], name_count_new=len(new_names))


def raw_cdo_tags(d, names, soff, ssize):
    """裸字节走查 CDO 属性流（不依赖 walker），返回 [(name, type, size, tag_off, value)]。"""
    pos, end = soff, soff + ssize - 8
    out = []
    while pos < end:
        ni, nn, ti, tn, sz, ai = struct.unpack_from("<6i", d, pos)
        nname = names[ni] if nn == 0 else "%s_%d" % (names[ni], nn)
        tname = names[ti] if tn == 0 else "%s_%d" % (names[ti], tn)
        val = struct.unpack_from("<i", d, pos + 24)[0] if tname in ("IntProperty", "ObjectProperty") \
            else d[pos + 24:pos + 24 + sz]
        out.append((nname, tname, sz, pos, val))
        pos += 24 + sz
    assert pos == end, "属性流未恰好终止于 None 前（end 差 %d）" % (end - pos)
    return out


def verify_insert(rel, r):
    """insert_cdo_int_property 的 L0 验证（裸字节走查，不依赖 walker）。"""
    ue4prop.GAME = GAME
    d0, d1 = r["src"], r["dst"]
    s0 = parse_summary_pos(d0)
    n0, _ = read_names(d0, s0["name_offset"], s0["name_count"])
    s1 = parse_summary_pos(d1)
    n1, nend = read_names(d1, s1["name_offset"], s1["name_count"])
    assert nend == s1["import_offset"], "补丁后 name 链未对齐 ImportOffset"
    if r["add_names"]:
        assert n1[len(n0):] == r["add_names"], "追加 name 不在表尾"
    for i in range(s0["import_count"]):
        a = d0[s0["import_offset"] + i * 28: s0["import_offset"] + (i + 1) * 28]
        b = d1[s1["import_offset"] + i * 28: s1["import_offset"] + (i + 1) * 28]
        assert a == b, "import 条目 %d 被改动" % i

    def find_cdo(s, names, d):
        for i in range(s["export_count"]):
            w = struct.unpack_from("<17i", d, s["export_offset"] + i * 68)
            if w[4] == 0 and names[w[3]] == r["cdo_name"]:
                return i, w[6], w[7]
        raise RuntimeError("CDO 缺失")

    i0, sz0, off0 = find_cdo(s0, n0, d0)
    i1, sz1, off1 = find_cdo(s1, n1, d1)
    assert i0 == i1 == r["cdo_idx"], "CDO export 序号变化"
    assert sz1 == sz0 + r["tag_len"], "CDO SerialSize 未增长"
    t0 = raw_cdo_tags(d0, n0, off0, sz0)
    t1 = raw_cdo_tags(d1, n1, off1, sz1)
    added = [t for t in t1 if t[0] == r["prop"]]
    assert len(added) == 1 and added[0][1] == "IntProperty" and added[0][4] == r["value"], \
        "补丁后 CDO 未读出 %s=%s" % (r["prop"], r["value"])
    keep1 = [x for x in t1 if x[0] != r["prop"]]
    assert [x[:3] for x in t0] == [x[:3] for x in keep1], "既有属性清单变化"
    for a, b in zip(t0, keep1):
        assert d0[a[3]:a[3] + 24 + a[2]] == d1[b[3]:b[3] + 24 + b[2]], "既有属性字节被改动"
    for i in range(s0["export_count"]):
        if i == r["cdo_idx"]:
            continue
        w0 = struct.unpack_from("<17i", d0, s0["export_offset"] + i * 68)
        w1 = struct.unpack_from("<17i", d1, s1["export_offset"] + i * 68)
        assert w0[6] == w1[6], "export %d SerialSize 变化" % i
        assert d0[w0[7]:w0[7] + w0[6]] == d1[w1[7]:w1[7] + w1[6]], "export %d 数据被改动" % i
    # decode_bp：函数清单不变
    import importlib.util, shutil, tempfile
    spec = importlib.util.spec_from_file_location("decode_bp", str(ROOT / "427/Tools/bp_decode/decode_bp.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    orig_game = m.ue4prop.GAME
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        fns = {}
        for tag_, blob in (("old", d0), ("new", d1)):
            fdir = tmp / tag_ / rel.rsplit("/", 1)[0]
            fdir.mkdir(parents=True, exist_ok=True)
            (tmp / tag_ / (rel + ".uasset")).write_bytes(blob)
            m.ue4prop.GAME = tmp / tag_
            fns[tag_] = [(f["name"], f.get("bytecode_len")) for f in m.decode_pkg(rel)["functions"]]
        assert fns["old"] == fns["new"], "函数清单/字节码长度变化"
    finally:
        m.ue4prop.GAME = orig_game
        shutil.rmtree(tmp, ignore_errors=True)
    return True


def retarget_import(rel, old_obj, new_base):
    src = GAME / (rel + ".uasset")
    d = src.read_bytes()
    s = parse_summary_pos(d)
    names, nend = read_names(d, s["name_offset"], s["name_count"])
    assert nend == s["import_offset"], "name chain end != ImportOffset"
    assert s["import_offset"] + 28 * s["import_count"] == s["export_offset"], "import chain end != ExportOffset"

    new_cls = new_base + "_C"
    old_cls = old_obj
    old_pkg = "/Game/" + rel.rsplit("/", 1)[0] + "/" + old_obj.replace("_C", "")
    new_pkg = "/Game/" + rel.rsplit("/", 1)[0] + "/" + new_base

    # 定位要改的 import 条目
    targets = []  # (record_index, field="obj_i", new_name)
    for i in range(s["import_count"]):
        base = s["import_offset"] + i * 28
        cpkg_i, cpkg_n, cls_i, cls_n, pkg_idx, obj_i, obj_n = struct.unpack_from("<7i", d, base)
        oname = names[obj_i] if obj_n == 0 else None
        if oname == old_cls:
            targets.append((i, new_cls))
        elif oname == old_pkg:
            targets.append((i, new_pkg))
    if not targets:
        raise RuntimeError("import 表里找不到 %s / %s" % (old_cls, old_pkg))

    # 新 name 条目（已存在则复用 ⇒ delta 可能为 0）
    add = []
    for nm in (new_cls, new_pkg):
        if nm not in names:
            add.append(nm)
    new_names = names + add
    idx_of = {nm: new_names.index(nm) for nm in (new_cls, new_pkg)}

    # 重建 name 表
    old_nt = d[s["name_offset"]:s["import_offset"]]
    new_nt = b"".join(enc_name(x) for x in new_names)
    delta = len(new_nt) - len(old_nt)

    out = bytearray(d[:s["name_offset"]] + new_nt + d[s["import_offset"]:])

    # summary 字段补丁
    P = s["pos"]
    struct.pack_into("<i", out, P["name_count"], len(new_names))
    for k in ("import_offset", "export_offset", "depends_offset", "sar_offset"):
        v = s[k]
        if v >= s["import_offset"]:
            struct.pack_into("<i", out, P[k], v + delta)
    if s["thumbnail_offset"] >= s["import_offset"]:
        struct.pack_into("<i", out, P["thumbnail_offset"], s["thumbnail_offset"] + delta)
    struct.pack_into("<i", out, P["headers_size"], s["headers_size"] + delta)
    if r_tail1 := s.get("tail1_pos"):
        struct.pack_into("<i", out, r_tail1, len(d) - 4 + delta)   # trailer 偏移平移
    if len(d[s["name_offset"]:s["import_offset"]]) and s.get("tail1_pos"):
        # 尾块首字 = sar_offset 副本，同样平移
        t0p = s["name_offset"] - 20
        if struct.unpack_from("<i", out, t0p)[0] == s["sar_offset"]:
            struct.pack_into("<i", out, t0p, s["sar_offset"] + delta)

    # import 条目重指向（注意 import_offset 已平移）
    new_io = s["import_offset"] + delta
    for ri, newnm in targets:
        base = new_io + ri * 28
        struct.pack_into("<i", out, base + 20, idx_of[newnm])   # obj_i @ 第 6 个 int32

    # export SerialOffset 平移
    new_eo = s["export_offset"] + delta
    esz = (s["depends_offset"] - s["export_offset"]) // s["export_count"] if s["export_count"] else 0
    for i in range(s["export_count"]):
        base = new_eo + i * esz
        old_so = struct.unpack_from("<i", out, base + 28)[0]
        struct.pack_into("<i", out, base + 28, old_so + delta)

    return dict(src=d, dst=bytes(out), delta=delta, targets=targets,
                old_cls=old_cls, new_cls=new_cls, old_pkg=old_pkg, new_pkg=new_pkg,
                add_names=add, src_sha=hashlib.sha256(d).hexdigest(),
                dst_sha=hashlib.sha256(bytes(out)).hexdigest(),
                import_count=s["import_count"], export_count=s["export_count"],
                esz=esz, name_count_old=s["name_count"], name_count_new=len(new_names))


def verify(rel, r):
    ue4prop.GAME = GAME
    t_old = ue4prop.tables_from_file(r["src"], rel)
    t_new = ue4prop.tables_from_file(r["dst"], rel)
    so, names_o, imps_o, exps_o, _ = t_old
    sn, names_n, imps_n, exps_n, _ = t_new
    assert len(names_n) == r["name_count_new"], "name count mismatch"
    assert len(imps_n) == len(imps_o), "import count mismatch"
    # import 逐条对比：仅目标条目的 object_name 变化
    changed = []
    tgt_recs = {ri for ri, _ in r["targets"]}
    for i, (a, b) in enumerate(zip(imps_o, imps_n)):
        same = (a["class_package"] == b["class_package"] and a["class_name"] == b["class_name"]
                and a["package_index"] == b["package_index"] and a["object_name"] == b["object_name"])
        if not same:
            changed.append(i)
    assert set(changed) == tgt_recs, "import 变化集 %s != 目标 %s" % (changed, sorted(tgt_recs))
    for i in tgt_recs:
        assert imps_n[i]["object_name"] in (r["new_cls"], r["new_pkg"]), "目标 import 未改对"
    # export 偏移/大小
    assert len(exps_n) == len(exps_o)
    for a, b in zip(exps_o, exps_n):
        assert b["serial_offset"] == a["serial_offset"] + r["delta"], "SerialOffset 平移错误"
        assert b["serial_size"] == a["serial_size"], "SerialSize 变化"
    # export 数据逐字节不变
    for a, b in zip(exps_o, exps_n):
        assert r["src"][a["serial_offset"]:a["serial_offset"] + a["serial_size"]] == \
               r["dst"][b["serial_offset"]:b["serial_offset"] + b["serial_size"]], "export 数据被改动"
    # decode 复解：函数清单不变（src/dst 各自放独立临时目录）
    import importlib.util, shutil, tempfile
    spec = importlib.util.spec_from_file_location("decode_bp", str(ROOT / "427/Tools/bp_decode/decode_bp.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    orig_game = m.ue4prop.GAME
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        outs = {}
        for tag, blob in (("new", r["dst"]), ("old", r["src"])):
            fdir = tmp / tag / rel.rsplit("/", 1)[0]
            fdir.mkdir(parents=True, exist_ok=True)
            (tmp / tag / (rel + ".uasset")).write_bytes(blob)
            m.ue4prop.GAME = tmp / tag
            out = m.decode_pkg(rel)
            outs[tag] = [(f["name"], f.get("bytecode_len")) for f in out["functions"]]
        assert outs["old"] == outs["new"], "函数清单/字节码长度变化"
    finally:
        m.ue4prop.GAME = orig_game
        shutil.rmtree(tmp, ignore_errors=True)
    return True


def main(argv):
    rel = "Characters/Buffs/BUFF_Plus20DamageMitgation_NonStacking"
    old_obj = "BUFF_Plus20Damage_1Turn_C"
    new_base = "BUFF_20DamageMitigation_1Turn"
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "check":
        r = retarget_import(rel, old_obj, new_base)
        print("== %s" % rel)
        print("   import 改动条目: %s" % r["targets"])
        print("   %s -> %s" % (r["old_cls"], r["new_cls"]))
        print("   %s -> %s" % (r["old_pkg"], r["new_pkg"]))
        print("   新增 name 条目: %s" % r["add_names"])
        print("   name 表增量 delta=%d B（正=变长插入）" % r["delta"])
        print("   src %d B sha %s" % (len(r["src"]), r["src_sha"][:16]))
        print("\nCHECK PASS")
        return 0
    if cmd == "build":
        r = retarget_import(rel, old_obj, new_base)
        verify(rel, r)
        rel_file = "deathwatchgame/Content/" + rel + ".uasset"
        pkg = ROOT / "steam patching" / "patches" / "bugfix_praetorian_mitigation"
        dst_path = pkg / "assets" / rel_file
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst_path.write_bytes(r["dst"])
        manifest = dict(
            name="bugfix_praetorian_mitigation",
            description=("OB-13 语义修复（Praetorian 文实不符）：ABP_Praetorian 授予的 "
                         "BUFF_Plus20DamageMitgation_NonStacking 内部把 "
                         "BUFF_Plus20Damage_1Turn（+20% 伤害）当作效果载体，"
                         "与描述「+20% Damage Mitigation」不符。变长改写 name 表"
                         "（新增 2 条 name）并把两个 import 条目重指向 "
                         "BUFF_20DamageMitigation_1Turn（PercentageMitigation=0.2、TurnsLeft=1，"
                         "语义精确匹配）；AddBuff/HasBuff 的 EX_ObjectConst 按 import 索引引用，自动跟随。"),
            source="Steam 干净基准 Build 1837603（就地改，不引用 refs/PC_EE 任何资产）",
            grading=("L0：重建后复析（name 数/import 逐条/export SerialOffset+Size/数据逐字节/"
                     "decode_bp 函数清单）全过。L2/L3 未验证。"),
            install_note=("首次使用变长 uasset 编辑器出包；实机若加载 Fatal 需回滚并检查"
                          "（BFL 变长 Fatal 先例在此不适用——本次不改字节码，只平移表）。"),
            entries=[dict(relative_path=rel_file, dst_sha256=r["dst_sha"],
                          src_sha256=r["src_sha"], size=len(r["dst"]), differs_from_steam=True)],
            detail=[dict(asset=rel, what="import 重指向 + name 表 +2 条（变长）",
                         delta_bytes=r["delta"])],
        )
        (pkg / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
                                           encoding="utf-8")
        print("BUILD OK -> %s" % pkg)
        print("  %-72s %d B  %s  (delta=%d)" % (rel_file, len(r["dst"]), r["dst_sha"][:16], r["delta"]))
        return 0
    raise SystemExit("usage: uasset_mutate.py [check|build]")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
