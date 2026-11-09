"""
GoldSrc (Half-Life / CS 1.6) dosya okuyuculari
  - BSP v30  (harita)
  - WAD3     (harita dokulari)
  - MDL v10  (Studio model: silah, karakter)
Hepsi saf Python + numpy + Pillow. Cikti Ursina koordinatlarina cevrilmis ham listelerdir.
"""
import os
import re
import struct
import math
from pathlib import Path

import numpy as np
from PIL import Image

KNOWN_ROOTS = ("maps", "models", "sound", "sprites", "media", "gfx")

# Haritada cizilmeyecek arac dokulari
RENDER_SKIP = {"aaatrigger", "null", "hint", "skip", "origin", "clip",
               "trigger", "bevel", "black_hidden", "noclip", "sky"}
# Carpismaya girmeyecek arac dokulari
COLLIDE_SKIP = {"aaatrigger", "origin", "hint", "skip", "trigger", "noclip"}


# --------------------------------------------------------------------------
#  Dosya indeksi: klasoru bir kere tarar, "maps/..", "models/.." gibi
#  yollari bulunduklari yerden bagimsiz cozer (cstrike alt klasoru olsa da olur)
# --------------------------------------------------------------------------
class FileIndex:
    def __init__(self, root):
        self.root = Path(root)
        self.files = {}      # normalize yol -> Path
        self.all = []        # tum dosyalar
        skip = {"__pycache__", ".git", "venv", ".venv", "node_modules"}
        for dp, dn, fn in os.walk(self.root):
            dn[:] = [d for d in dn if d.lower() not in skip]
            for f in fn:
                p = Path(dp) / f
                self.all.append(p)
                key = self._norm(p.relative_to(self.root))
                self.files.setdefault(key, p)

    @staticmethod
    def _norm(rel):
        parts = [x.lower() for x in Path(rel).parts]
        for i, part in enumerate(parts):
            if part in KNOWN_ROOTS:
                return "/".join(parts[i:])
        return "/".join(parts)

    def find(self, rel):
        return self.files.get(rel.lower().replace("\\", "/"))

    def ext(self, suffix):
        s = suffix.lower()
        return sorted((p for p in self.all if p.suffix.lower() == s),
                      key=lambda p: p.name.lower())

    def under(self, folder, suffix):
        folder = folder.lower().rstrip("/") + "/"
        return sorted((p for k, p in self.files.items()
                       if k.startswith(folder) and p.suffix.lower() == suffix.lower()),
                      key=lambda p: p.name.lower())


