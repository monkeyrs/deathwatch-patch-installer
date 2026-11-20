# -*- coding: utf-8 -*-
"""Installer-side Chinese runtime generation package: Chinese data comes from zh_runtime.json (exported from Excel at build time).

The four chains correspond one-to-one with the generation tools in the repo:
  locres      -> locres_v3.parse_any / write_v3 / write_legacy
  FlairName   -> flairname_lexicon.transform_variable / static_verify
  asset_text  -> asset_text_lexicon.find_prop / transform / verify
  BFL         -> bfl_display_string_patch.load / find_targets / assemble / validate

Every write first backs up to backup_dir/files/<relative path>, then uses a temp file + readback check + os.replace.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import struct

import asset_text_lexicon as atl
import bfl_display_string_patch as bfl
import flairname_lexicon as fll
import locres_v3
import ue4prop


def load_data(path: pathlib.Path) -> dict:
    import json
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _rel(root: pathlib.Path, p: pathlib.Path) -> str:
    return p.relative_to(root).as_posix()


def _safe_rel(rel: str) -> pathlib.PurePosixPath:
    p = pathlib.PurePosixPath(str(rel).replace("\\", "/"))
    if not p.parts or p.is_absolute() or ".." in p.parts:
        raise RuntimeError(f"非法相对路径: {rel!r}")
    if p.parts[0].endswith(":"):
        raise RuntimeError(f"非法相对路径: {rel!r}")
    return p


def _safe_join(base: pathlib.Path, rel: str) -> pathlib.Path:
    return pathlib.Path(base).joinpath(*_safe_rel(rel).parts)


class _Backup:
    def __init__(self, root: pathlib.Path, backup_dir: pathlib.Path):
        self.root = root
        self.dir = pathlib.Path(backup_dir) / "files"
        self.saved: set[str] = set()

    def save(self, path: pathlib.Path) -> None:
        rel = _rel(self.root, path)
        if rel in self.saved:
            return
        dst = self.dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            dst.write_bytes(path.read_bytes())
        self.saved.add(rel)

    @staticmethod
    def write_atomic(path: pathlib.Path, data: bytes) -> None:
        tmp = path.with_name(path.name + ".dwtmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)


def _result(chain: str, **kw) -> dict:
    out = {"chain": chain, "ok": True, "applied": 0, "skipped": 0,
           "errors": [], "detail": ""}
    out.update(kw)
    return out


def apply_locres(content: pathlib.Path, rows: list[dict], backup: _Backup,
                 log=print) -> dict:
    target = content / "Localization" / "Game" / "en" / "Game.locres"
    res = _result("locres")
    if not target.is_file():
        res.update(ok=False, errors=[f"缺少 {target}"])
        return res
    raw = target.read_bytes()
    is_v3 = raw[:16] == struct.pack("<4I", *locres_v3.MAGIC)
    parsed = locres_v3.parse_any(target)
    entries = list(parsed.entries)
    table = {(r["ns"], r["key"]): r for r in rows}
    applied = skipped = drift = 0
    for i, (ns, key, src_hash, val) in enumerate(entries):
        r = table.pop((ns, key), None)
        if r is None:
            continue
        if int(r["hash"]) != src_hash:
            drift += 1
            res["errors"].append(f"hash 漂移，跳过 {ns}/{key}")
            continue
        if r["zh"] == val:
            skipped += 1
            continue
        entries[i] = (ns, key, src_hash, r["zh"])
        applied += 1
    absent = len(table)
    if not applied:
        res.update(skipped=skipped,
                   detail=f"已是最新（{skipped} 条）；未命中 {absent} 条")
        return res
    backup.save(target)
    tmp = target.with_name("Game.locres.dwgen")
    writer = locres_v3.write_v3 if is_v3 else locres_v3.write_legacy
    writer(entries, tmp)
    chk = locres_v3.parse_any(tmp)
    if len(chk.entries) != len(entries):
        tmp.unlink(missing_ok=True)
        raise RuntimeError("locres 回读条数不符")
    a = {(n, k) for n, k, _h, _v in entries}
    b = {(n, k) for n, k, _h, _v in chk.entries}
    if a != b:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("locres 回读 namespace/key 集合不符")
    _Backup.write_atomic(target, tmp.read_bytes())
    tmp.unlink(missing_ok=True)
    res.update(applied=applied, skipped=skipped,
               detail=f"写入 {applied} 条，已是中文 {skipped} 条，"
                      f"未命中 {absent} 条，hash 漂移 {drift} 条")
    log(f"[zh] locres: {res['detail']}")
    return res


def _has_cjk(data: bytes) -> bool:
    """Loosely detect UTF-16LE Chinese (used for idempotency checks on already-patched assets)."""
    for i in range(0, max(0, len(data) - 1), 2):
        if 0x4E <= data[i + 1] <= 0x9F:
            return True
    return False


def apply_flairname(content: pathlib.Path, mapping: dict, backup: _Backup,
                    log=print) -> dict:
    res = _result("flairname")
    if not mapping:
        return res
    ue4prop.GAME = content
    fll.ORIG_CONTENT = content
    zh_values = set(mapping.values())
    base = content / fll.PKG_REL_DIR
    files = sorted(base.glob("UTP_*.uasset")) if base.is_dir() else []
    applied = skipped = 0
    for src in files:
        try:
            r = ue4prop.read_package(f"{fll.PKG_REL_DIR}/{src.stem}")
            flair = None
            for w in r.get("walks", []):
                for p in w.get("props", []):
                    if isinstance(p, dict) and p.get("name") == "FlairName":
                        flair = p.get("value", {})
            if flair is None:
                continue
            en = str(flair.get("source", ""))
            if en in zh_values:
                skipped += 1
                continue
            zh = mapping.get(hashlib.sha256(en.encode("utf-8")).hexdigest()) \
                or mapping.get(en)
            if not zh:
                continue
            data = src.read_bytes()
            new_data, _info = fll.transform_variable(data, r, zh)
            n_props = sum(len(w.get("props", [])) for w in r.get("walks", []))
            fll.static_verify(new_data, src.stem, zh, n_props)
            backup.save(src)
            _Backup.write_atomic(src, new_data)
            applied += 1
        except Exception as exc:                  # noqa: BLE001
            try:
                done = _has_cjk(src.read_bytes())
            except OSError:
                done = False
            if done:
                skipped += 1
            else:
                res["errors"].append(f"{src.name}: {exc}")
    res.update(applied=applied, skipped=skipped, ok=not res["errors"],
               detail=f"改写 {applied} 个资产，已中文 {skipped} 个，失败 {len(res['errors'])} 个")
    log(f"[zh] FlairName: {res['detail']}")
    return res


def apply_asset_text(content: pathlib.Path, rows: list[dict], backup: _Backup,
                     log=print) -> dict:
    res = _result("asset_text")
    if not rows:
        return res
    ue4prop.GAME = content
    atl.ORIG_CONTENT = content
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["asset_file"], []).append(r)
    applied = skipped = 0
    for rel, items in groups.items():
        try:
            rel = _safe_rel(rel).as_posix()
            path = _safe_join(content, rel)
        except Exception as exc:                  # noqa: BLE001
            res["errors"].append(f"{rel}: {exc}")
            continue
        if not path.is_file():
            res["errors"].append(f"{rel}: 文件不存在")
            continue
        try:
            for r in items:
                data = path.read_bytes()
                parsed, _w, p = atl.find_prop(rel, r["key"])
                sb = atl.source_bounds(data, p)
                if sb["src"] == r["zh"]:
                    skipped += 1
                    continue
                dst, info, _s, _ei = atl.transform(
                    data, parsed, p, r["key"], None, r["zh"])
                atl.verify(dst, rel, r["key"], r["zh"], data, parsed, info)
                backup.save(path)
                _Backup.write_atomic(path, dst)
                applied += 1
        except Exception as exc:                  # noqa: BLE001
            res["errors"].append(f"{rel}: {exc}")
    res.update(applied=applied, skipped=skipped, ok=not res["errors"],
               detail=f"改写 {applied} 处，已中文 {skipped} 处，失败 {len(res['errors'])} 处")
    log(f"[zh] asset_text: {res['detail']}")
    return res


def apply_bfl(content: pathlib.Path, mapping: dict, backup: _Backup,
              log=print) -> dict:
    res = _result("bfl")
    if not mapping:
        return res
    try:
        r, raw, ctx = bfl.load(bfl.REL, game=content)
        if not r.get("exports") or not r.get("names"):
            raise RuntimeError("BFL 资产解析失败（无可读导出/名称表）")
        targets = bfl.find_targets(r, raw, ctx, mapping)
        if not targets:
            res.update(skipped=1, detail="已是中文（未发现 0x1F 可翻串）")
            return res
        applied_src, applied_zh = [], []
        for _e, _bc, _info, _new_bc, _delta, rep in targets:
            applied_src.extend(rep.get("applied") or [])
            for src in rep.get("applied") or []:
                key = src.lower()
                zh = (mapping.get(hashlib.sha256(key.encode("utf-8")).hexdigest())
                      or mapping.get(key))
                if zh:
                    applied_zh.append(zh)
        new_raw, _funcs = bfl.assemble(raw, r, targets)
        problems, _seen = bfl.validate(new_raw, set(x.lower() for x in applied_src),
                                       applied_zh)
        if problems:
            raise RuntimeError("; ".join(problems[:3]))
        target = content / (bfl.REL + ".uasset")
        backup.save(target)
        _Backup.write_atomic(target, new_raw)
        res.update(applied=len(applied_zh),
                   detail=f"改写 {len(applied_zh)} 条显示名")
        log(f"[zh] BFL: {res['detail']}")
        return res
    except Exception as exc:                      # noqa: BLE001
        res.update(ok=False, errors=[str(exc)], detail="BFL 生成失败")
        log(f"[zh] BFL 失败: {exc}")
        return res


def target_rels(root: pathlib.Path, data: dict) -> list[str]:
    """Relative paths that the Chinese chains rewrite (used to pre-restore selected features before reapplying)."""
    root = pathlib.Path(root)
    content = root / "deathwatchgame" / "Content"
    rels: set[str] = {
        "deathwatchgame/Content/Localization/Game/en/Game.locres",
        "deathwatchgame/Content/" + bfl.REL + ".uasset",
    }
    base = content / fll.PKG_REL_DIR
    if base.is_dir():
        rels.update(p.relative_to(root).as_posix()
                    for p in base.glob("UTP_*.uasset"))
    for row in data.get("asset_text") or []:
        rel = str(row.get("asset_file") or "").replace("\\", "/")
        if not rel:
            continue
        try:
            rel = _safe_rel(rel).as_posix()
        except Exception:
            continue
        if not rel.startswith("deathwatchgame/"):
            rel = "deathwatchgame/Content/" + rel
        rels.add(rel)
    return sorted(rels)


def apply_zh_text(root: pathlib.Path, data: dict, backup_dir: pathlib.Path,
                  log=print) -> list[dict]:
    """Generate Chinese in place: locres + FlairName + asset_text + BFL. Returns results per chain."""
    root = pathlib.Path(root)
    content = root / "deathwatchgame" / "Content"
    backup = _Backup(root, pathlib.Path(backup_dir))
    steps = (
        ("locres", lambda: apply_locres(content, data.get("locres") or [], backup, log)),
        ("flairname", lambda: apply_flairname(content, data.get("flairname") or {}, backup, log)),
        ("asset_text", lambda: apply_asset_text(content, data.get("asset_text") or [], backup, log)),
        ("bfl", lambda: apply_bfl(content, data.get("bfl") or {}, backup, log)),
    )
    results = []
    for name, fn in steps:
        try:
            results.append(fn())
        except Exception as exc:                  # noqa: BLE001
            results.append(_result(name, ok=False, errors=[str(exc)], detail="异常"))
            log(f"[zh] {name} 异常: {exc}")
    return results
