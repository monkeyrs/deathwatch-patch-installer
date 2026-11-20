#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generic UE4 (4.6-era) Kismet / Blueprint bytecode decoder.

Decodes the *raw* serialized bytecode stored in cooked .uasset Function exports into
(a) a flat instruction stream (offset + opcode + resolved operands) and
(b) a nested expression tree, exposing called function names, constants and branch
targets.

Format facts (established empirically against Steam Build 1837603 = UE4.6, cross-checked
against atenfyr/UAssetAPI's EExprToken table + CUE4Parse UStruct/UFunction deserializers):

  Export layout for a UFunction inside a cooked package:
    [ tagged-property stream ][ NONE terminator (12 B) ]
    [ 20-byte UStruct/UFunction header ]   <- SuperStruct(4) ScriptText(4) Children(4)
                                              bytecodeBufferSize(4) serializedScriptSize(4)
    [ bytecode: serializedScriptSize bytes ]
    [ 4 trailing bytes ]

  Opcode set = EExprToken (0x00..). Operand widths for this build:
    * CodeSkipSizeType / jump offsets = uint32 (4 B)   [EX_Jump / EX_PushExecutionFlow / EX_Skip]
    * EX_Context middle block = uint32 offset + 4-byte property pointer + uint8 PropertyType
    * identifier FPackageIndex = int32, FName = (int32 index, int32 number) = 8 B

This module is read-only; it never writes into the game tree.

Reference for the EExprToken table (verified over network):
  https://github.com/atenfyr/UAssetAPI  UAssetAPI/Kismet/Bytecode/EExprToken.cs
  https://github.com/atenfyr/UAssetAPI  .../Expressions/*.cs  (operand layout)
  https://github.com/FabianFG/CUE4Parse .../UObject/UStruct.cs, UFunction.cs (export header)
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

# ----------------------------------------------------------------------------- opcode table
# EExprToken (UE4). Values are stable across UE4; a handful were added after 4.6 and simply
# never appear in this build (they are still named here so unknown tokens are distinguishable).
EXPR_TOKEN = {
    0x00: "EX_LocalVariable",
    0x01: "EX_InstanceVariable",
    0x02: "EX_DefaultVariable",
    0x04: "EX_Return",
    0x06: "EX_Jump",
    0x07: "EX_JumpIfNot",
    0x09: "EX_Assert",
    0x0B: "EX_Nothing",
    0x0C: "EX_NothingInt32",
    0x0F: "EX_Let",
    0x11: "EX_BitFieldConst",
    0x12: "EX_ClassContext",
    0x13: "EX_MetaCast",
    0x14: "EX_LetBool",
    0x15: "EX_EndParmValue",
    0x16: "EX_EndFunctionParms",
    0x17: "EX_Self",
    0x18: "EX_Skip",
    0x19: "EX_Context",
    0x1A: "EX_Context_FailSilent",
    0x1B: "EX_VirtualFunction",
    0x1C: "EX_FinalFunction",
    0x1D: "EX_IntConst",
    0x1E: "EX_FloatConst",
    0x1F: "EX_StringConst",
    0x20: "EX_ObjectConst",
    0x21: "EX_NameConst",
    0x22: "EX_RotationConst",
    0x23: "EX_VectorConst",
    0x24: "EX_ByteConst",
    0x25: "EX_IntZero",
    0x26: "EX_IntOne",
    0x27: "EX_True",
    0x28: "EX_False",
    0x29: "EX_TextConst",
    0x2A: "EX_NoObject",
    0x2B: "EX_TransformConst",
    0x2C: "EX_IntConstByte",
    0x2D: "EX_NoInterface",
    0x2E: "EX_DynamicCast",
    0x2F: "EX_StructConst",
    0x30: "EX_EndStructConst",
    0x31: "EX_SetArray",
    0x32: "EX_EndArray",
    0x33: "EX_PropertyConst",
    0x34: "EX_UnicodeStringConst",
    0x35: "EX_Int64Const",
    0x36: "EX_UInt64Const",
    0x37: "EX_DoubleConst",
    0x38: "EX_PrimitiveCast",
    0x39: "EX_SetSet",
    0x3A: "EX_EndSet",
    0x3B: "EX_SetMap",
    0x3C: "EX_EndMap",
    0x3D: "EX_SetConst",
    0x3E: "EX_EndSetConst",
    0x3F: "EX_MapConst",
    0x40: "EX_EndMapConst",
    0x41: "EX_Vector3fConst",
    0x42: "EX_StructMemberContext",
    0x43: "EX_LetMulticastDelegate",
    0x44: "EX_LetDelegate",
    0x45: "EX_LocalVirtualFunction",
    0x46: "EX_LocalFinalFunction",
    0x48: "EX_LocalOutVariable",
    0x4A: "EX_DeprecatedOp4A",
    0x4B: "EX_InstanceDelegate",
    0x4C: "EX_PushExecutionFlow",
    0x4D: "EX_PopExecutionFlow",
    0x4E: "EX_ComputedJump",
    0x4F: "EX_PopExecutionFlowIfNot",
    0x50: "EX_Breakpoint",
    0x51: "EX_InterfaceContext",
    0x52: "EX_ObjToInterfaceCast",
    0x53: "EX_EndOfScript",
    0x54: "EX_CrossInterfaceCast",
    0x55: "EX_InterfaceToObjCast",
    0x5A: "EX_WireTracepoint",
    0x5B: "EX_SkipOffsetConst",
    0x5C: "EX_AddMulticastDelegate",
    0x5D: "EX_ClearMulticastDelegate",
    0x5E: "EX_Tracepoint",
    0x5F: "EX_LetObj",
    0x60: "EX_LetWeakObjPtr",
    0x61: "EX_BindDelegate",
    0x62: "EX_RemoveMulticastDelegate",
    0x63: "EX_CallMulticastDelegate",
    0x64: "EX_LetValueOnPersistentFrame",
    0x65: "EX_ArrayConst",
    0x66: "EX_EndArrayConst",
    0x67: "EX_SoftObjectConst",
    0x68: "EX_CallMath",
    0x69: "EX_SwitchValue",
    0x6A: "EX_InstrumentationEvent",
    0x6B: "EX_ArrayGetByRef",
    0x6C: "EX_ClassSparseDataVariable",
    0x6D: "EX_FieldPathConst",
}

# terminators that a param/element loop stops on
_END_FUNCTION_PARMS = 0x16
_END_STRUCT_CONST = 0x30
_END_ARRAY = 0x32
_END_SET = 0x3A
_END_MAP = 0x3C
_END_SET_CONST = 0x3E
_END_MAP_CONST = 0x40
_END_ARRAY_CONST = 0x66
_END_OF_SCRIPT = 0x53

# text literal types (EBlueprintTextLiteralType)
_TEXT_TYPES = {0: "Empty", 1: "LocalizedText", 2: "InvariantText",
               3: "LiteralString", 4: "StringTableEntry"}


class DecodeError(Exception):
    pass


@dataclass
class LoadCtx:
    """Names + object tables needed to resolve operands inside one package."""
    names: list
    imports: list          # dicts with 'object_name' / 'class_name'
    exports: list          # dicts with 'name' / 'cls'

    def fname(self, idx: int, num: int) -> str:
        if 0 <= idx < len(self.names):
            s = self.names[idx]
        else:
            return f"<FName idx {idx} OOR>"
        return s if num == 0 else f"{s}_{num}"

    def pkgindex(self, idx: int) -> str:
        if idx == 0:
            return "None"
        if idx > 0:
            e = idx - 1
            if 0 <= e < len(self.exports):
                x = self.exports[e]
                return f"export[{e}]:{x['cls']}:{x['name']}"
            return f"export#{idx}(OOR)"
        i = -idx - 1
        if 0 <= i < len(self.imports):
            return f"import[{i}]:{self.imports[i]['object_name']}"
        return f"import#{i}(OOR)"


@dataclass
class Node:
    token: int
    op: str
    off: int
    end: int
    operands: dict = field(default_factory=dict)
    children: list = field(default_factory=list)   # sub-expressions
    note: str = ""

    def to_obj(self):
        o = {"off": self.off, "token": "0x%02X" % self.token, "op": self.op}
        if self.operands:
            o.update(self.operands)
        if self.note:
            o["note"] = self.note
        if self.children:
            o["children"] = [c.to_obj() for c in self.children]
        return o


class BytecodeReader:
    """Parses one function's bytecode buffer into a tree of Nodes."""

    def __init__(self, data: bytes, ctx: LoadCtx, base: int = 0):
        self.d = data
        self.ctx = ctx
        self.base = base            # absolute file offset of data[0] (for error messages)
        self.pos = 0
        self.unknown_ops = set()

    # -- primitive reads -------------------------------------------------------
    def _need(self, n, what):
        if self.pos + n > len(self.d):
            raise DecodeError(f"{what}: need {n} bytes at {self.pos}, only "
                              f"{len(self.d) - self.pos} left")

    def u8(self):
        self._need(1, "u8"); v = self.d[self.pos]; self.pos += 1; return v

    def u16(self):
        self._need(2, "u16"); v = struct.unpack_from("<H", self.d, self.pos)[0]; self.pos += 2; return v

    def i32(self):
        self._need(4, "i32"); v = struct.unpack_from("<i", self.d, self.pos)[0]; self.pos += 4; return v

    def u32(self):
        self._need(4, "u32"); v = struct.unpack_from("<I", self.d, self.pos)[0]; self.pos += 4; return v

    def i64(self):
        self._need(8, "i64"); v = struct.unpack_from("<q", self.d, self.pos)[0]; self.pos += 8; return v

    def u64(self):
        self._need(8, "u64"); v = struct.unpack_from("<Q", self.d, self.pos)[0]; self.pos += 8; return v

    def f32(self):
        self._need(4, "f32"); v = struct.unpack_from("<f", self.d, self.pos)[0]; self.pos += 4; return v

    def f64(self):
        self._need(8, "f64"); v = struct.unpack_from("<d", self.d, self.pos)[0]; self.pos += 8; return v

    def raw(self, n):
        self._need(n, "raw"); v = self.d[self.pos:self.pos + n]; self.pos += n; return v

    def cstr(self):
        end = self.d.find(b"\x00", self.pos)
        if end < 0:
            raise DecodeError(f"unterminated string at {self.pos}")
        b = self.d[self.pos:end]
        self.pos = end + 1
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return b.decode("latin1")

    def u16str(self):
        end = self.pos
        while end + 1 < len(self.d) and self.d[end:end + 2] != b"\x00\x00":
            end += 2
        s = self.d[self.pos:end].decode("utf-16-le", "replace")
        self.pos = end + 2
        return s

    def fname(self):
        idx = self.i32(); num = self.i32()
        return self.ctx.fname(idx, num), idx, num

    # -- expression parsing ----------------------------------------------------
    def read_expr(self) -> Node:
        start = self.pos
        self._need(1, "opcode")
        tok = self.u8()
        name = EXPR_TOKEN.get(tok)
        if name is None:
            self.unknown_ops.add(tok)
            raise DecodeError(f"unknown opcode 0x%02X at offset {start}" % tok)
        node = Node(token=tok, op=name, off=start, end=start)

        if tok in (0x00, 0x01, 0x02, 0x48):              # *Variable (property pointer)
            idx = self.i32()
            node.operands["prop"] = self.ctx.pkgindex(idx)
            node.operands["prop_index"] = idx
        elif tok == 0x04:                                 # Return <expr>
            node.children.append(self.read_expr())
        elif tok == 0x06:                                 # Jump <u32>
            node.operands["code_offset"] = self.u32()
        elif tok == 0x07:                                 # JumpIfNot <u32> <expr>
            node.operands["code_offset"] = self.u32()
            node.children.append(self.read_expr())
        elif tok == 0x09:                                 # Assert
            node.operands["line"] = self.u16()
            node.operands["debug"] = self.u8()
            node.children.append(self.read_expr())
        elif tok == 0x0B:
            pass
        elif tok == 0x0C:
            node.operands["value"] = self.i32()
        elif tok == 0x0F or tok == 0x14:                  # Let / LetBool  <var> <value>
            node.children.append(self.read_expr())
            node.children.append(self.read_expr())
        elif tok == 0x11:                                 # BitFieldConst
            node.children.append(self.read_expr())
            node.operands["value"] = self.u8()
        elif tok in (0x12, 0x19, 0x1A):                   # (Class)Context
            obj = self.read_expr()
            off = self.u32()
            ptr = self.i32()
            prop_type = self.u8()
            ctx_expr = self.read_expr()
            node.children.extend([obj, ctx_expr])
            node.operands["skip_offset"] = off
            node.operands["prop_ptr"] = ptr
            node.operands["prop_type"] = prop_type
        elif tok == 0x13 or tok == 0x2E:                  # MetaCast / DynamicCast
            idx = self.i32()
            node.operands["class"] = self.ctx.pkgindex(idx)
            node.children.append(self.read_expr())
        elif tok in (0x15, 0x16, 0x17, 0x25, 0x26, 0x27, 0x28, 0x2A, 0x2D,
                     0x30, 0x32, 0x3A, 0x3C, 0x3E, 0x40, 0x50, 0x53, 0x5A,
                     0x5E, 0x66):
            pass
        elif tok == 0x18:                                 # Skip <u32> <expr>
            node.operands["code_offset"] = self.u32()
            node.children.append(self.read_expr())
        elif tok == 0x1B or tok == 0x45:                  # VirtualFunction / LocalVirtualFunction
            nm, idx, num = self.fname()
            node.operands["function"] = nm
            node.children.extend(self._read_params())
        elif tok in (0x1C, 0x46, 0x63, 0x68):             # Final/LocalFinal/CallMulticast/CallMath
            idx = self.i32()
            node.operands["function"] = self.ctx.pkgindex(idx)
            node.children.extend(self._read_params())
        elif tok == 0x1D:
            node.operands["value"] = self.i32()
        elif tok == 0x1E:
            node.operands["value"] = round(self.f32(), 6)
        elif tok == 0x1F:
            node.operands["value"] = self.cstr()
        elif tok == 0x20:                                 # ObjectConst
            idx = self.i32()
            node.operands["object"] = self.ctx.pkgindex(idx)
        elif tok == 0x21:                                 # NameConst
            nm, idx, num = self.fname()
            node.operands["value"] = nm
        elif tok == 0x22:
            node.operands["value"] = [self.i32(), self.i32(), self.i32()]
        elif tok == 0x23:
            node.operands["value"] = [round(self.f32(), 6) for _ in range(3)]
        elif tok == 0x24:
            node.operands["value"] = self.u8()
        elif tok == 0x29:                                 # TextConst (FScriptText)
            self._read_text(node)
        elif tok == 0x2B:
            node.operands["value"] = [round(self.f32(), 6) for _ in range(10)]
        elif tok == 0x2C:
            node.operands["value"] = self.u8()
        elif tok == 0x2F:                                 # StructConst
            idx = self.i32()
            size = self.i32()
            node.operands["struct"] = self.ctx.pkgindex(idx)
            node.operands["struct_size"] = size
            while self._peek() != _END_STRUCT_CONST:
                node.children.append(self.read_expr())
            self.read_expr()                              # consume EndStructConst
        elif tok == 0x31:                                 # SetArray
            node.children.append(self.read_expr())        # inner prop / l-value
            while self._peek() != _END_ARRAY:
                node.children.append(self.read_expr())
            self.read_expr()
        elif tok in (0x39, 0x3B, 0x3D, 0x3F):             # Set/Map consts
            term = {0x39: _END_SET, 0x3B: _END_MAP, 0x3D: _END_SET_CONST,
                    0x3F: _END_MAP_CONST}[tok]
            if tok in (0x3D, 0x3F):
                idx = self.i32()
                node.operands["inner"] = self.ctx.pkgindex(idx)
            if tok == 0x3F:
                node.operands["num_keys"] = self.i32()
            node.children.append(self.read_expr())
            while self._peek() != term:
                node.children.append(self.read_expr())
            self.read_expr()
        elif tok == 0x34:
            node.operands["value"] = self.u16str()
        elif tok == 0x35:
            node.operands["value"] = self.i64()
        elif tok == 0x36:
            node.operands["value"] = self.u64()
        elif tok == 0x37:
            node.operands["value"] = round(self.f64(), 6)
        elif tok == 0x38:                                 # PrimitiveCast
            node.operands["cast_type"] = self.u8()
            node.children.append(self.read_expr())
        elif tok == 0x41:
            node.operands["value"] = [round(self.f32(), 6) for _ in range(3)]
        elif tok == 0x42:                                 # StructMemberContext: FField ptr FIRST, then expr
            node.operands["prop_ptr"] = self.i32()
            node.children.append(self.read_expr())
        elif tok == 0x43 or tok == 0x44:                  # LetMulticastDelegate / LetDelegate
            node.children.append(self.read_expr())
            node.children.append(self.read_expr())
        elif 0x4C == tok:
            node.operands["pushing_address"] = self.u32()
        elif tok == 0x4D:
            pass
        elif tok == 0x4E:                                 # ComputedJump <expr>
            node.children.append(self.read_expr())
        elif tok == 0x4F:                                 # PopExecutionFlowIfNot <expr>
            node.children.append(self.read_expr())
        elif tok == 0x51:                                 # InterfaceContext
            node.children.append(self.read_expr())
        elif tok in (0x52, 0x54, 0x55):                   # interface casts
            idx = self.i32()
            node.operands["class"] = self.ctx.pkgindex(idx)
            node.children.append(self.read_expr())
        elif tok == 0x5B:
            node.operands["code_offset"] = self.u32()
        elif tok == 0x5C or tok == 0x5F or tok == 0x60:   # delegate/obj assigns
            node.children.append(self.read_expr())
            node.children.append(self.read_expr())
        elif tok == 0x5D:                                 # ClearMulticastDelegate
            node.children.append(self.read_expr())
        elif tok == 0x61:                                 # BindDelegate <fname> <expr> <expr>
            nm, idx, num = self.fname()
            node.operands["function"] = nm
            node.children.append(self.read_expr())
            node.children.append(self.read_expr())
        elif tok == 0x62:                                 # RemoveMulticastDelegate
            node.children.append(self.read_expr())
            node.children.append(self.read_expr())
        elif tok == 0x64:                                 # LetValueOnPersistentFrame
            idx = self.i32()
            node.operands["prop"] = self.ctx.pkgindex(idx)
            node.children.append(self.read_expr())
        elif tok == 0x65:                                 # ArrayConst
            idx = self.i32()
            node.operands["inner"] = self.ctx.pkgindex(idx)
            while self._peek() != _END_ARRAY_CONST:
                node.children.append(self.read_expr())
            self.read_expr()
        elif tok == 0x69:                                 # SwitchValue
            n = self.i32()
            node.operands["num_cases"] = n
            node.children.append(self.read_expr())        # index
            for _ in range(n):
                node.children.append(self.read_expr())    # case
                node.children.append(self.read_expr())    # value
            node.children.append(self.read_expr())        # default
            node.operands["end_offset"] = self.u32()
        elif tok == 0x6B:                                 # ArrayGetByRef
            node.children.append(self.read_expr())
            node.children.append(self.read_expr())
        elif tok in (0x4B,):                              # InstanceDelegate <fname>
            nm, idx, num = self.fname()
            node.operands["function"] = nm
        else:
            self.unknown_ops.add(tok)
            raise DecodeError(f"unimplemented opcode {name} (0x%02X) at offset {start}" % tok)

        node.end = self.pos
        return node

    def _read_text(self, node: Node):
        """EX_TextConst payload.

        UE4.6-era layout (this build): three consecutive Kismet expressions that are the
        localized FText's source / key / namespace strings (typically EX_StringConst, and an
        empty EX_StringConst "" for the unused fields).  Verified on OBJ_*::GetDescription
        (e.g. "num" / "" / "") and XP_BaseCombat::On Miss ("MISS" / "<hex>_Value1" / "").
        There is NO leading EBlueprintTextLiteralType byte in this generation.

        The newer (UE4.13+) layout instead starts with a literal-type byte; we detect it by
        checking whether the next byte is an FText-body opcode or an EX_StringConst.
        """
        if self._peek() == 0x1F:                           # legacy 3-string form
            node.operands["text_type"] = "legacy-3str"
            node.note = "legacy FText (source,key,namespace)"
            for _ in range(3):
                node.children.append(self.read_expr())
            return
        # modern FScriptText fallback
        t = self.u8()
        node.operands["text_type"] = _TEXT_TYPES.get(t, f"unknown({t})")
        if t == 1:                                         # LocalizedText: source, key, ns
            node.children.append(self.read_expr())
            node.children.append(self.read_expr())
            node.children.append(self.read_expr())
        elif t in (2, 3):
            node.children.append(self.read_expr())
        elif t == 4:                                       # StringTableEntry
            idx = self.i32()
            node.operands["string_table"] = self.ctx.pkgindex(idx)
            node.children.append(self.read_expr())
            node.children.append(self.read_expr())

    def _peek(self) -> int:
        return self.d[self.pos] if self.pos < len(self.d) else -1

    def _read_params(self) -> list:
        params = []
        for _ in range(10000):
            if self._peek() == _END_FUNCTION_PARMS:
                self.read_expr()                           # consume EX_EndFunctionParms
                return params
            if self._peek() < 0:
                raise DecodeError("param list ran off the end of the buffer")
            params.append(self.read_expr())
        raise DecodeError("param list did not terminate")

    def read_all(self) -> list:
        stmts = []
        while self.pos < len(self.d):
            if self._peek() == _END_OF_SCRIPT:
                n = Node(token=0x53, op="EX_EndOfScript", off=self.pos, end=self.pos + 1)
                self.pos += 1
                stmts.append(n)
                break
            stmts.append(self.read_expr())
        return stmts


# ----------------------------------------------------------------------------- extraction
HDR = 20          # UStruct/UFunction header before the bytecode
TRAILER = 4       # 4 bytes after the bytecode inside the export region


def extract_function_bytecode(pkg: dict, export_name: str, raw: bytes, cls: str | None = None):
    """Return (bytecode_bytes, info) for a Function export, or (None, why).

    A BP can have a property *and* a function with the same name (e.g. Based4's
    `CanSeeExplosion`), so callers should pass `cls` to disambiguate; when omitted we
    prefer an export whose class is a Function/DelegateFunction.
    """
    fn_classes = {"Function", "DelegateFunction"}
    exp = None
    for e in pkg.get("exports", []):
        if e["name"] != export_name:
            continue
        if cls is not None:
            if e.get("cls") == cls:
                exp = e
                break
        elif e.get("cls") in fn_classes:
            exp = e
            break
        elif exp is None:
            exp = e
    if exp is None:
        return None, f"export {export_name!r} not found"
    walk = None
    for w in pkg.get("walks", []):
        if w["name"] != export_name:
            continue
        if cls is not None and w.get("cls") != cls:
            continue
        if exp.get("cls") in fn_classes and w.get("cls") not in fn_classes:
            continue
        walk = w
        break
    if walk is None:
        return None, "no walk for export"
    off, size = exp["off"], exp["size"]
    after = walk["after"]                 # absolute offset just past the None terminator
    post = raw[after:off + size]
    if len(post) < HDR + TRAILER:
        return None, f"post-terminator region too small ({len(post)} B)"
    hdr = struct.unpack_from("<5i", post, 0)
    serialized = hdr[4]
    if serialized < 0 or HDR + serialized > len(post):
        return None, f"serializedScriptSize {serialized} inconsistent with region {len(post)}"
    bc = post[HDR:HDR + serialized]
    info = dict(header=dict(SuperStruct=hdr[0], ScriptText=hdr[1], Children=hdr[2],
                            bytecodeBufferSize=hdr[3], serializedScriptSize=hdr[4]),
                bytecode_off=after + HDR, bytecode_len=serialized, trailer=len(post) - HDR - serialized)
    return bc, info


def decode_function(bytecode: bytes, ctx: LoadCtx, base_off: int = 0):
    r = BytecodeReader(bytecode, ctx, base_off)
    stmts = r.read_all()
    consumed = r.pos
    return stmts, consumed, sorted(r.unknown_ops)


# ----------------------------------------------------------------------------- summaries
def iter_nodes(nodes):
    for n in nodes:
        yield n
        yield from iter_nodes(n.children)


def summarize(nodes):
    """Called function names / constants / branch targets from a node tree."""
    calls, consts, branches = [], [], []
    for n in iter_nodes(nodes):
        if n.op in ("EX_VirtualFunction", "EX_LocalVirtualFunction", "EX_InstanceDelegate",
                    "EX_BindDelegate"):
            calls.append(n.operands.get("function"))
        elif n.op in ("EX_FinalFunction", "EX_LocalFinalFunction", "EX_CallMulticastDelegate",
                      "EX_CallMath"):
            calls.append(n.operands.get("function"))
        if n.op in ("EX_IntConst", "EX_FloatConst", "EX_IntConstByte", "EX_ByteConst",
                    "EX_Int64Const", "EX_UInt64Const", "EX_DoubleConst"):
            consts.append({"op": n.op, "value": n.operands.get("value")})
        elif n.op in ("EX_StringConst", "EX_UnicodeStringConst", "EX_NameConst"):
            consts.append({"op": n.op, "value": n.operands.get("value")})
        elif n.op == "EX_TextConst":
            for c in n.children:
                if c.op in ("EX_StringConst", "EX_UnicodeStringConst"):
                    consts.append({"op": c.op, "value": c.operands.get("value")})
        elif n.op == "EX_VectorConst":
            consts.append({"op": n.op, "value": n.operands.get("value")})
        if n.op in ("EX_Jump", "EX_JumpIfNot", "EX_Skip", "EX_PushExecutionFlow"):
            branches.append({"op": n.op, "target": n.operands.get("code_offset")
                             if n.op != "EX_PushExecutionFlow" else n.operands.get("pushing_address")})
    return dict(calls=calls, consts=consts, branches=branches)


def render_text(nodes, indent=0, out=None):
    """Human-readable tree listing."""
    out = out if out is not None else []
    for n in nodes:
        pad = "  " * indent
        head = f"{pad}{n.off:06x}  {n.op}"
        bits = []
        for k, v in n.operands.items():
            bits.append(f"{k}={v}")
        if bits:
            head += "  " + " ".join(bits)
        if n.note:
            head += f"   # {n.note}"
        out.append(head)
        render_text(n.children, indent + 1, out)
    return out