# --------------------------------------------------------------------------
#  Miptex (BSP icindeki ve WAD icindeki doku formati)
# --------------------------------------------------------------------------
def parse_miptex(buf, off):
    """buf[off:] bir miptex yapisi. (ad, w, h, PIL.Image|None) dondurur."""
    name = buf[off:off + 16].split(b"\0")[0].decode("latin1")
    w, h = struct.unpack_from("<II", buf, off + 16)
    offs = struct.unpack_from("<4I", buf, off + 24)
    if offs[0] == 0 or w == 0 or h == 0 or w > 4096 or h > 4096:
        return name, w, h, None
    pix = np.frombuffer(buf, np.uint8, w * h, off + offs[0])
    pal_off = off + offs[3] + (w // 8) * (h // 8) + 2
    pal = np.frombuffer(buf, np.uint8, 768, pal_off).reshape(256, 3)
    rgb = pal[pix].reshape(h, w, 3)
    alpha = np.full((h, w), 255, np.uint8)
    if name.startswith("{"):
        alpha[pix.reshape(h, w) == 255] = 0
        # seffaf piksellerin renk tasmasini onle
        rgb = rgb.copy()
        rgb[pix.reshape(h, w) == 255] = (0, 0, 0)
    img = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
    return name, w, h, img


class WadSet:
    """Klasordeki tum .wad dosyalarini indeksler."""

    def __init__(self, paths):
        self.entries = {}
        self.datas = []
        for p in paths:
            try:
                data = Path(p).read_bytes()
                if data[:4] != b"WAD3":
                    continue
                n, infoofs = struct.unpack_from("<ii", data, 4)
                di = len(self.datas)
                self.datas.append(data)
                for i in range(n):
                    filepos, disksize, size, typ, comp, _pad, nm = struct.unpack_from(
                        "<iiiBBh16s", data, infoofs + 32 * i)
                    if typ != 0x43:
                        continue
                    name = nm.split(b"\0")[0].decode("latin1").lower()
                    self.entries.setdefault(name, (di, filepos))
            except Exception as e:  # bozuk wad'i atla
                print("WAD okunamadi:", p, e)

    def get(self, name):
        e = self.entries.get(name.lower())
        if not e:
            return None
        di, pos = e
        return parse_miptex(self.datas[di], pos)


def _placeholder(w=64, h=64, c1=(90, 90, 90), c2=(60, 60, 60)):
    a = np.zeros((h, w, 4), np.uint8)
    a[..., 3] = 255
    yy, xx = np.mgrid[0:h, 0:w]
    m = ((xx // 8 + yy // 8) % 2) == 0
    a[m, :3] = c1
    a[~m, :3] = c2
    return Image.fromarray(a, "RGBA")


# --------------------------------------------------------------------------
#  BSP  (tamamen vektörel: büyük haritalar da saniyeler içinde yüklenir)
# --------------------------------------------------------------------------
_SHADE_LEVELS = 16
_LIGHT_DIR = np.array([0.35, 0.25, 0.9])
_LIGHT_DIR = _LIGHT_DIR / np.linalg.norm(_LIGHT_DIR)
CHUNK = 40.0     # çizim parçası boyutu (metre): kamera görmediği parçaları çizmez

# Kapı gibi hareketli şeyler dışarıda bırakılır (hep açık kabul edilir) ->
# haritanın her yeri gezilebilir kalır.
SOLID_CLASSES = {"func_wall", "func_breakable", "func_conveyor", "func_pushable"}
RENDER_ONLY_CLASSES = {"func_illusionary"}
LADDER_CLASSES = {"func_ladder"}


def parse_entities(text):
    out = []
    for blk in re.findall(r"\{([^}]*)\}", text):
        out.append(dict(re.findall(r'"([^"]*)"\s+"([^"]*)"', blk)))
    return out


def _to_int(v, default=0):
    try:
        return int(float(v))
    except Exception:
        return default


def load_bsp(path, wadset=None, scale=0.025):
    data = Path(path).read_bytes()
    ver = struct.unpack_from("<i", data, 0)[0]
    if ver != 30:
        raise ValueError(f"Desteklenmeyen BSP surumu: {ver} (GoldSrc BSP30 gerekli)")
    lumps = [struct.unpack_from("<ii", data, 4 + 8 * i) for i in range(15)]

    def L(i):
        o, l = lumps[i]
        return data[o:o + l]

    ents = parse_entities(L(0).split(b"\0")[0].decode("latin1"))

    planes = np.frombuffer(L(1), np.dtype([("n", "<f4", 3), ("d", "<f4"), ("t", "<i4")]))
    verts = np.frombuffer(L(3), "<f4").reshape(-1, 3).astype(np.float64)
    texinfo = np.frombuffer(L(6), np.dtype([("s", "<f4", 3), ("ss", "<f4"), ("t", "<f4", 3),
                                            ("ts", "<f4"), ("mip", "<i4"), ("flags", "<i4")]))
    faces = np.frombuffer(L(7), np.dtype([("plane", "<u2"), ("side", "<u2"), ("first", "<i4"),
                                          ("num", "<u2"), ("ti", "<u2"), ("styles", "u1", 4),
                                          ("light", "<i4")]))
    edges = np.frombuffer(L(12), "<u2").reshape(-1, 2).astype(np.int64)
    surfedges = np.frombuffer(L(13), "<i4").astype(np.int64)
    mdl_lump = L(14)
    n_models = len(mdl_lump) // 64
    models = [struct.unpack_from("<9f7i", mdl_lump, 64 * i) for i in range(n_models)]

    # --- dokular
    tex_lump = L(2)
    nm = struct.unpack_from("<i", tex_lump, 0)[0]
    offs = struct.unpack_from("<%di" % nm, tex_lump, 4)
    mips = []
    for o in offs:
        if o <= 0:
            mips.append(("null", 64, 64, None))
            continue
        name, w, h, img = parse_miptex(tex_lump, o)
        if img is None and wadset is not None:
            ext = wadset.get(name)
            if ext and ext[3] is not None:
                name, w, h, img = ext
        mips.append((name, w or 64, h or 64, img))
    n_mip = len(mips)
    lnames = [m[0].lower() for m in mips]
    tw = np.array([m[1] for m in mips], np.float64)
    th = np.array([m[2] for m in mips], np.float64)
    r_ok = np.array([not (n in RENDER_SKIP or n.startswith("sky")) for n in lnames])
    c_ok = np.array([(n not in COLLIDE_SKIP) and not n.startswith("!") for n in lnames])
    water = np.array([n.startswith("!") for n in lnames])
    masked = np.array([m[0].startswith("{") for m in mips])

    # --- hangi modeller dahil
    segments = []     # (first, num, collide, alpha)
    ladders = []
    m0 = models[0]
    segments.append((m0[14], m0[15], True, 1.0))
    for e in ents:
        mk = e.get("model", "")
        if not mk.startswith("*"):
            continue
        idx = _to_int(mk[1:], -1)
        if idx <= 0 or idx >= n_models:
            continue
        cn = e.get("classname", "")
        md = models[idx]
        if cn in SOLID_CLASSES or cn in RENDER_ONLY_CLASSES:
            alpha = 1.0
            rm, ra = _to_int(e.get("rendermode", 0)), _to_int(e.get("renderamt", 255), 255)
            if rm in (2, 3, 5) and 0 < ra < 255:
                alpha = round(ra / 255.0, 1)
            segments.append((md[14], md[15], cn in SOLID_CLASSES, alpha))
        elif cn in LADDER_CLASSES:
            lo = (md[0] * scale, md[2] * scale, md[1] * scale)
            hi = (md[3] * scale, md[5] * scale, md[4] * scale)
            ladders.append((lo, hi))

    groups = {}
    coll_parts = []
    all_pts = []
    missing = set()

    for first_face, num_faces, collide, seg_alpha in segments:
        if num_faces <= 0:
            continue
        F = faces[first_face:first_face + num_faces]
        mip_f = texinfo["mip"][F["ti"]].astype(np.int64)
        keep = (F["num"] >= 3) & (mip_f >= 0) & (mip_f < n_mip)
        F = F[keep]
        mip_f = mip_f[keep]
        if len(F) == 0:
            continue
        num = F["num"].astype(np.int64)
        first = F["first"].astype(np.int64)

        # köşe noktaları
        tot = int(num.sum())
        start = np.cumsum(num) - num
        within = np.arange(tot) - np.repeat(start, num)
        se = surfedges[np.repeat(first, num) + within]
        vi = edges[np.abs(se), (se < 0).astype(np.int64)]
        P = verts[vi]

        # fan üçgenleme
        tc = num - 2
        ttot = int(tc.sum())
        tface = np.repeat(np.arange(len(F)), tc)
        tstart = np.cumsum(tc) - tc
        li = np.arange(ttot) - np.repeat(tstart, tc) + 1
        a = start[tface]
        b = a + li
        c = b + 1
        tri_gs = np.stack([P[a], P[b], P[c]], 1)                    # (T,3,3) GS koordinatı
        tri_u = tri_gs[:, :, [0, 2, 1]] * scale                     # Ursina: (x, z, y)
        all_pts.append(tri_u.reshape(-1, 3))
        mip_t = mip_f[tface]

        if collide:
            sel = c_ok[mip_t]
            if sel.any():
                coll_parts.append(tri_u[sel])

        rsel = r_ok[mip_t]
        if not rsel.any():
            continue
        ti_f = F["ti"].astype(np.int64)
        s_vec = texinfo["s"][ti_f].astype(np.float64)
        t_vec = texinfo["t"][ti_f].astype(np.float64)
        ss = texinfo["ss"][ti_f].astype(np.float64)
        ts = texinfo["ts"][ti_f].astype(np.float64)
        s_t, t_t = s_vec[tface], t_vec[tface]
        U = (np.einsum("tvk,tk->tv", tri_gs, s_t) + ss[tface][:, None]) / tw[mip_t][:, None]
        Vv = (np.einsum("tvk,tk->tv", tri_gs, t_t) + ts[tface][:, None]) / th[mip_t][:, None]
        uv = np.stack([U, 1.0 - Vv], 2)                             # (T,3,2)

        n = planes["n"][F["plane"]].astype(np.float64)
        n = np.where((F["side"] != 0)[:, None], -n, n)
        diff = np.maximum(0.0, n @ _LIGHT_DIR)
        lvl_f = np.rint((0.55 + 0.45 * diff) * (_SHADE_LEVELS - 1)).astype(np.int32)
        lvl_t = lvl_f[tface]

        cen = tri_u.mean(1)
        ck = np.stack([mip_t, np.floor(cen[:, 0] / CHUNK).astype(np.int64),
                       np.floor(cen[:, 2] / CHUNK).astype(np.int64)], 1)
        ck = ck[rsel]
        tri_r, uv_r, lvl_r = tri_u[rsel], uv[rsel], lvl_t[rsel]
        uniq, inv = np.unique(ck, axis=0, return_inverse=True)
        inv = inv.reshape(-1)
        order = np.argsort(inv, kind="stable")
        counts = np.bincount(inv, minlength=len(uniq))
        ends = np.cumsum(counts)
        begs = ends - counts
        for gi in range(len(uniq)):
            sl = order[begs[gi]:ends[gi]]
            mi = int(uniq[gi][0])
            key = (mi, int(uniq[gi][1]), int(uniq[gi][2]), seg_alpha)
            g = groups.get(key)
            if g is None:
                name, w_, h_, img = mips[mi]
                if img is None:
                    missing.add(name)
                    img = mips[mi] = (name, w_, h_, _placeholder())
                    img = mips[mi][3]
                al = 0.55 if water[mi] else seg_alpha
                g = groups[key] = dict(name=name, img=img, v=[], uv=[], lvl=[], alpha=al,
                                       masked=bool(masked[mi]))
            g["v"].append(tri_r[sl].reshape(-1, 3))
            g["uv"].append(uv_r[sl].reshape(-1, 2))
            g["lvl"].append(np.repeat(lvl_r[sl], 3))

    out_groups = []
    for g in groups.values():
        out_groups.append(dict(
            name=g["name"], img=g["img"], alpha=g["alpha"], masked=g["masked"],
            verts=[tuple(x) for x in np.concatenate(g["v"]).tolist()],
            uvs=[tuple(x) for x in np.concatenate(g["uv"]).tolist()],
            levels=np.concatenate(g["lvl"]).tolist()))

    coll = np.concatenate(coll_parts) if coll_parts else np.zeros((0, 3, 3))

    # --- spawn noktalari
    spawns = {"ct": [], "t": [], "any": []}
    for e in ents:
        cn = e.get("classname", "")
        if cn not in ("info_player_start", "info_player_deathmatch",
                      "info_player_terrorist", "info_player_counterterrorist"):
            continue
        try:
            ox, oy, oz = [float(x) for x in e["origin"].split()]
        except Exception:
            continue
        ang = float(e.get("angle", "0") or 0)
        pos = (ox * scale, oz * scale, oy * scale)
        yaw = 90.0 - ang
        item = (pos, yaw)
        spawns["any"].append(item)
        if cn in ("info_player_start", "info_player_counterterrorist"):
            spawns["ct"].append(item)
        else:
            spawns["t"].append(item)

    allp = np.concatenate(all_pts) if all_pts else np.zeros((1, 3))
    bounds = (tuple(float(x) for x in allp.min(0)), tuple(float(x) for x in allp.max(0)))
    return dict(groups=out_groups, coll=coll, spawns=spawns, bounds=bounds, ladders=ladders,
                missing=sorted(missing), levels=_SHADE_LEVELS,
                face_count=int(sum(s[1] for s in segments)))


# --------------------------------------------------------------------------
#  MDL (Studio v10) - sadece 0. sekansin 0. karesi (statik poz)
# --------------------------------------------------------------------------
def _angle_matrix(ang):
    """Studio euler (roll=a0, pitch=a1, yaw=a2) -> 3x3 donusum."""
    sr, cr = math.sin(ang[0] * 0.5), math.cos(ang[0] * 0.5)
    sp, cp = math.sin(ang[1] * 0.5), math.cos(ang[1] * 0.5)
    sy, cy = math.sin(ang[2] * 0.5), math.cos(ang[2] * 0.5)
    x = sr * cp * cy - cr * sp * sy
    y = cr * sp * cy + sr * cp * sy
    z = cr * cp * sy - sr * sp * cy
    w = cr * cp * cy + sr * sp * sy
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])


def load_mdl(path, scale=0.025):
    """
    Dondurur: liste of dict(img, verts, uvs, masked)  (Ursina koordinatlari, ayakta=0)
    Model eksenleri: GS +x ileri -> Ursina +z, GS +y sol -> Ursina -x, GS +z yukari -> +y
    """
    d = Path(path).read_bytes()
    if d[:4] != b"IDST":
        raise ValueError("Gecerli bir MDL degil (IDST degil): " + str(path))
    (numbones, boneindex, _nbc, _bci, _nhb, _hbi, numseq, seqindex, _nsg, _sgi,
     numtex, texindex, _texdata, numskinref, numskinfam, skinindex,
     numbody, bodyindex) = struct.unpack_from("<18i", d, 140)

    # --- kemikler, 0. sekans 0. kare
    bones = []
    for i in range(numbones):
        o = boneindex + 112 * i
        parent = struct.unpack_from("<i", d, o + 32)[0]
        value = struct.unpack_from("<6f", d, o + 64)
        sc = struct.unpack_from("<6f", d, o + 88)
        bones.append((parent, list(value), sc))

    anim_base = None
    if numseq > 0:
        anim_base = struct.unpack_from("<i", d, seqindex + 124)[0]
        if anim_base <= 0 or anim_base >= len(d):
            anim_base = None

    world = []
    for i, (parent, value, sc) in enumerate(bones):
        val = list(value)
        if anim_base is not None:
            ao = anim_base + i * 12
            if ao + 12 <= len(d):
                offs = struct.unpack_from("<6H", d, ao)
                for j in range(6):
                    if offs[j] and ao + offs[j] + 4 <= len(d):
                        v = struct.unpack_from("<h", d, ao + offs[j] + 2)[0]
                        val[j] = value[j] + v * sc[j]
        m = np.eye(4)
        m[:3, :3] = _angle_matrix(val[3:6])
        m[:3, 3] = val[0:3]
        if parent >= 0 and parent < len(world):
            m = world[parent] @ m
        world.append(m)
    if not world:
        world = [np.eye(4)]

    # --- dokular (ana dosyada yoksa 'adt.mdl' kardes dosyasindan)
    td = d
    if numtex == 0:
        sib = Path(path).with_name(Path(path).stem + "t.mdl")
        if sib.exists():
            t_data = sib.read_bytes()
            if t_data[:4] == b"IDST":
                td = t_data
                numtex, texindex = struct.unpack_from("<2i", td, 180)
    textures = []
    for i in range(numtex):
        o = texindex + 80 * i
        nm = td[o:o + 64].split(b"\0")[0].decode("latin1")
        flags, w, h, idx = struct.unpack_from("<4i", td, o + 64)
        masked = bool(flags & 0x40)
        if w <= 0 or h <= 0 or idx <= 0 or idx + w * h + 768 > len(td):
            textures.append((nm, 1, 1, _placeholder(), False))
            continue
        pix = np.frombuffer(td, np.uint8, w * h, idx)
        pal = np.frombuffer(td, np.uint8, 768, idx + w * h).reshape(256, 3)
        rgb = pal[pix].reshape(h, w, 3)
        alpha = np.full((h, w), 255, np.uint8)
        if masked:
            alpha[pix.reshape(h, w) == 255] = 0
        textures.append((nm, w, h, Image.fromarray(np.dstack([rgb, alpha]), "RGBA"), masked))

    # --- govde parcalari: her bodypart'tan ilk dolu model
    out = {}
    for bp in range(numbody):
        o = bodyindex + 76 * bp
        nmodels, _base, modelindex = struct.unpack_from("<3i", d, o + 64)
        chosen = None
        for mi in range(nmodels):
            mo = modelindex + 112 * mi
            nummesh = struct.unpack_from("<i", d, mo + 72)[0]
            if nummesh > 0:
                chosen = mo
                break
        if chosen is None:
            continue
        mo = chosen
        nummesh, meshindex = struct.unpack_from("<2i", d, mo + 72)
        numverts, vertinfoindex, vertindex = struct.unpack_from("<3i", d, mo + 80)
        vb = np.frombuffer(d, np.uint8, numverts, vertinfoindex)
        vp = np.frombuffer(d, "<f4", numverts * 3, vertindex).reshape(-1, 3).astype(np.float64)
        # kemik donusumu
        vw = np.empty_like(vp)
        for b in np.unique(vb):
            sel = vb == b
            M = world[b] if b < len(world) else np.eye(4)
            vw[sel] = vp[sel] @ M[:3, :3].T + M[:3, 3]
        # GS -> Ursina (model): (x,y,z) -> (-y, z, x)
        U = np.stack([-vw[:, 1], vw[:, 2], vw[:, 0]], 1) * scale

        for k in range(nummesh):
            mo2 = meshindex + 20 * k
            _numtris, triindex, skinref = struct.unpack_from("<3i", d, mo2)
            tex_i = 0
            if skinindex > 0 and numskinref > 0:
                tex_i = struct.unpack_from("<h", d, skinindex + 2 * skinref)[0]
            if tex_i < 0 or tex_i >= len(textures):
                tex_i = 0
            if not textures:
                continue
            _nm, tw, th, img, masked = textures[tex_i]
            g = out.setdefault(tex_i, dict(img=img, masked=masked, verts=[], uvs=[]))
            p = triindex
            while p + 2 <= len(d):
                n = struct.unpack_from("<h", d, p)[0]
                p += 2
                if n == 0:
                    break
                fan = n < 0
                n = abs(n)
                vs = []
                for _ in range(n):
                    vi, ni, s, t = struct.unpack_from("<4h", d, p)
                    p += 8
                    vs.append((vi, s, t))
                tris = []
                if fan:
                    for i in range(1, n - 1):
                        tris.append((0, i, i + 1))
                else:
                    for i in range(n - 2):
                        tris.append((i, i + 1, i + 2) if i % 2 == 0 else (i + 1, i, i + 2))
                for a, b, c in tris:
                    for q in (a, b, c):
                        vi, s, t = vs[q]
                        if vi >= len(U):
                            vi = 0
                        g["verts"].append(tuple(U[vi].tolist()))
                        g["uvs"].append((s / tw, 1.0 - t / th))
    return list(out.values())
