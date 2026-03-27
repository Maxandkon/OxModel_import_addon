"""
Star Control: Origins — OxModel Importer для Blender  v2.4
===========================================================
Підтримує: меш, UV, нормалі, матеріали (per-face/bone),
           кістки (4×4 col-major або 3×4), ієрархія кісток,
           ваги вершин, автоматичний fix нормалей

Встановлення:
  Edit → Preferences → Add-ons → Install → вибрати цей файл
  Активувати "Import-Export: Star Control Origins (.OxModel)"

Використання:
  File → Import → Star Control Origins (.OxModel)
"""

bl_info = {
    "name":        "Star Control Origins (.OxModel)",
    "author":      "Reverse engineered importer v2.2",
    "version":     (2, 4, 0),
    "blender":     (3, 0, 0),
    "location":    "File > Import > Star Control Origins (.OxModel)",
    "description": "Імпорт моделей з гри Star Control: Origins",
    "category":    "Import-Export",
}

import bpy
import struct
import math
import os
import mathutils
from bpy_extras.io_utils import ImportHelper
from bpy.props import (StringProperty, BoolProperty, FloatProperty, IntProperty)
from bpy.types import Operator


# ---------------------------------------------------------------------------
# КОНСТАНТИ СТРИМІВ
# ---------------------------------------------------------------------------
STREAM_POSITIONS  = 0
STREAM_NORMALS    = 1
STREAM_TANGENTS   = 2
STREAM_BITANGENTS = 3
STREAM_UV0        = 4
STREAM_UV1        = 5
STREAM_BONE_IDX   = 6   # float32, значення — цілі числа (bone index)
STREAM_BONE_WGT   = 7   # float32 ваги, сума ≈ 1.0


# ---------------------------------------------------------------------------
# ПАРСЕР
# ---------------------------------------------------------------------------

class OxModelParseError(Exception):
    pass


