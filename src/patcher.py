# -*- coding: utf-8 -*-
"""Installer core engine: backup sessions, manifest package deployment, restore, and Black Shield save preflight/injection.

Backup layout:
    <安装根>/.dwpatch_backup/
        index.json            # Global earliest-backup index (files / created), used for cross-session restore
        <UTC 时间戳>/
            session.json      # This session's files + created
            files/<相对路径>   # Originals of overwritten files (may reference an earlier session's backup of the same path)
            save/dw_metagame.save # Save backup taken before Black Shield injection

All overwrites use a temporary file + os.replace; dst_sha256 from the manifest is always verified after writing.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import pathlib
import shutil
import sys

VERSION = "1.1.0"
BACKUP_DIR = ".dwpatch_backup"
INDEX_NAME = "index.json"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_atomic(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".dwtmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def safe_rel_path(rel: str) -> pathlib.PurePosixPath:
    """Constrain a relative path from a manifest/index to stay under the target root (reject absolute paths, drive letters, and ..)."""
    p = pathlib.PurePosixPath(str(rel).replace("\\", "/"))
    if not p.parts or p.is_absolute() or ".." in p.parts:
        raise RuntimeError(f"非法相对路径: {rel!r}")
    if p.parts[0].endswith(":"):
        raise RuntimeError(f"非法相对路径: {rel!r}")
    return p


def safe_join(base: pathlib.Path, rel: str) -> pathlib.Path:
    return pathlib.Path(base).joinpath(*safe_rel_path(rel).parts)


class BackupIndex:
    """Cross-session global backup index.

    `<游戏根>/.dwpatch_backup/index.json` records the earliest original file by `relative_path`:
        files:   {rel: {relative_path, sha256, size, backup_path}}
        created: [rel]   files created by the installer that were absent in the original (deleted on restore)
    Only the earliest backup is kept for a file installed multiple times; empty sessions are not indexed.
    """

    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.base = self.root / BACKUP_DIR
        self.path = self.base / INDEX_NAME
        self.files: dict[str, dict] = {}
        self.created: list[str] = []

    @classmethod
    def load(cls, root) -> "BackupIndex":
        obj = cls(root)
        try:
            data = json.loads(obj.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return obj
        if isinstance(data, dict):
            files = data.get("files")
            if isinstance(files, dict):
                obj.files = {str(k): v for k, v in files.items()
                             if isinstance(v, dict)}
            created = data.get("created")
            if isinstance(created, list):
                obj.created = [str(v) for v in created]
        return obj

    @classmethod
    def from_sessions(cls, root) -> "BackupIndex":
        """Rebuild the earliest-backup index in time order when only session.json files exist (legacy layout)."""
        obj = cls(root)
        if not obj.base.is_dir():
            return obj
        dirs = [d for d in obj.base.iterdir()
                if d.is_dir() and (d / "session.json").is_file()]
        for d in sorted(dirs, key=lambda p: p.name):
            try:
                data = json.loads((d / "session.json").read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            for e in data.get("files") or []:
                rel = e.get("relative_path") if isinstance(e, dict) else None
                if not rel or rel in obj.created or rel in obj.files:
                    continue
                obj.files[rel] = {
                    "relative_path": rel,
                    "sha256": e.get("sha256"),
                    "size": e.get("size"),
                    "backup_path": (e.get("backup_path")
                                    or f"{d.name}/files/{rel}"),
                }
            for rel in (data.get("created")
                        if isinstance(data.get("created"), list) else []):
                if rel in obj.files or rel in obj.created:
                    continue
                obj.created.append(rel)
        return obj

    def merge(self, other: "BackupIndex") -> bool:
        """Merge history from older sessions into the index, keeping existing entries at their earliest."""
        changed = False
        for rel, e in other.files.items():
            if rel in self.created or rel in self.files:
                continue
            self.files[rel] = e
            changed = True
        for rel in other.created:
            if rel in self.files or rel in self.created:
                continue
            self.created.append(rel)
            changed = True
        return changed

    def add_file(self, rel: str, sha256: str, size: int, backup_path: str) -> None:
        if rel in self.created or rel in self.files:
            return
        self.files[rel] = {"relative_path": rel, "sha256": sha256,
                           "size": size, "backup_path": backup_path}
        self.save()

    def add_created(self, rel: str) -> None:
        if rel in self.files or rel in self.created:
            return
        self.created.append(rel)
        self.save()

    def save(self) -> None:
        data = {
            "version": VERSION,
            "updated_utc": _dt.datetime.now(_dt.timezone.utc)
                              .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "files": self.files,
            "created": self.created,
        }
        write_atomic(self.path, json.dumps(data, ensure_ascii=False,
                                           indent=1).encode("utf-8"))


class Session:
    """One install/restore session: records overwritten and newly created files.

    Backups are still written per session, but which copy counts as the original is decided by the global
    BackupIndex: only the earliest backup is kept across installs, so a later install never treats a
    patched intermediate state as the original. When nothing actually changed, no session.json is created
    and no empty directories are left behind.
    """

    def __init__(self, root: pathlib.Path, stamp: str | None = None):
        self.root = pathlib.Path(root)
        stamp = stamp or _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.dir = self.root / BACKUP_DIR / stamp
        self.files_dir = self.dir / "files"
        self.entries: dict[str, dict] = {}
        self.created: list[str] = []
        self.index = BackupIndex.load(self.root)

    def backup(self, path: pathlib.Path) -> None:
        rel = path.relative_to(self.root).as_posix()
        if rel in self.entries or rel in self.created:
            return
        # This file was created by a previous install: its original state is "absent", so remove it on restore.
        if rel in self.index.created:
            self.created.append(rel)
            return
        # An earlier backup exists: reuse the earliest original instead of copying the current (possibly patched) content.
        entry = self.index.files.get(rel)
        if entry:
            self.entries[rel] = {"relative_path": rel,
                                 "sha256": entry.get("sha256"),
                                 "size": entry.get("size"),
                                 "backup_path": entry.get("backup_path")}
            return
        dst = self.files_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_file():
            # Retry with the same stamp / leftover backup from an older version: keep the earliest content.
            got = sha256_file(dst)
            size = dst.stat().st_size
        else:
            shutil.copy2(path, dst)
            got = sha256_file(dst)
            size = dst.stat().st_size
        self.entries[rel] = {"relative_path": rel, "sha256": got, "size": size,
                             "backup_path": f"{self.dir.name}/files/{rel}"}
        self.index.add_file(rel, got, size, f"{self.dir.name}/files/{rel}")

    def mark_created(self, path: pathlib.Path) -> None:
        rel = path.relative_to(self.root).as_posix()
        if rel in self.created or rel in self.entries:
            return
        # The index says this file existed in the original: even if currently missing, restore should write
        # the original back rather than delete it.
        entry = self.index.files.get(rel)
        if entry:
            self.entries[rel] = {"relative_path": rel,
                                 "sha256": entry.get("sha256"),
                                 "size": entry.get("size"),
                                 "backup_path": entry.get("backup_path")}
            return
        self.index.add_created(rel)
        self.created.append(rel)

    def finalize(self) -> pathlib.Path | None:
        # The Chinese text chain (zh_runtime) backs up into the same files/ directory itself, bypassing
        # Session.backup; here we register backups already in files/ but not yet listed, in the manifest and global index.
        if self.files_dir.is_dir():
            changed = False
            for p in sorted(self.files_dir.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(self.files_dir).as_posix()
                if rel in self.entries or rel in self.created:
                    continue
                if rel in self.index.created:
                    self.created.append(rel)
                    continue
                entry = self.index.files.get(rel)
                if entry:
                    self.entries[rel] = {"relative_path": rel,
                                         "sha256": entry.get("sha256"),
                                         "size": entry.get("size")}
                    continue
                got = sha256_file(p)
                size = p.stat().st_size
                self.entries[rel] = {"relative_path": rel,
                                     "sha256": got, "size": size,
                                     "backup_path": f"{self.dir.name}/files/{rel}"}
                self.index.files[rel] = {
                    "relative_path": rel, "sha256": got, "size": size,
                    "backup_path": f"{self.dir.name}/files/{rel}",
                }
                changed = True
            if changed:
                self.index.save()
        if not self.entries and not self.created:
            # No actual changes: do not leave an empty session (the Black Shield save backup lives separately under save/).
            try:
                if self.files_dir.is_dir() and not any(self.files_dir.iterdir()):
                    self.files_dir.rmdir()
                if self.dir.is_dir() and not any(self.dir.iterdir()):
                    self.dir.rmdir()
            except OSError:
                pass
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        data = {
            "version": VERSION,
            "created_utc": _dt.datetime.now(_dt.timezone.utc)
                              .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "root": str(self.root),
            "files": list(self.entries.values()),
            "created": self.created,
        }
        p = self.dir / "session.json"
        write_atomic(p, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
        return p


def new_session(root, stamp: str | None = None) -> Session:
    return Session(pathlib.Path(root), stamp)


def latest_session(root) -> pathlib.Path | None:
    base = pathlib.Path(root) / BACKUP_DIR
    if not base.is_dir():
        return None
    cands = sorted((d for d in base.iterdir()
                    if d.is_dir() and (d / "session.json").is_file()),
                   key=lambda d: d.name, reverse=True)
    return cands[0] if cands else None


def restore_session(root, session_dir) -> dict:
    """Restore overwritten files from session.json and delete files created in that session."""
    root = pathlib.Path(root)
    session_dir = pathlib.Path(session_dir)
    res = {"ok": True, "restored": 0, "deleted": 0, "errors": []}
    try:
        data = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    except Exception as exc:                      # noqa: BLE001
        return {"ok": False, "restored": 0, "deleted": 0,
                "errors": [f"session.json 读取失败: {exc}"]}
    for e in data.get("files") or []:
        rel = e.get("relative_path")
        try:
            src = safe_join(session_dir / "files", rel)
            if not src.is_file() and e.get("backup_path"):
                # This entry's original backup may live in an earlier session (global index reuse).
                src = safe_join(session_dir.parent, e["backup_path"])
            dst = safe_join(root, rel)
            if not src.is_file():
                raise FileNotFoundError(f"备份缺失: {src}")
            got = sha256_file(src)
            if e.get("sha256") and got != e["sha256"]:
                raise RuntimeError("备份文件校验失败")
            write_atomic(dst, src.read_bytes())
            res["restored"] += 1
        except Exception as exc:                  # noqa: BLE001
            res["errors"].append(f"{rel}: {exc}")
    for rel in data.get("created") or []:
        try:
            safe_join(root, rel).unlink(missing_ok=True)
            res["deleted"] += 1
        except Exception as exc:                  # noqa: BLE001
            res["errors"].append(f"{rel}: {exc}")
    res["ok"] = not res["errors"]
    return res


def restore_all(root) -> dict:
    """Restore to the pre-install state using the global index (earliest backup across installs).

    For legacy layouts with only session.json and no index.json, first rebuild the index from all sessions.
    Created files are always deleted; `found` lets the UI distinguish "no backup" from "restored 0 items".
    """
    root = pathlib.Path(root)
    idx = BackupIndex.load(root)
    if idx.merge(BackupIndex.from_sessions(root)):
        idx.save()
    save_backups: list[str] = []
    if idx.base.is_dir():
        for d in sorted((p for p in idx.base.iterdir() if p.is_dir()),
                        key=lambda p: p.name):
            sd = d / "save"
            if sd.is_dir():
                for f in sorted(sd.iterdir()):
                    if f.is_file():
                        save_backups.append(str(f))
    res = {"ok": True, "restored": 0, "deleted": 0, "errors": [],
           "found": bool(idx.files or idx.created or save_backups),
           "save_backups": save_backups}
    for rel, e in idx.files.items():
        try:
            backup_path = e.get("backup_path")
            if not backup_path:
                raise RuntimeError("索引缺 backup_path")
            src = safe_join(idx.base, backup_path)
            if not src.is_file():
                raise FileNotFoundError(f"备份缺失: {src}")
            got = sha256_file(src)
            want = e.get("sha256")
            if want and got != want:
                raise RuntimeError("备份文件校验失败")
            dst = safe_join(root, rel)
            write_atomic(dst, src.read_bytes())
            if want and sha256_file(dst) != want:
                raise RuntimeError("写回后 hash 校验失败")
            res["restored"] += 1
        except Exception as exc:                  # noqa: BLE001
            res["errors"].append(f"{rel}: {exc}")
    for rel in idx.created:
        try:
            safe_join(root, rel).unlink(missing_ok=True)
            res["deleted"] += 1
        except Exception as exc:                  # noqa: BLE001
            res["errors"].append(f"{rel}: {exc}")
    res["ok"] = not res["errors"]
    return res


def apply_equal_length_transform(data: bytes, transform: dict) -> bytes:
    """Apply an equal-length byte transform from manifest.transform (no full assets required)."""
    if not isinstance(transform, dict):
        raise ValueError("transform 不是对象")
    if transform.get("kind") != "equal_length_bytes":
        raise ValueError(f"不支持的 transform.kind: {transform.get('kind')!r}")
    buf = bytearray(data)
    for op in transform.get("operations") or []:
        off = int(op["offset"], 16)
        before = bytes.fromhex(op["before_hex"])
        after = bytes.fromhex(op["after_hex"])
        if not before or len(before) != len(after):
            raise ValueError(f"规则 {op.get('label', '?')} 不是等长变换")
        if off < 0 or off + len(before) > len(buf):
            raise ValueError(f"规则 {op.get('label', '?')} 越界")
        if bytes(buf[off:off + len(before)]) != before:
            raise RuntimeError(f"规则 {op.get('label', '?')} 前置字节不符")
        buf[off:off + len(after)] = after
    return bytes(buf)


def pre_restore_selected(root, rels, log=print) -> dict:
    """Before re-applying, restore files touched by the selected features to their earliest originals.

    Only writes back when the current content differs from the earliest backup; files with no backup or
    already matching are skipped. Typical case: install a lower visual tier, then pick the highest tier —
    restore DefaultEngine.ini first and let apply_manifest_package write the highest tier, instead of
    reporting "already patched".
    """
    root = pathlib.Path(root)
    idx = BackupIndex.load(root)
    if idx.merge(BackupIndex.from_sessions(root)):
        idx.save()
    res = {"ok": True, "restored": 0, "deleted": 0, "skipped": 0, "errors": []}
    for rel in sorted(set(str(x) for x in rels if x)):
        try:
            if rel in idx.created:
                dst = safe_join(root, rel)
                if dst.exists():
                    dst.unlink()
                    res["deleted"] += 1
                else:
                    res["skipped"] += 1
                continue
            e = idx.files.get(rel)
            if not e:
                res["skipped"] += 1
                continue
            backup_path = e.get("backup_path")
            if not backup_path:
                raise RuntimeError("索引缺 backup_path")
            src = safe_join(idx.base, backup_path)
            if not src.is_file():
                raise FileNotFoundError(f"备份缺失: {src}")
            dst = safe_join(root, rel)
            want = e.get("sha256")
            cur = sha256_file(dst) if dst.is_file() else None
            if want and cur == want:
                res["skipped"] += 1
                continue
            got = sha256_file(src)
            if want and got != want:
                raise RuntimeError("备份文件校验失败")
            write_atomic(dst, src.read_bytes())
            if want and sha256_file(dst) != want:
                raise RuntimeError("预还原后 hash 校验失败")
            res["restored"] += 1
        except Exception as exc:                  # noqa: BLE001
            res["errors"].append(f"{rel}: {exc}")
    res["ok"] = not res["errors"]
    log(f"[pre-restore] restored={res['restored']} deleted={res['deleted']} "
        f"skipped={res['skipped']} errors={len(res['errors'])}")
    return res


def apply_manifest_package(root, pkg_dir, session: Session, log=print) -> dict:
    """Deploy one patch package according to the entries in manifest.json."""
    root = pathlib.Path(root)
    pkg_dir = pathlib.Path(pkg_dir)
    res = {"ok": True, "applied": 0, "skipped": 0, "errors": [],
           "detail": ""}
    mf = pkg_dir / "manifest.json"
    try:
        meta = json.loads(mf.read_text(encoding="utf-8"))
    except Exception as exc:                      # noqa: BLE001
        res.update(ok=False, errors=[f"manifest 读取失败: {exc}"])
        return res
    entries = meta.get("entries") or []
    if not entries:
        res.update(ok=False, errors=[f"{pkg_dir.name}: manifest.entries 为空"])
        return res
    recipe = meta.get("recipe")
    if recipe:
        # New flow: the payload ships only a recipe; first copy the original backup, then compute the
        # target bytes on the retained original using the Python logic in zhgen.asset_recipes.
        try:
            from zhgen import asset_recipes
            content = root / "deathwatchgame" / "Content"
            for e in entries:
                rel = e.get("relative_path")
                dst = safe_join(root, rel)
                if not dst.is_file():
                    raise FileNotFoundError(f"目标文件缺失: {dst}")
                cur = sha256_file(dst)
                expect = e.get("src_sha256")
                if expect and cur != expect:
                    raise RuntimeError("目标文件不是预期原版（recipe 需先预还原）")
            outs = asset_recipes.compute_package(content, meta)
            for e in entries:
                rel = e.get("relative_path")
                want = e.get("dst_sha256")
                if want and sha256_bytes(outs[rel]) != want:
                    raise RuntimeError(f"{rel}: recipe 结果 hash 不符")
            for e in entries:
                rel = e.get("relative_path")
                dst = safe_join(root, rel)
                session.backup(dst)
                write_atomic(dst, outs[rel])
                if e.get("dst_sha256") and sha256_file(dst) != e["dst_sha256"]:
                    raise RuntimeError(f"{rel}: 写入后 hash 校验失败")
                res["applied"] += 1
            res["ok"] = True
            res["detail"] = f"recipe={recipe}，计算并写入 {res['applied']} 个文件"
            log(f"[pkg] {pkg_dir.name}: {res['detail']}")
            return res
        except Exception as exc:                  # noqa: BLE001
            res.update(ok=False, errors=[f"{type(exc).__name__}: {exc}"])
            res["detail"] = f"recipe={recipe} 失败: {exc}"
            log(f"[pkg] {pkg_dir.name}: {res['detail']}")
            return res
    for e in entries:
        rel = e.get("relative_path")
        try:
            if not rel:
                raise ValueError("entry 缺 relative_path")
            dst = safe_join(root, rel)
            want = e.get("dst_sha256")
            already_fixed = e.get("already_fixed_sha256") or []
            expect_src = e.get("src_sha256")
            acceptable = e.get("acceptable_src_sha256") or []
            transform = e.get("transform")
            if transform:
                # Only equal-length transforms are shipped; the installer payload contains no full locres.
                if not dst.is_file():
                    raise FileNotFoundError(f"目标文件缺失: {dst}")
                cur = sha256_file(dst)
                if want and cur == want:
                    res["skipped"] += 1
                    continue
                if cur in already_fixed:
                    res["skipped"] += 1
                    continue
                if expect_src and cur != expect_src and cur not in acceptable:
                    raise RuntimeError("目标文件不是预期原版（src_sha256 不符）")
                out = apply_equal_length_transform(dst.read_bytes(), transform)
                if want and sha256_bytes(out) != want:
                    raise RuntimeError("变换后 hash 不符")
                session.backup(dst)
                write_atomic(dst, out)
                if want and sha256_file(dst) != want:
                    raise RuntimeError("写入后 hash 校验失败")
                res["applied"] += 1
                continue
            src = safe_join(pkg_dir / "assets", rel)
            if not src.is_file():
                raise FileNotFoundError(f"包内资产缺失: {src}")
            src_hash = sha256_file(src)
            if want and src_hash != want:
                raise RuntimeError(f"包内资产 hash 不符（{src_hash[:12]} != {want[:12]}）")
            if dst.is_file() and want and sha256_file(dst) == want:
                res["skipped"] += 1
                continue
            if dst.is_file() and sha256_file(dst) in already_fixed:
                # This file was overwritten to an equivalent final state by a later layer (e.g. zh_cn),
                # so treat it as already fixed and do not write English back.
                res["skipped"] += 1
                continue
            cur = sha256_file(dst) if dst.is_file() else None
            if dst.is_file() and expect_src and cur != expect_src and cur not in acceptable:
                raise RuntimeError("目标文件不是预期原版（src_sha256 不符）")
            if dst.is_file():
                session.backup(dst)
            else:
                session.mark_created(dst)
            write_atomic(dst, src.read_bytes())
            if want and sha256_file(dst) != want:
                raise RuntimeError("写入后 hash 校验失败")
            res["applied"] += 1
        except Exception as exc:                  # noqa: BLE001
            res["errors"].append(f"{rel}: {exc}")
    res["ok"] = not res["errors"]
    res["detail"] = f"写入 {res['applied']} 个文件，已是最新 {res['skipped']} 个，失败 {len(res['errors'])} 个"
    log(f"[pkg] {pkg_dir.name}: {res['detail']}")
    return res


# ── Black Shield (save injection) ───────────────────────────────────────────

def _save_inject():
    d = pathlib.Path(__file__).resolve().parent / "saveinject"
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    import save_inject  # noqa: PLC0415
    return save_inject


BLACKSHIELD_DEFAULT = {
    "op": "add-unit",
    "name": "Black Shield",
    "template": "UTP_Assault_DarkAngel",
    "active": [],
    "passive": ["ABP_Alacrity", "ABP_Praetorian", "ABP_CripplingShot"],
    "ranged": "WT_T4_SW_Flamer",
    "melee": "WT_T4_Chainsword",
    "consumables": ["WTP_T4_StasisField"],
    "unlock": True,
    "force": False,
}


def apply_camera_patch(root, rules_path, session: Session, log=print) -> dict:
    """Camera unlock: apply equal-length byte transforms from camera_rules.json (exe + blueprint CDO)."""
    root = pathlib.Path(root)
    res = {"ok": True, "applied": 0, "skipped": 0, "errors": [], "detail": ""}
    try:
        doc = json.loads(pathlib.Path(rules_path).read_text(encoding="utf-8"))
        targets = doc.get("targets") or []
        if not targets:
            raise ValueError("camera_rules.json 没有 targets")
    except Exception as exc:                      # noqa: BLE001
        res.update(ok=False, errors=[f"规则读取失败: {exc}"])
        return res
    changed = []
    for t in targets:
        try:
            p = safe_join(root, t["relative_path"])
            if not p.is_file():
                raise FileNotFoundError(str(p))
            cur = sha256_file(p)
            if cur == t["dst_sha256"]:
                res["skipped"] += 1
                continue
            if cur != t["src_sha256"]:
                raise RuntimeError(f"版本不符（sha={cur[:12]}…），拒绝强改")
            buf = bytearray(p.read_bytes())
            for r in t["rules"]:
                off = int(r["offset"], 16)
                before = bytes.fromhex(r["before_hex"])
                after = bytes.fromhex(r["after_hex"])
                if len(before) != len(after):
                    raise RuntimeError(f"规则 {r['label']} 不是等长变换")
                if bytes(buf[off:off + len(before)]) != before:
                    raise RuntimeError(f"规则 {r['label']} 前置字节不符")
                buf[off:off + len(after)] = after
            out = bytes(buf)
            if sha256_bytes(out) != t["dst_sha256"]:
                raise RuntimeError("变换后 hash 不符")
            session.backup(p)
            write_atomic(p, out)
            res["applied"] += 1
            changed.append(t["relative_path"])
        except Exception as exc:                  # noqa: BLE001
            res["ok"] = False
            res["errors"].append(f"{t.get('relative_path')}: {exc}")
    res["detail"] = f"applied={res['applied']} skipped={res['skipped']}"
    if changed:
        res["detail"] += " · " + ", ".join(changed)
    return res


SPLASH_DESC_REL = "deathwatchgame/Content/Localization/Game/en/Game.locres"
# Crippling Shot and Splash share the same en/Game.locres (zh_cn overwrites the en slot in place),
# so the text layer uses the same path.
CRIPPLING_DESC_REL = SPLASH_DESC_REL
_SPLASH_PAIRS = [
    ("Deals 35% damage to all adjacent units.", "Deals 99% damage to all adjacent units."),
    ("对相邻单位造成 35% 伤害", "对相邻单位造成 99% 伤害"),
]
# Crippling Shot display string 2%→8% (equal-length UTF-16LE). The bugfix card's 1%→2% only changes
# the en value at 5AC6786D; here we change the strings that currently display "2%": two English variants
# (with/without a period) plus the three localized Chinese strings. The baseline also has a 1% string
# without a period at CAB34395 that is left untouched — it belongs to the bugfix card, out of scope here.
_CRIPPLING_PAIRS = [
    ("2% Chance to reduce target's AP by 1 per shot", "8% Chance to reduce target's AP by 1 per shot"),
    ("每次射击，有 2% 几率：-1 AP 于目标", "每次射击，有 8% 几率：-1 AP 于目标"),
]


def apply_balance_splash_text(root, session: Session, log=print) -> dict:
    """Missile splash display text 35%→99% (equal-length UTF-16LE); must be called after zh_cn.

    The Chinese text chain overwrites en/Game.locres in place, so this adapts by checking whether the
    current file contains the en 35% or the zh 35% string; if neither is present and 99% already is, treat
    it as fixed and skip. Only the locres display value is changed; the FText source hash anchor is left
    alone (same rule as bugfix_crippling_shot_desc). The backend multiplier is handled separately by the
    balance package via float32 0.35→0.99.
    """
    res = {"ok": True, "applied": 0, "skipped": 0, "errors": [], "detail": ""}
    p = pathlib.Path(root) / SPLASH_DESC_REL
    if not p.is_file():
        res["ok"] = False
        res["errors"].append(f"locres 缺失: {p}")
        res["detail"] = "locres missing"
        return res
    raw = p.read_bytes()
    chosen = None
    for old, new in _SPLASH_PAIRS:
        ob, nb = old.encode("utf-16-le"), new.encode("utf-16-le")
        if len(ob) != len(nb):
            res["ok"] = False
            res["errors"].append("溅射串不等长")
            return res
        if raw.count(ob) == 1:
            chosen = (ob, nb)
            break
    if chosen is None:
        if any(raw.count(n.encode("utf-16-le")) == 1 for _, n in _SPLASH_PAIRS):
            res["skipped"] = 1
            res["detail"] = "already 99%"
            return res
        res["ok"] = False
        res["errors"].append("溅射 35% 串未唯一命中（en/zh 皆无）")
        res["detail"] = "splash string not found"
        return res
    ob, nb = chosen
    out = raw.replace(ob, nb)
    if len(out) != len(raw):
        res["ok"] = False
        res["errors"].append("替换后长度变化")
        return res
    session.backup(p)
    write_atomic(p, out)
    res["applied"] = 1
    res["detail"] = "splash 35%→99% (equal-length)"
    log(f"[balance] {res['detail']}")
    return res


def apply_balance_crippling_text(root, session: Session, log=print) -> dict:
    """Crippling Shot display string 2%→8% (equal-length UTF-16LE); must be called after zh_cn.

    Same rule as apply_balance_splash_text: zh_cn overwrites en/Game.locres in place, so replacement
    adapts to whether the current file contains the en 2% or zh 2% string; if neither is present and 8%
    already is, treat it as fixed and skip. Only the locres display value is changed; the FText source hash
    anchor is left alone. The actual probability 0.02→0.08 is handled by the balance package
    (BUFF_2PercentChanceReduceAPBy1PerShot @0x73B), decoupled from this display transform. The bugfix
    card's 1%→2% (en 5AC6786D) is kept separately; this only handles strings that display "2%".
    """
    res = {"ok": True, "applied": 0, "skipped": 0, "errors": [], "detail": ""}
    p = pathlib.Path(root) / CRIPPLING_DESC_REL
    if not p.is_file():
        res["ok"] = False
        res["errors"].append(f"locres 缺失: {p}")
        res["detail"] = "locres missing"
        return res
    raw = p.read_bytes()
    out = raw
    replaced = 0
    for old, new in _CRIPPLING_PAIRS:
        ob, nb = old.encode("utf-16-le"), new.encode("utf-16-le")
        if len(ob) != len(nb):
            res["ok"] = False
            res["errors"].append("致残射击显示串不等长")
            return res
        cnt = out.count(ob)
        if cnt:
            out = out.replace(ob, nb)
            replaced += cnt
    if replaced:
        if len(out) != len(raw):
            res["ok"] = False
            res["errors"].append("替换后长度变化")
            return res
        session.backup(p)
        write_atomic(p, out)
        res["applied"] = replaced
        res["detail"] = f"crippling 2%→8% ×{replaced} (equal-length)"
        log(f"[balance] {res['detail']}")
        return res
    # No 2% string: if 8% is already present treat it as fixed, otherwise report an error.
    if any(out.count(n.encode("utf-16-le")) >= 1 for _, n in _CRIPPLING_PAIRS):
        res["skipped"] = 1
        res["detail"] = "already 8%"
    else:
        res["ok"] = False
        res["errors"].append("致残射击 2% 串未命中（en/zh 皆无，且 8% 不在）")
        res["detail"] = "crippling 2% string not found"
    return res


def blackshield_preflight(save_path=None) -> dict:
    try:
        return _save_inject().preflight_save(save_path)
    except Exception as exc:                      # noqa: BLE001
        return {"exists": False, "parse_ok": False, "path": str(save_path or ""),
                "reason": f"{type(exc).__name__}: {exc}"}


def inject_blackshield(session: Session, save_path=None, log=print) -> dict:
    """Write the save only after preflight passes: verify a temporary copy, then replace atomically, with automatic rollback on failure."""
    res = {"ok": False, "applied": 0, "skipped": 0, "errors": [], "detail": "",
           "backup": ""}
    pre = blackshield_preflight(save_path)
    if not pre.get("exists"):
        res.update(skipped=1, detail="save_not_found",
                   errors=[])
        log("[save] 未检测到存档，跳过黑盾注入")
        return res
    if not pre.get("parse_ok"):
        res.update(skipped=1, detail="save_unreadable",
                   errors=[pre.get("reason") or "存档无法解析"])
        log("[save] 存档无法解析，跳过黑盾注入")
        return res
    src = pathlib.Path(pre["path"])
    tmp = src.with_name(src.name + ".dwnew")
    try:
        m = _save_inject()
        content = session.root / "deathwatchgame" / "Content"
        if hasattr(m, "use_content"):
            m.use_content(content)
        orig = src.read_bytes()
        out, note = m.apply_ops(orig, [dict(BLACKSHIELD_DEFAULT)])
        # Validate the in-memory result first, then write the temporary copy; only replace the real save
        # after reading the temporary copy back successfully.
        m.SGP.parse_props(out)
        tmp.write_bytes(out)
        m.SGP.parse_props(tmp.read_bytes())
        bak = session.dir / "save" / src.name
        bak.parent.mkdir(parents=True, exist_ok=True)
        bak.write_bytes(orig)
        os.replace(tmp, src)
        try:
            m.SGP.parse_props(src.read_bytes())
        except Exception:
            # Roll back in edge cases so the user is never left with an unparseable save.
            write_atomic(src, orig)
            raise
        res.update(ok=True, applied=1, backup=str(bak),
                   detail=f"已追加 {BLACKSHIELD_DEFAULT['name']}（存档 {len(orig)}→{len(out)} B）")
        log(f"[save] {res['detail']}")
        return res
    except Exception as exc:                      # noqa: BLE001
        msg = str(exc)
        if "已存在" in msg:
            res.update(ok=True, skipped=1, detail="存档里已有黑盾，未重复注入")
            log(f"[save] {res['detail']}")
            return res
        res.update(errors=[msg])
        log(f"[save] 注入失败: {msg}")
        return res
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