class OxModelParser:
    """Читає бінарний .OxModel файл і витягує всю геометрію та скелет."""

    def __init__(self, filepath):
        with open(filepath, "rb") as fh:
            self.data = fh.read()
        self.filepath = filepath
        self._parse()

    # ── low-level ──────────────────────────────────────────────────────────

    def _u32(self, o): return struct.unpack_from("<I", self.data, o)[0]

    def _ok_floats(self, vals, limit=500.0):
        """True якщо всі float32 у межах і не NaN/Inf."""
        for v in vals:
            if math.isnan(v) or math.isinf(v): return False
            if abs(v) > limit: return False
        return True

    # ── головний парсер ────────────────────────────────────────────────────

    def _parse(self):
        if len(self.data) < 16:
            raise OxModelParseError("Файл занадто малий")

        self.version    = self._u32(0x04)
        table_off       = self._u32(0x08)
        num_entries     = self._u32(0x00)

        # Секційна таблиця: пари (offset, size)
        sections = {}
        for i in range(num_entries // 2):
            off = self._u32(table_off + i * 8)
            sz  = self._u32(table_off + i * 8 + 4)
            if off in (0, 0xFFFFFFFF): continue
            if off + sz <= len(self.data):
                sections[i] = (off, sz)
        self.sections = sections

        # ─ Секція 1: LOD дескриптор ────────────────────────────────────────
        s1 = sections.get(1)
        if not s1: raise OxModelParseError("Немає секції 1 (mesh descriptor)")
        self.sec1_off, self.sec1_size = s1

        desc = self.sec1_off + 4          # LOD0 descriptor offset
        self.sub_off_1 = self._u32(desc + 16)  # bone hierarchy table
        self.sub_off_2 = self._u32(desc + 20)  # bone matrices (alt, 3×4)
        self.sub_off_3 = self._u32(desc + 24)  # bone matrices (main, 4×4)
        self.num_submeshes = self._u32(desc + 84)

        # ─ Секція 3: index buffer ──────────────────────────────────────────
        s3 = sections.get(3)
        if not s3: raise OxModelParseError("Немає секції 3 (індекси)")
        n_idx = s3[1] // 4
        self.indices = list(struct.unpack_from(f"<{n_idx}I", self.data, s3[0]))

        # ─ Секція 5: stream table ──────────────────────────────────────────
        s5 = sections.get(5)
        if not s5: raise OxModelParseError("Немає секції 5 (стрими)")
        self.streams = self._read_stream_table(s5[0])

        pos = self.streams.get(STREAM_POSITIONS)
        if not pos: raise OxModelParseError("Немає потоку позицій")
        self.num_verts = pos["count"] // 3

        # ─ Дані вершин ─────────────────────────────────────────────────────
        self.positions  = self._read_vec3(STREAM_POSITIONS)
        self.normals    = self._read_vec3(STREAM_NORMALS)
        self.tangents   = self._read_vec3(STREAM_TANGENTS)
        self.bitangents = self._read_vec3(STREAM_BITANGENTS)
        self.uvs        = self._read_uv(STREAM_UV0)
        self.uvs2       = self._read_uv(STREAM_UV1)
        self.bone_idx   = self._read_bone_idx()
        self.bone_wgt   = self._read_bone_wgt()

        # ─ Скелет ──────────────────────────────────────────────────────────
        self.bone_matrices = []
        self.bone_parents  = []
        self._find_bones()

    # ── stream table ───────────────────────────────────────────────────────

    def _read_stream_table(self, base):
        streams = {}
        for i in range(16):
            o = base + i * 16
            if o + 16 > len(self.data): break
            cnt = self._u32(o)
            off = self._u32(o + 4)
            if cnt == 0 and off == 0: break
            if cnt == 0 or off >= len(self.data): continue
            streams[i] = {"count": cnt, "offset": off, "stride": self._u32(o + 8)}
        return streams

    # ── vertex helpers ─────────────────────────────────────────────────────

    def _read_vec3(self, sid):
        e = self.streams.get(sid)
        if not e: return []
        o = e["offset"]
        return [struct.unpack_from("<3f", self.data, o + i*12)
                for i in range(self.num_verts)]

    def _read_uv(self, sid):
        e = self.streams.get(sid)
        if not e: return []
        o = e["offset"]
        return [struct.unpack_from("<2f", self.data, o + i*8)
                for i in range(self.num_verts)]

    def _read_bone_idx(self):
        e = self.streams.get(STREAM_BONE_IDX)
        if not e: return []
        o = e["offset"]
        result = []
        for i in range(self.num_verts):
            f0,f1,f2,f3 = struct.unpack_from("<4f", self.data, o + i*16)
            result.append((int(f0), int(f1), int(f2), int(f3)))
        return result

    def _read_bone_wgt(self):
        e = self.streams.get(STREAM_BONE_WGT)
        if not e: return []
        o = e["offset"]
        return [struct.unpack_from("<4f", self.data, o + i*16)
                for i in range(self.num_verts)]

    # ── bone matrices ──────────────────────────────────────────────────────

    def _find_bones(self):
        """
        Знаходить матриці кісток у секції 1.

        Формат:
          - sub_off_3 → 4×4 float32 (stride=64, col-major)  ← основний
          - sub_off_2 → 3×4 float32 (stride=48)             ← запасний
          - brute-force                                      ← fallback
        """
        mats = (self._try_4x4(self.sec1_off + self.sub_off_3) or
                self._try_3x4(self.sec1_off + self.sub_off_2) or
                self._brute_force())
        self.bone_matrices = mats

        if mats and self.sub_off_1 > 0:
            self.bone_parents = self._parse_hierarchy(
                self.sec1_off + self.sub_off_1, len(mats))

    def _rot_ok(self, v4x4, start):
        """Перевірка чи перші 3 стовпці матриці — одиничні вектори."""
        r0 = math.sqrt(v4x4[start+0]**2 + v4x4[start+1]**2 + v4x4[start+2]**2)
        r1 = math.sqrt(v4x4[start+4]**2 + v4x4[start+5]**2 + v4x4[start+6]**2)
        r2 = math.sqrt(v4x4[start+8]**2 + v4x4[start+9]**2 + v4x4[start+10]**2)
        return 0.3 < r0 < 2.0 and 0.3 < r1 < 2.0 and 0.3 < r2 < 2.0

    def _try_4x4(self, start, min_n=3):
        """Зчитує послідовні 4×4 матриці (stride=64, col-major)."""
        mats, off = [], start
        while off + 64 <= len(self.data):
            v = struct.unpack_from("<16f", self.data, off)
            if not self._ok_floats(v) or not self._rot_ok(v, 0): break
            # col-major: стовпці 0,1,2 = rotation; стовпець 3 = translation
            # mathutils.Matrix constructor takes ROWS, so we pass col0..col3 as rows
            # then call .transposed() to get the proper transform
            mats.append(mathutils.Matrix([
                [v[0], v[1], v[2], v[3]],
                [v[4], v[5], v[6], v[7]],
                [v[8], v[9], v[10], v[11]],
                [v[12], v[13], v[14], v[15]],
            ]))
            off += 64
        return mats if len(mats) >= min_n else []

    def _try_3x4(self, start, min_n=3):
        """Зчитує послідовні 3×4 матриці (stride=48)."""
        mats, off = [], start
        while off + 48 <= len(self.data):
            v = struct.unpack_from("<12f", self.data, off)
            if not self._ok_floats(v) or not self._rot_ok(v, 0): break
            mats.append(mathutils.Matrix([
                [v[0],  v[1],  v[2],  v[3]],
                [v[4],  v[5],  v[6],  v[7]],
                [v[8],  v[9],  v[10], v[11]],
                [0.0,   0.0,   0.0,   1.0],
            ]))
            off += 48
        return mats if len(mats) >= min_n else []

    def _brute_force(self, min_run=10):
        """Пошук найбільшого блоку матриць у секції 1."""
        best, off = [], self.sec1_off
        end = self.sec1_off + self.sec1_size - 64 * min_run
        while off < end:
            r4 = self._try_4x4(off, min_run)
            if r4 and len(r4) > len(best):
                best = r4; off += len(r4) * 64; continue
            r3 = self._try_3x4(off, min_run)
            if r3 and len(r3) > len(best):
                best = r3; off += len(r3) * 48; continue
            off += 4
        return best

    # ── bone hierarchy ─────────────────────────────────────────────────────

    def _parse_hierarchy(self, table_off, num_bones):
        """
        Розбирає ієрархію кісток (32-байтні записи).

        Формат запису:
          v0 = (entry_depth << 16 | parent_bone_idx)
          v1 = (v1_hi        << 16 | own_bone_idx)

        Три кроки:
          1. Пряма таблиця: v1_lo → parent = v0_lo
          2. Depth-chain: depth_to_bone[D]=v0_lo; кістка при depth D
             = depth_to_bone[D+1]; батько кістки = depth_to_bone[D]
             (з gap-bridging для пропущених глибин)
          3. Fallback для ізольованих кісток: просторовий пошук
             найближчого батька серед вже розміщених.
        """
        parents      = [-1] * num_bones
        own_in_table = set()

        # ── Крок 1: прямі parent → own ──────────────────────────────────
        for i in range(num_bones):
            base = table_off + i * 32
            if base + 32 > len(self.data): break
            v0    = self._u32(base)
            v1    = self._u32(base + 4)
            v0_lo = v0 & 0xffff
            v1_lo = v1 & 0xffff
            if v1_lo == 0xffff or v1_lo >= num_bones: continue
            own_in_table.add(v1_lo)
            parents[v1_lo] = v0_lo if (v0_lo < num_bones and v0_lo != 0xffff) else -1

        chain_set = set(b for b in range(num_bones) if b not in own_in_table)

        # ── depth_to_bone[D] = перший v0_lo для entries з v0_hi=D ────────
        depth_to_bone = {}
        for i in range(num_bones):
            base  = table_off + i * 32
            if base + 32 > len(self.data): break
            v0    = self._u32(base)
            v0_hi = (v0 >> 16) & 0xffff
            v0_lo =  v0        & 0xffff
            if v0_lo < num_bones and 0 < v0_hi < 65535:
                depth_to_bone.setdefault(v0_hi, v0_lo)

        depths_sorted = sorted(depth_to_bone.keys())

        # ── Крок 2: chain-кістки через власну глибину ────────────────────
        # Для кожної chain-кістки B:
        #   зібрати всі depths, де B фігурує як v0_lo (батько)
        #   own_depth = min(depths) - 1
        #   parent = depth_to_bone[найближча глибина <= own_depth]
        bone_depths_as_parent = {}
        for i in range(num_bones):
            base  = table_off + i * 32
            if base + 32 > len(self.data): break
            v0    = self._u32(base)
            v0_hi = (v0 >> 16) & 0xffff
            v0_lo =  v0        & 0xffff
            if v0_lo < num_bones and 0 < v0_hi < 65535:
                bone_depths_as_parent.setdefault(v0_lo, []).append(v0_hi)

        for b in sorted(chain_set):
            if parents[b] != -1:
                continue
            dep_list = [d for d in bone_depths_as_parent.get(b, [])
                        if 0 < d < 65535]
            if not dep_list:
                continue
            own_depth = min(dep_list) - 1
            par = -1
            for dd in reversed(depths_sorted):
                if dd <= own_depth:
                    par = depth_to_bone[dd]
                    break
            if 0 <= par < num_bones and par != b:
                parents[b] = par

        # ── Крок 2б: v0_hi=65535 entries («branch continuation») ─────────
        # Entries де v0_hi=65535 і v0_lo = chain кістка означають:
        # «ця chain кістка є поточним батьком у гілці»
        # Її батько = v0_lo попереднього entry з валідним depth (0 < v0_hi < 65535)
        prev_valid_bone = -1
        for i in range(num_bones):
            base  = table_off + i * 32
            if base + 32 > len(self.data): break
            v0    = self._u32(base)
            v0_hi = (v0 >> 16) & 0xffff
            v0_lo =  v0        & 0xffff
            if v0_lo >= num_bones: continue
            if 0 < v0_hi < 65535:
                prev_valid_bone = v0_lo          # останній "anchor"
            elif v0_hi == 65535 and v0_lo in chain_set:
                # chain кістка v0_lo = термінальна в цій гілці
                if (parents[v0_lo] == -1 and
                        0 <= prev_valid_bone < num_bones and
                        prev_valid_bone != v0_lo):
                    parents[v0_lo] = prev_valid_bone

        # ── Крок 4: перевірка циклів та виправлення ─────────────────────
        # Після всіх присвоєнь перевіряємо кожну кістку:
        # якщо walk по батьках потрапляє у цикл → відрізаємо зв'язок (parent=-1)
        for start in range(num_bones):
            visited = set()
            cur     = start
            path    = []
            while cur != -1 and cur not in visited:
                visited.add(cur)
                path.append(cur)
                cur = parents[cur] if cur < num_bones else -1
            if cur in visited:
                # Знайшли цикл: відрізаємо зв'язок першої кістки у циклі
                idx = path.index(cur)
                parents[path[idx]] = -1
        def bone_pos(b):
            if b >= len(self.bone_matrices): return (0., 0., 0.)
            m = self.bone_matrices[b]
            return (m[3][0], m[3][1], m[3][2])

        def dist2(a, b):
            return sum((a[i] - b[i]) ** 2 for i in range(3))

        # Множина вже «розміщених» кісток (мають батька або є коренем)
        placed = set(b for b in range(num_bones) if parents[b] >= 0 or b == 0)

        # Обробляємо chain-кістки без батька у порядку зростання відстані від origin
        unset = sorted(
            (b for b in chain_set if parents[b] == -1 and b != 0),
            key=lambda b: dist2(bone_pos(b), (0., 0., 0.))
        )
        for b in unset:
            pos_b = bone_pos(b)
            best_par, best_d = -1, float('inf')
            for p in placed:
                pos_p = bone_pos(p)
                d = dist2(pos_b, pos_p)
                if d < best_d and d > 0:
                    best_d, best_par = d, p
            parents[b] = best_par
            placed.add(b)

        return parents


# ---------------------------------------------------------------------------
# PALETTE LOADER & MATERIAL BUILDER
# ---------------------------------------------------------------------------

def _find_palette_file(oxmodel_path):
    """Шукає .Palette файл поруч із .OxModel."""
    model_dir = os.path.dirname(os.path.abspath(oxmodel_path))
    char_name = os.path.basename(model_dir)
    candidate = os.path.join(model_dir, char_name + ".Palette")
    if os.path.isfile(candidate):
        return candidate
    try:
        for f in sorted(os.listdir(model_dir)):
            if f.lower().endswith(".palette"):
                return os.path.join(model_dir, f)
    except OSError:
        pass
    return None


def _parse_palette(palette_path):
    """
    Зчитує CSV-файл .Palette.
    Повертає список словників:
      [{"name": str, "diffuse": path|None, "ao": path|None,
        "normal": path|None, "normal_world": bool,
        "specular": path|None, "emissive": path|None}, ...]
    """
    import csv, io as _io
    try:
        with open(palette_path, "rb") as fh:
            raw = fh.read().decode("utf-8", errors="replace")
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    except OSError:
        return []

    reader = csv.reader(_io.StringIO(raw))
    rows   = list(reader)
    if len(rows) < 2:
        return []

    header = [h.strip().lower() for h in rows[0]]
    col    = {h: i for i, h in enumerate(header)}

    def get(row, key):
        i = col.get(key)
        if i is None or i >= len(row): return None
        v = row[i].strip()
        return v if v else None

    entries = []
    for row in rows[1:]:
        if not any(c.strip() for c in row): continue
        name = get(row, "name")
        if not name: continue
        nmap = get(row, "normalmap")
        norm_world = bool(nmap and "_NORMW" in nmap.upper()
                          and "_NORMT" not in nmap.upper())
        entries.append({
            "name":         name,
            "diffuse":      get(row, "diffuse"),
            "ao":           get(row, "ambientocclusion"),
            "normal":       nmap,
            "normal_world": norm_world,
            "specular":     get(row, "specularmap"),
            "emissive":     get(row, "emissivemap"),
        })
    return entries


def _resolve_texture_path(tex_rel, base_dir):
    """Перетворює відносний шлях текстури у абсолютний."""
    if not tex_rel:
        return None
    tex_rel = tex_rel.lstrip("/\\")
    search_root = base_dir
    for _ in range(6):
        candidate = os.path.join(search_root, tex_rel)
        if os.path.isfile(candidate):
            return candidate
        for ext in (".png", ".tga", ".jpg"):
            c2 = os.path.splitext(candidate)[0] + ext
            if os.path.isfile(c2):
                return c2
        search_root = os.path.dirname(search_root)
    # Fallback: тільки filename у base_dir
    fname = os.path.basename(tex_rel)
    base_noext = os.path.splitext(os.path.join(base_dir, fname))[0]
    for ext in ("", ".dds", ".png", ".tga", ".jpg"):
        c = base_noext + ext
        if os.path.isfile(c):
            return c
    return None


def _get_or_create_oxmat_nodegroup():
    """
    Повертає (або створює) Node Group "OxMat" —
    шаблон матеріалу для Star Control Origins.

    Входи: Diffuse, DiffuseAlpha, AO, Normal, NormalStrength,
           Specular, Emissive, EmissiveStrength
    Вихід: Surface (Shader)
    """
    GRP_NAME = "OxMat"
    ng = bpy.data.node_groups.get(GRP_NAME)
    if ng:
        return ng

    ng = bpy.data.node_groups.new(GRP_NAME, "ShaderNodeTree")

    def mk_input(name, stype, default=None):
        try:
            s = ng.interface.new_socket(name, in_out="INPUT", socket_type=stype)
        except AttributeError:
            s = ng.inputs.new(stype, name)
        if default is not None:
            try: s.default_value = default
            except Exception: pass

    def mk_output(name, stype):
        try:
            ng.interface.new_socket(name, in_out="OUTPUT", socket_type=stype)
        except AttributeError:
            ng.outputs.new(stype, name)

    mk_input("Diffuse",          "NodeSocketColor", (0.8, 0.8, 0.8, 1.0))
    mk_input("DiffuseAlpha",     "NodeSocketFloat", 1.0)
    mk_input("AO",               "NodeSocketColor", (1.0, 1.0, 1.0, 1.0))
    mk_input("Normal",           "NodeSocketColor", (0.5, 0.5, 1.0, 1.0))
    mk_input("NormalStrength",   "NodeSocketFloat", 1.0)
    mk_input("Specular",         "NodeSocketColor", (0.5, 0.5, 0.5, 1.0))
    mk_input("Emissive",         "NodeSocketColor", (0.0, 0.0, 0.0, 1.0))
    mk_input("EmissiveStrength", "NodeSocketFloat", 7.0)
    mk_output("Surface", "NodeSocketShader")

    nd  = ng.nodes
    lnk = ng.links

    def N(t, x, y, **kw):
        n = nd.new(t); n.location = (x, y)
        for k, v in kw.items():
            try: setattr(n, k, v)
            except Exception: pass
        return n

    gIN  = N("NodeGroupInput",  -900,  0)
    gOUT = N("NodeGroupOutput",  600,  0)

    pbsdf    = N("ShaderNodeBsdfPrincipled",  200,  100)
    ao_mix   = N("ShaderNodeMixRGB",         -200,  250)
    nmap_nd  = N("ShaderNodeNormalMap",       -200, -100)
    emis_mul = N("ShaderNodeMixRGB",         -200, -350)

    ao_mix.blend_type   = "MULTIPLY"
    emis_mul.blend_type = "MULTIPLY"
    try:
        ao_mix.inputs["Fac"].default_value   = 1.0
        emis_mul.inputs["Fac"].default_value = 1.0
    except Exception: pass

    # AO × Diffuse → Base Color
    lnk.new(gIN.outputs["AO"],              ao_mix.inputs[1])
    lnk.new(gIN.outputs["Diffuse"],         ao_mix.inputs[2])
    lnk.new(ao_mix.outputs["Color"],        pbsdf.inputs["Base Color"])
    lnk.new(gIN.outputs["DiffuseAlpha"],    pbsdf.inputs["Alpha"])

    # Normal
    lnk.new(gIN.outputs["Normal"],          nmap_nd.inputs["Color"])
    lnk.new(gIN.outputs["NormalStrength"],  nmap_nd.inputs["Strength"])
    lnk.new(nmap_nd.outputs["Normal"],      pbsdf.inputs["Normal"])

    # Specular
    for sp_name in ("Specular Tint", "Specular", "Specular IOR Level"):
        try: lnk.new(gIN.outputs["Specular"], pbsdf.inputs[sp_name]); break
        except Exception: pass

    # Emissive = Emissive × EmissiveStrength (через Fac)
    lnk.new(gIN.outputs["Emissive"],          emis_mul.inputs[1])
    lnk.new(gIN.outputs["EmissiveStrength"],  emis_mul.inputs["Fac"])
    try:
        emis_mul.inputs[2].default_value = (1, 1, 1, 1)
    except Exception: pass
    for em_name in ("Emission Color", "Emission"):
        try: lnk.new(emis_mul.outputs["Color"], pbsdf.inputs[em_name]); break
        except Exception: pass
    try: pbsdf.inputs["Emission Strength"].default_value = 1.0
    except Exception: pass

    # Output
    lnk.new(pbsdf.outputs["BSDF"], gOUT.inputs["Surface"])

    return ng


def _build_material_from_palette(mat_name, palette_entry, base_dir):
    """
    Створює/оновлює Blender-матеріал на основі запису з Palette.
    Будує node-tree: OxMat NodeGroup + Image Texture ноди.
    """
    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        mat = bpy.data.materials.new(mat_name)
    mat.use_nodes    = True
    mat.blend_method = "CLIP"

    tree  = mat.node_tree
    nodes = tree.nodes
    links = tree.links
    nodes.clear()

    ng       = _get_or_create_oxmat_nodegroup()
    out_node = nodes.new("ShaderNodeOutputMaterial"); out_node.location = (700, 0)
    grp_node = nodes.new("ShaderNodeGroup");          grp_node.location = (350, 0)
    grp_node.node_tree = ng
    grp_node.name = grp_node.label = "OxMat"
    links.new(grp_node.outputs["Surface"], out_node.inputs["Surface"])

    pe = palette_entry or {}

    def add_tex(label, rel_path, x, y, colorspace="sRGB"):
        n = nodes.new("ShaderNodeTexImage")
        n.location = (x, y)
        n.name = n.label = label
        abs_p = _resolve_texture_path(rel_path, base_dir) if rel_path else None
        if abs_p:
            try:
                img = bpy.data.images.get(os.path.basename(abs_p))
                if img is None:
                    img = bpy.data.images.load(abs_p)
                try: img.colorspace_settings.name = colorspace
                except Exception: pass
                n.image = img
            except Exception as e:
                print(f"[OxModel] Texture load warning ({abs_p}): {e}")
        return n

    dif  = add_tex("Diffuse",  pe.get("diffuse"),  -350,  400, "sRGB")
    ao   = add_tex("AO",       pe.get("ao"),        -350,  130, "Non-Color")
    norm = add_tex("Normal",   pe.get("normal"),    -350, -140, "Non-Color")
    spec = add_tex("Specular", pe.get("specular"),  -350, -410, "Non-Color")
    emis = add_tex("Emissive", pe.get("emissive"),  -350, -680, "sRGB")

    if pe.get("normal_world"):
        norm.label = "Normal (World Space — підключи до окремої Normal Map ноди з World)"

    links.new(dif.outputs["Color"],  grp_node.inputs["Diffuse"])
    links.new(dif.outputs["Alpha"],  grp_node.inputs["DiffuseAlpha"])
    links.new(ao.outputs["Color"],   grp_node.inputs["AO"])
    links.new(norm.outputs["Color"], grp_node.inputs["Normal"])
    links.new(spec.outputs["Color"], grp_node.inputs["Specular"])
    links.new(emis.outputs["Color"], grp_node.inputs["Emissive"])

    try: grp_node.inputs["NormalStrength"].default_value   = 1.0
    except Exception: pass
    try: grp_node.inputs["EmissiveStrength"].default_value = 7.0
    except Exception: pass

    return mat


def _assign_palette_materials(mesh, faces, base_name,
                              palette_entries, base_dir):
    """
    Створює один Blender-матеріал (з OxMat NodeGroup) на кожен запис у Palette.
    Логіка призначення матеріальних слотів:

      • Записи з "_BackPiece" у назві → другорядний матеріал (slot 1+)
      • Решта записів → основний матеріал (slot 0)
      • Якщо Palette порожній → один порожній матеріал

    Всі полігони отримують material_index 0 (головний матеріал = перший
    не-BackPiece запис). Користувач вручну перевизначає BackPiece-грані.
    Якщо в Palette тільки BackPiece-записи → slot 0 = перший з них.

    Підказка з назвами матеріалів виводиться в системну консоль.
    """
    if not palette_entries:
        # Немає Palette — один порожній матеріал
        mat = bpy.data.materials.get(base_name)
        if mat is None:
            mat = bpy.data.materials.new(base_name)
            mat.use_nodes = True
        mesh.materials.append(mat)
        for poly in mesh.polygons:
            poly.material_index = 0
        return

    # ── Сортуємо: не-BackPiece спочатку, BackPiece в кінці ────────────────
    def is_backpiece(entry):
        return "backpiece" in entry["name"].lower()

    main_entries = [e for e in palette_entries if not is_backpiece(e)]
    back_entries = [e for e in palette_entries if     is_backpiece(e)]
    ordered      = main_entries + back_entries   # main first, back last

    # ── Будуємо матеріали ──────────────────────────────────────────────────
    mat_names = []
    for i, entry in enumerate(ordered):
        mat_name = f"{base_name}_{entry['name']}"
        mat = _build_material_from_palette(mat_name, entry, base_dir)
        mesh.materials.append(mat)
        mat_names.append(mat_name)

    # ── Всі полігони → slot 0 (головний матеріал) ─────────────────────────
    for poly in mesh.polygons:
        poly.material_index = 0

    # ── Консольна підказка ─────────────────────────────────────────────────
    print(f"[OxModel] Матеріали для '{base_name}':")
    for i, name in enumerate(mat_names):
        role = "(BackPiece — призначити вручну)" if is_backpiece(ordered[i]) else "(головний)"
        print(f"  Slot {i}: {name}  {role}")
    if back_entries:
        print(f"[OxModel]  → Для BackPiece: Edit Mode → виділити грані → "
              f"вибрати Slot 1 → Assign")



# ---------------------------------------------------------------------------
# BLENDER IMPORTER
# ---------------------------------------------------------------------------

class OxModelBlenderImporter:
    """Конвертує дані OxModelParser в об'єкти Blender."""

    def __init__(self, parser: OxModelParser, options: dict):
        self.p   = parser
        self.opt = options

    def execute(self, context):
        try:
            mesh_obj = self._build_mesh(context)
            if mesh_obj is None:
                return {"CANCELLED"}
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"[OxModel] Mesh build ERROR: {exc}")
            return {"CANCELLED"}

        # Ensure we are in OBJECT mode before proceeding
        if context.mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except Exception:
                pass

        arm_obj = None
        if self.opt.get("import_armature") and self.p.bone_matrices:
            try:
                arm_obj = self._build_armature(context)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[OxModel] Armature build ERROR (non-fatal): {exc}")

        if arm_obj:
            try:
                mesh_obj.parent = arm_obj
                self._assign_bone_weights(mesh_obj)
                self._add_armature_modifier(mesh_obj, arm_obj)
            except Exception as exc:
                print(f"[OxModel] Weight assignment ERROR (non-fatal): {exc}")

        if self.opt.get("fix_normals"):
            try:
                self._fix_normals(mesh_obj, context)
            except Exception as exc:
                print(f"[OxModel] Fix normals ERROR (non-fatal): {exc}")

        return {"FINISHED"}

    # ── mesh ───────────────────────────────────────────────────────────────

    def _build_mesh(self, context):
        p, opt = self.p, self.opt
        name  = os.path.splitext(os.path.basename(p.filepath))[0]
        scale = opt.get("scale", 0.01)

        verts = list(p.positions)
        faces = []
        for i in range(len(p.indices) // 3):
            a, b, c = p.indices[i*3], p.indices[i*3+1], p.indices[i*3+2]
            if a < len(verts) and b < len(verts) and c < len(verts):
                faces.append((a, b, c))

        if not faces:
            return None

        mesh = bpy.data.meshes.new(name)
        obj  = bpy.data.objects.new(name, mesh)
        context.collection.objects.link(obj)
        context.view_layer.objects.active = obj
        obj.select_set(True)

        mesh.from_pydata(verts, [], faces)
        mesh.update()
        obj.scale = (scale, scale, scale)

        # Smooth shading (works in both Blender 3.x and 4.x)
        for poly in mesh.polygons:
            poly.use_smooth = True

        if p.uvs and opt.get("import_uvs", True):
            self._apply_uvs(mesh, faces, p.uvs, "UVMap")
        if p.uvs2 and opt.get("import_uvs", True):
            self._apply_uvs(mesh, faces, p.uvs2, "UVMap2")

        if p.normals and opt.get("import_normals", True):
            self._apply_normals(mesh, faces, p.normals)

        if opt.get("material_mode", "slots") != "none":
            self._assign_materials(mesh, faces, name)

        return obj

    def _apply_uvs(self, mesh, faces, uvs, layer_name):
        layer = mesh.uv_layers.new(name=layer_name)
        idx   = 0
        for fv in faces:
            for vi in fv:
                if vi < len(uvs):
                    u, v = uvs[vi]
                    layer.data[idx].uv = (u, 1.0 - v)   # flip V для Blender
                idx += 1

    def _apply_normals(self, mesh, faces, normals):
        # Custom normals: works in Blender 3.x and 4.x without auto smooth
        lnorms = []
        for fv in faces:
            for vi in fv:
                lnorms.append(normals[vi] if vi < len(normals) else (0.0, 0.0, 1.0))
        try:
            mesh.normals_split_custom_set(lnorms)
        except Exception as e:
            # Blender 4.2+: normals_split_custom_set may need calc_normals_split first
            try:
                mesh.calc_normals_split()
                mesh.normals_split_custom_set(lnorms)
            except Exception:
                print(f"[OxModel] Custom normals skipped: {e}")

    def _assign_materials(self, mesh, faces, base_name):
        """
        Режими (opt['material_mode']):
          'palette' — читає .Palette поряд із файлом, будує OxMat NodeGroup,
                      розрізняє унікальні матеріали за slot-індексами.
          'slots'   — N порожніх слотів (без текстур), призначати вручну.
          'single'  — один матеріал для всієї моделі.
        """
        mode = self.opt.get("material_mode", "palette")
        p    = self.p

        if mode == "palette":
            # Шукаємо .Palette поруч із файлом
            palette_path = _find_palette_file(p.filepath)
            palette_entries = _parse_palette(palette_path) if palette_path else []
            base_dir = os.path.dirname(os.path.abspath(p.filepath))

            _assign_palette_materials(
                mesh, faces, base_name,
                palette_entries,
                base_dir,
            )
            # Зберігаємо шлях до Palette в custom properties
            if palette_path:
                mesh.id_data["oxmodel_palette"] = palette_path
            return

        # ── Старі режими ───────────────────────────────────────────────────
        n_slots = p.num_submeshes if mode == "slots" else 1
        n_slots = max(1, min(n_slots, 64))
        for i in range(n_slots):
            mat_name = base_name if n_slots == 1 else f"{base_name}_Mat{i+1:02d}"
            mat = bpy.data.materials.get(mat_name)
            if mat is None:
                mat = bpy.data.materials.new(mat_name)
                mat.use_nodes = True
            mesh.materials.append(mat)
        for poly in mesh.polygons:
            poly.material_index = 0

    # ── fix normals ────────────────────────────────────────────────────────

    def _fix_normals(self, obj, context):
        """Normals → Average → Face Area (виправляє артефакти освітлення)."""
        prev_active = context.view_layer.objects.active
        for o in list(context.selected_objects):
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        try:
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.mesh.average_normals(average_type="FACE_AREA")
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception as e:
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except Exception:
                pass
            print(f"[OxModel] fix_normals warning (non-fatal): {e}")
        finally:
            context.view_layer.objects.active = prev_active

    # ── armature ───────────────────────────────────────────────────────────

    def _build_armature(self, context):
        """
        Створює Blender Armature з bone_matrices (col-major 4×4).

        Особливості:
        - Кістки з одним child: хвіст = голова child (connected=True)
        - Кістки без children: хвіст = голова + напрямок батьківської кістки
        - Кістки з кількома children: хвіст = середня точка між children
        """
        p     = self.p
        scale = self.opt.get("scale", 0.01)
        name  = os.path.splitext(os.path.basename(p.filepath))[0] + "_Armature"

        arm_data = bpy.data.armatures.new(name)
        arm_obj  = bpy.data.objects.new(name, arm_data)
        context.collection.objects.link(arm_obj)

        for o in list(context.selected_objects):
            o.select_set(False)
        arm_obj.select_set(True)
        context.view_layer.objects.active = arm_obj

        bpy.ops.object.mode_set(mode="EDIT")
        eb = arm_data.edit_bones

        NUM     = len(p.bone_matrices)
        parents = p.bone_parents

        # --- обчислюємо голови кісток ---
        heads = []
        for b_idx, mat in enumerate(p.bone_matrices):
            heads.append((
                mat[3][0] * scale,
                mat[3][1] * scale,
                mat[3][2] * scale,
            ))

        # --- карта children ---
        children = {i: [] for i in range(NUM)}
        for i, par in enumerate(parents):
            if 0 <= par < NUM and par != i:
                children[par].append(i)

        # --- мінімальна довжина кістки ---
        MIN_LEN = 0.02

        # --- витягуємо осі кісток з матриць OxModel (col-major 4x4) ---
        # Матриця col-major: mat[0]=col0=gameX, mat[1]=col1=gameY, mat[2]=col2=gameZ
        # Ігровий движок: LOCAL X = первинна вісь кістки (parent→child), 
        #                 LOCAL Y = вісь збоку, LOCAL Z = вгору
        # Blender: LOCAL Y = первинна вісь кістки (head→tail)
        # Відповідність: Blender_X = -gameY = -col1
        #                Blender_Y =  gameX =  col0  (первинна вісь)
        #                Blender_Z =  gameZ =  col2
        bone_y_axes = []   # Blender Y = gameX = col0 (напрямок кістки)
        bone_z_axes = []   # Blender Z = gameZ = col2 (вгору)
        bone_x_axes = []   # Blender X = -gameY = -col1
        for b_idx, mat in enumerate(p.bone_matrices):
            col0 = mathutils.Vector((mat[0][0], mat[0][1], mat[0][2]))  # gameX
            col1 = mathutils.Vector((mat[1][0], mat[1][1], mat[1][2]))  # gameY
            col2 = mathutils.Vector((mat[2][0], mat[2][1], mat[2][2]))  # gameZ
            for v in (col0, col1, col2):
                if v.length > 0.001:
                    v.normalize()
            # Blender axes
            bY = col0                            # bone direction
            bZ = col2                            # up
            bX = mathutils.Vector((-col1.x, -col1.y, -col1.z))  # -gameY
            bone_y_axes.append(bY)
            bone_z_axes.append(bZ)
            bone_x_axes.append(bX)

        # --- створюємо кістки ---
        bones = []
        for b_idx in range(NUM):
            bone = eb.new(f"Bone_{b_idx:03d}")
            bone.head = heads[b_idx]
            bone.tail = (heads[b_idx][0],
                         heads[b_idx][1] + MIN_LEN,
                         heads[b_idx][2])
            bones.append(bone)

        # --- призначаємо батьків (у Edit Mode) ---
        for idx, par in enumerate(parents):
            if 0 <= par < NUM and par != idx:
                bones[idx].parent = bones[par]

        # --- налаштовуємо tail та connected ---
        for b_idx in range(NUM):
            kids = children[b_idx]

            if len(kids) == 1:
                child_head = heads[kids[0]]
                bones[b_idx].tail = child_head
                bones[kids[0]].use_connect = True

            elif len(kids) > 1:
                avg = [
                    sum(heads[c][i] for c in kids) / len(kids)
                    for i in range(3)
                ]
                bones[b_idx].tail = tuple(avg)
                hx,hy,hz = heads[b_idx]
                d = math.sqrt((avg[0]-hx)**2+(avg[1]-hy)**2+(avg[2]-hz)**2)
                if d < MIN_LEN:
                    bones[b_idx].tail = (hx, hy + MIN_LEN, hz)

            else:
                par = parents[b_idx]
                if 0 <= par < NUM:
                    ph = heads[par]
                    bh = heads[b_idx]
                    dx, dy, dz = bh[0]-ph[0], bh[1]-ph[1], bh[2]-ph[2]
                    d  = math.sqrt(dx*dx + dy*dy + dz*dz)
                    if d > 0.001:
                        dx /= d; dy /= d; dz /= d
                    else:
                        dx, dy, dz = 0.0, 1.0, 0.0
                    L = max(MIN_LEN, d * 0.5)
                    bones[b_idx].tail = (bh[0]+dx*L, bh[1]+dy*L, bh[2]+dz*L)
                else:
                    bh = heads[b_idx]
                    bones[b_idx].tail = (bh[0], bh[1] + MIN_LEN, bh[2])

            # Фінальна перевірка мінімальної довжини
            if bones[b_idx].length < 0.001:
                bh = heads[b_idx]
                bones[b_idx].tail = (bh[0], bh[1] + MIN_LEN, bh[2])
                bones[b_idx].use_connect = False

        # --- встановлюємо орієнтацію кісток з матриць OxModel ---
        # EditBone.matrix = 3x3 Matrix (world space), стовпці = [X, Y, Z] осі.
        # Blender_X = -gameY, Blender_Y = gameX, Blender_Z = gameZ
        for b_idx in range(NUM):
            x = bone_x_axes[b_idx]  # -gameY
            y = bone_y_axes[b_idx]  #  gameX (bone direction)
            z = bone_z_axes[b_idx]  #  gameZ (up)

            # Re-orthogonalize: Z = X × Y, then X = Y × Z
            if x.length > 0.001 and y.length > 0.001:
                z2 = x.cross(y)
                if z2.length > 0.001:
                    z = z2.normalized()
                x2 = y.cross(z)
                if x2.length > 0.001:
                    x = x2.normalized()

            # Fallback for degenerate cases
            if x.length < 0.001:
                x = mathutils.Vector((1.0, 0.0, 0.0))
                if abs(y.dot(x)) > 0.9:
                    x = mathutils.Vector((0.0, 0.0, 1.0))
                x = y.cross(x).normalized() if y.cross(x).length > 0.001 else x

            try:
                mat3 = mathutils.Matrix([
                    [x[0], y[0], z[0]],
                    [x[1], y[1], z[1]],
                    [x[2], y[2], z[2]],
                ])
                bones[b_idx].matrix = mat3
            except Exception:
                try:
                    bones[b_idx].align_roll(z)
                except Exception:
                    pass

        bpy.ops.object.mode_set(mode="OBJECT")

        # ── Зберігаємо local rest quaternions для OxAnim ──────────────────
        self._store_rest_quats(arm_obj)

        return arm_obj

    def _store_rest_quats(self, arm_obj):
        """Обчислює та зберігає local rest quaternions кожної кістки як
        custom property на armature для використання OxAnim-імпортером."""
        p = self.p
        rest_quats = []
        bone_parents = p.bone_parents
        NUM = len(p.bone_matrices)

        def mat3_to_quat(mat):
            """Col-major 4x4 mathutils.Matrix → (w,x,y,z) quaternion.

            OxModel stores 4x4 col-major: v[0..3]=col0, v[4..7]=col1...
            _try_4x4 builds Matrix([col0, col1, col2, col3]) so mat[i] = col_i.
            mat[i][j] = col_i[j] = R[j][i] (column index = row in rotation matrix).
            Therefore: R[row][col] = mat[col][row]  ← swap indices!
            """
            import math as _m
            r00=mat[0][0]; r10=mat[0][1]; r20=mat[0][2]
            r01=mat[1][0]; r11=mat[1][1]; r21=mat[1][2]
            r02=mat[2][0]; r12=mat[2][1]; r22=mat[2][2]
            trace = r00+r11+r22
            if trace > 0:
                s = 0.5/_m.sqrt(trace+1.0)
                w=0.25/s; x=(r21-r12)*s; y=(r02-r20)*s; z=(r10-r01)*s
            elif r00>r11 and r00>r22:
                s=2.0*_m.sqrt(1.0+r00-r11-r22)
                w=(r21-r12)/s; x=0.25*s; y=(r01+r10)/s; z=(r02+r20)/s
            elif r11>r22:
                s=2.0*_m.sqrt(1.0+r11-r00-r22)
                w=(r02-r20)/s; x=(r01+r10)/s; y=0.25*s; z=(r12+r21)/s
            else:
                s=2.0*_m.sqrt(1.0+r22-r00-r11)
                w=(r10-r01)/s; x=(r02+r20)/s; y=(r12+r21)/s; z=0.25*s
            mg=_m.sqrt(w*w+x*x+y*y+z*z)
            return (w/mg,x/mg,y/mg,z/mg) if mg>0.001 else (1.0,0.0,0.0,0.0)

        def qmul(q1,q2):
            w1,x1,y1,z1=q1; w2,x2,y2,z2=q2
            return (w1*w2-x1*x2-y1*y2-z1*z2,
                    w1*x2+x1*w2+y1*z2-z1*y2,
                    w1*y2-x1*z2+y1*w2+z1*x2,
                    w1*z2+x1*y2-y1*x2+z1*w2)

        def qinv(q):
            w,x,y,z=q; return (w,-x,-y,-z)

        # World quaternions from matrices (col-major, already stored in parser)
        world_q = [mat3_to_quat(m) for m in p.bone_matrices]

        for b in range(NUM):
            par = bone_parents[b] if b < len(bone_parents) else -1
            if 0 <= par < NUM:
                lq = qmul(qinv(world_q[par]), world_q[b])
            else:
                lq = world_q[b]
            rest_quats.append(lq)

        # Зберігаємо як flat list: [w0,x0,y0,z0, w1,x1,y1,z1, ...]
        flat = [v for q in rest_quats for v in q]
        arm_obj["oxanim_rest_quats"] = flat
        arm_obj["oxanim_num_bones"]  = NUM
        print(f"[OxModel] Stored {NUM} rest quats on armature '{arm_obj.name}'")

    def _assign_bone_weights(self, mesh_obj):
        p = self.p
        if not p.bone_idx or not p.bone_wgt: return

        for b in range(len(p.bone_matrices)):
            mesh_obj.vertex_groups.new(name=f"Bone_{b:03d}")
        vgs = mesh_obj.vertex_groups

        for vi in range(min(len(p.bone_idx), self.p.num_verts)):
            for k in range(4):
                bi = p.bone_idx[vi][k]
                w  = p.bone_wgt[vi][k] if vi < len(p.bone_wgt) else 0.0
                if w > 0.0001 and bi < len(vgs):
                    vgs[bi].add([vi], w, "ADD")

    def _add_armature_modifier(self, mesh_obj, arm_obj):
        mod = mesh_obj.modifiers.new("Armature", "ARMATURE")
        mod.object = arm_obj
        mod.use_vertex_groups = True


# ---------------------------------------------------------------------------
# OPERATORS
# ---------------------------------------------------------------------------

class IMPORT_OT_oxmodel(Operator, ImportHelper):
    """Імпортує .OxModel з Star Control: Origins"""

    bl_idname  = "import_scene.oxmodel"
    bl_label   = "Імпорт OxModel"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".OxModel"
    filter_glob: StringProperty(default="*.OxModel;*.oxmodel", options={"HIDDEN"})

    import_normals: BoolProperty(
        name="Нормалі",
        description="Застосувати кастомні нормалі з файлу",
        default=True)
    fix_normals: BoolProperty(
        name="Fix нормалей (Face Area Average)",
        description=(
            "Автоматично виконати Normals → Average → Face Area.\n"
            "Рекомендується для моделей з артефактами освітлення."
        ),
        default=False)
    import_uvs: BoolProperty(
        name="UV координати",
        default=True)
    material_mode: bpy.props.EnumProperty(
        name="Матеріали",
        description=(
            "Palette — авто-матеріали з .Palette файлу (OxMat NodeGroup + текстури).\n"
            "Slots  — N порожніх слотів, призначати вручну.\n"
            "Single — один матеріал для всієї моделі."
        ),
        items=[
            ("palette", "Авто (Palette + OxMat)", "Читає .Palette, будує NodeGroup з текстурами"),
            ("slots",   "N слотів (вручну)",       "Порожні слоти, призначати вручну"),
            ("single",  "Один матеріал",            "Один матеріал для всієї моделі"),
        ],
        default="palette")
    import_armature: BoolProperty(
        name="Арматура / кістки",
        default=True)
    scale: FloatProperty(
        name="Масштаб",
        description="Множник масштабу (0.01 = ігрові одиниці → метри)",
        default=0.01, min=0.0001, max=100.0)

    def execute(self, context):
        opt = {
            "import_normals":   self.import_normals,
            "fix_normals":      self.fix_normals,
            "import_uvs":       self.import_uvs,
            "material_mode":    self.material_mode,
            "import_armature":  self.import_armature,
            "scale":            self.scale,
        }
        try:
            p = OxModelParser(self.filepath)
            r = OxModelBlenderImporter(p, opt).execute(context)
        except OxModelParseError as e:
            self.report({"ERROR"}, f"Помилка парсингу: {e}")
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, f"Неочікувана помилка: {e}")
            import traceback; traceback.print_exc()
            return {"CANCELLED"}

        if r == {"FINISHED"}:
            n_roots = sum(1 for x in p.bone_parents if x == -1) if p.bone_parents else 0
            self.report({"INFO"},
                f"{os.path.basename(self.filepath)} — "
                f"{p.num_verts} вершин, {len(p.indices)//3} полігонів, "
                f"{len(p.bone_matrices)} кісток ({n_roots} коренів)")
        return r

    def draw(self, context):
        l = self.layout
        l.use_property_split = True
        l.use_property_decorate = False
        c = l.column()
        c.prop(self, "scale")
        c.separator()
        c.prop(self, "import_normals")
        c.prop(self, "fix_normals")
        c.prop(self, "import_uvs")
        c.prop(self, "material_mode")
        c.separator()
        c.prop(self, "import_armature")


class IMPORT_OT_oxmodel_batch(Operator):
    """Масовий імпорт усіх .OxModel з теки"""

    bl_idname  = "import_scene.oxmodel_batch"
    bl_label   = "Mass Import OxModel (folder)"
    bl_options = {"REGISTER", "UNDO"}

    directory:        StringProperty(name="Тека", subtype="DIR_PATH")
    import_normals:   BoolProperty(name="Нормалі",         default=True)
    fix_normals:      BoolProperty(name="Fix норм.",        default=False)
    import_uvs:       BoolProperty(name="UV",               default=True)
    material_mode:    bpy.props.EnumProperty(
        name="Матеріали",
        items=[
            ("palette", "Авто (Palette)", ""),
            ("slots",   "N слотів",       ""),
            ("single",  "Один",           ""),
        ],
        default="palette")
    import_armature:  BoolProperty(name="Арматура",         default=True)
    scale:            FloatProperty(name="Масштаб",         default=0.01,
                                    min=0.0001, max=100.0)
    filter_glob:      StringProperty(default="*.OxModel",   options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if not self.directory or not os.path.isdir(self.directory):
            self.report({"ERROR"}, "Вкажіть правильну теку")
            return {"CANCELLED"}

        files = sorted(f for f in os.listdir(self.directory)
                       if f.lower().endswith(".oxmodel"))
        if not files:
            self.report({"WARNING"}, "Немає .OxModel файлів")
            return {"CANCELLED"}

        opt = {
            "import_normals":   self.import_normals,
            "fix_normals":      self.fix_normals,
            "import_uvs":       self.import_uvs,
            "material_mode":    self.material_mode,
            "import_armature":  self.import_armature,
            "scale":            self.scale,
        }

        ok, errs = 0, []
        for fname in files:
            try:
                p = OxModelParser(os.path.join(self.directory, fname))
                r = OxModelBlenderImporter(p, opt).execute(context)
                if r == {"FINISHED"}:
                    ok += 1
                else:
                    errs.append(fname)
            except Exception as e:
                errs.append(f"{fname}: {e}")

        msg = f"Імпортовано {ok}/{len(files)}"
        if errs:
            msg += f". Помилки: {'; '.join(errs[:3])}"
            self.report({"WARNING"}, msg)
        else:
            self.report({"INFO"}, msg)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# MENU / REGISTER
# ---------------------------------------------------------------------------

def menu_func(self, context):
    self.layout.operator(IMPORT_OT_oxmodel.bl_idname,
                         text="Star Control Origins (.OxModel)")
    self.layout.operator(IMPORT_OT_oxmodel_batch.bl_idname,
                         text="Star Control Origins — масовий імпорт (.OxModel)")


classes = (IMPORT_OT_oxmodel, IMPORT_OT_oxmodel_batch)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func)


if __name__ == "__main__":
    register()
