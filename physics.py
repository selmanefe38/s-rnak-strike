"""
Hızlı çarpışma sistemi (saf numpy, Panda/Ursina collider KULLANMAZ).

Neden: Ursina'nın 'mesh' collider'ı her karede fare ışını tarafından, her ışın atışında da
haritadaki TÜM çokgenlere karşı denenir. Büyük haritada bu oyunu kasar.

Burada üçgenler 3D bir ızgaraya (grid) yerleştirilir; bir ışın ya da bir karakter sadece
yakınındaki hücrelerdeki üçgenlerle test edilir. Testler numpy ile toplu (vektörel) yapılır.
"""
import math

import numpy as np

INF = float("inf")


class Hit:
    __slots__ = ("hit", "distance", "world_point", "world_normal")

    def __init__(self, hit=False, distance=INF, point=None, normal=None):
        self.hit = hit
        self.distance = distance
        self.world_point = point
        self.world_normal = normal


NOHIT = Hit()


def batch_cast(V0, E1, E2, o, d, L):
    """
    R ışını N üçgene karşı aynı anda dener (Möller-Trumbore, çift taraflı).
    o, d: (R,3)  L: (R,)  ->  t (R,)  [çarpmazsa inf],  idx (R,)
    """
    R = o.shape[0]
    if len(V0) == 0:
        return np.full(R, INF), np.zeros(R, np.int64)
    e1x, e1y, e1z = E1[:, 0], E1[:, 1], E1[:, 2]
    e2x, e2y, e2z = E2[:, 0], E2[:, 1], E2[:, 2]
    dx, dy, dz = d[:, 0:1], d[:, 1:2], d[:, 2:3]
    px = dy * e2z - dz * e2y
    py = dz * e2x - dx * e2z
    pz = dx * e2y - dy * e2x
    det = e1x * px + e1y * py + e1z * pz
    ok = np.abs(det) > 1e-10
    inv = 1.0 / np.where(ok, det, 1.0)
    tx = o[:, 0:1] - V0[:, 0]
    ty = o[:, 1:2] - V0[:, 1]
    tz = o[:, 2:3] - V0[:, 2]
    u = (tx * px + ty * py + tz * pz) * inv
    qx = ty * e1z - tz * e1y
    qy = tz * e1x - tx * e1z
    qz = tx * e1y - ty * e1x
    v = (dx * qx + dy * qy + dz * qz) * inv
    t = (e2x * qx + e2y * qy + e2z * qz) * inv
    eps = 1e-4
    hit = ok & (u >= -eps) & (v >= -eps) & (u + v <= 1 + eps) & (t >= 0) & (t <= L[:, None])
    t = np.where(hit, t, INF)
    idx = np.argmin(t, axis=1)
    return t[np.arange(R), idx], idx


class Local:
    """Bir noktanın etrafındaki üçgenlerin önbelleği (karakterler her karede bunu kullanır)."""
    __slots__ = ("V0", "E1", "E2", "N", "TMIN", "TMAX", "c")

    def __init__(self, V0, E1, E2, N, TMIN, TMAX, c):
        self.V0, self.E1, self.E2, self.N = V0, E1, E2, N
        self.TMIN, self.TMAX = TMIN, TMAX
        self.c = np.array(c, np.float64)

    def box(self, lo, hi):
        """AABB'si [lo,hi] ile kesisen ucgenleri dondurur: (V0,E1,E2,N)"""
        m = ((self.TMAX >= lo) & (self.TMIN <= hi)).all(1)
        return self.V0[m], self.E1[m], self.E2[m], self.N[m]

    def cast(self, o, d, L):
        return batch_cast(self.V0, self.E1, self.E2, o, d, L)


class TriMesh:
    def __init__(self, tris, cell=4.0):
        tris = np.asarray(tris, np.float64).reshape(-1, 3, 3)
        V0 = tris[:, 0]
        E1 = tris[:, 1] - V0
        E2 = tris[:, 2] - V0
        N = np.cross(E1, E2)
        ln = np.linalg.norm(N, axis=1)
        good = ln > 1e-9
        self.V0, self.E1, self.E2 = V0[good], E1[good], E2[good]
        self.N = N[good] / ln[good][:, None]
        tris = tris[good]
        self.tmin = tris.min(1)
        self.tmax = tris.max(1)
        self.cs = cell
        self.count = len(self.V0)
        self.cells = {}
        if self.count == 0:
            self.tmin = self.tmax = np.zeros((0, 3))
            self.bmin = np.zeros(3)
            self.bmax = np.zeros(3)
            return
        self.bmin = tris.reshape(-1, 3).min(0)
        self.bmax = tris.reshape(-1, 3).max(0)
        lo = np.floor(tris.min(1) / cell).astype(np.int64)
        hi = np.floor(tris.max(1) / cell).astype(np.int64)
        grid = {}
        lo_l, hi_l = lo.tolist(), hi.tolist()
        for i in range(self.count):
            (x0, y0, z0), (x1, y1, z1) = lo_l[i], hi_l[i]
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    for z in range(z0, z1 + 1):
                        k = (x, y, z)
                        g = grid.get(k)
                        if g is None:
                            grid[k] = [i]
                        else:
                            g.append(i)
        self.cells = {k: np.array(v, np.int64) for k, v in grid.items()}

    # ---- yerel küme
    def local(self, c, half):
        cs = self.cs
        x0, y0, z0 = (int(math.floor((c[i] - half) / cs)) for i in range(3))
        x1, y1, z1 = (int(math.floor((c[i] + half) / cs)) for i in range(3))
        parts = []
        get = self.cells.get
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                for z in range(z0, z1 + 1):
                    a = get((x, y, z))
                    if a is not None:
                        parts.append(a)
        if not parts:
            e = np.zeros((0, 3))
            return Local(e, e, e, e, e, e, c)
        idx = np.unique(np.concatenate(parts)) if len(parts) > 1 else parts[0]
        return Local(self.V0[idx], self.E1[idx], self.E2[idx], self.N[idx],
                     self.tmin[idx], self.tmax[idx], c)

    # ---- tek ışın (DDA ile ızgara geçişi)
    def _cells_along(self, o, d, maxd, limit=600):
        cs = self.cs
        ix = int(math.floor(o[0] / cs))
        iy = int(math.floor(o[1] / cs))
        iz = int(math.floor(o[2] / cs))
        out = [(ix, iy, iz)]
        step = [0, 0, 0]
        tmax = [INF, INF, INF]
        tdel = [INF, INF, INF]
        cur = [ix, iy, iz]
        for a in range(3):
            if d[a] > 1e-12:
                step[a] = 1
                tmax[a] = ((cur[a] + 1) * cs - o[a]) / d[a]
                tdel[a] = cs / d[a]
            elif d[a] < -1e-12:
                step[a] = -1
                tmax[a] = (cur[a] * cs - o[a]) / d[a]
                tdel[a] = -cs / d[a]
        n = 0
        while n < limit:
            a = 0 if (tmax[0] <= tmax[1] and tmax[0] <= tmax[2]) else (1 if tmax[1] <= tmax[2] else 2)
            if tmax[a] > maxd:
                break
            cur[a] += step[a]
            tmax[a] += tdel[a]
            out.append((cur[0], cur[1], cur[2]))
            n += 1
        return out

    def raycast(self, o, d, maxd):
        """o,d: (x,y,z) listeleri; d birim vektör olmalı."""
        if self.count == 0:
            return NOHIT
        ln = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
        if ln < 1e-9:
            return NOHIT
        d = (d[0] / ln, d[1] / ln, d[2] / ln)
        get = self.cells.get
        parts = []
        for k in self._cells_along(o, d, maxd):
            a = get(k)
            if a is not None:
                parts.append(a)
        if not parts:
            return NOHIT
        idx = np.unique(np.concatenate(parts)) if len(parts) > 1 else parts[0]
        oo = np.array([o], np.float64)
        dd = np.array([d], np.float64)
        t, k = batch_cast(self.V0[idx], self.E1[idx], self.E2[idx], oo, dd, np.array([maxd], np.float64))
        if t[0] == INF:
            return NOHIT
        tri = idx[k[0]]
        n = self.N[tri]
        if n[0] * d[0] + n[1] * d[1] + n[2] * d[2] > 0:
            n = -n
        td = float(t[0])
        return Hit(True, td, (o[0] + d[0] * td, o[1] + d[1] * td, o[2] + d[2] * td),
                   (float(n[0]), float(n[1]), float(n[2])))


# ==========================================================================
#  Küre - üçgen en yakın nokta (Ericson, tamamen vektörel)
# ==========================================================================
def closest_points(P, V0, E1, E2):
    """P: (S,3) noktalar, üçgenler (N,3)  ->  (S,N,3) her noktanın her üçgene en yakın noktası"""
    A = V0[None]
    AB = E1[None]
    AC = E2[None]
    ap = P[:, None, :] - A
    d1 = (AB * ap).sum(-1)
    d2 = (AC * ap).sum(-1)
    bp = ap - AB
    d3 = (AB * bp).sum(-1)
    d4 = (AC * bp).sum(-1)
    cp = ap - AC
    d5 = (AB * cp).sum(-1)
    d6 = (AC * cp).sum(-1)
    vc = d1 * d4 - d3 * d2
    vb = d5 * d2 - d1 * d6
    va = d3 * d6 - d5 * d4
    den = va + vb + vc
    den = np.where(np.abs(den) < 1e-12, 1.0, den)
    res = A + AB * (vb / den)[..., None] + AC * (vc / den)[..., None]
    # Ericson sırası: A, B, AB, C, AC, BC, yüz  -> ters sırayla üzerine yaz
    m = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
    t = (d4 - d3) / np.where(m, (d4 - d3) + (d5 - d6), 1.0)
    res = np.where(m[..., None], A + AB + (AC - AB) * t[..., None], res)
    m = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
    t = d2 / np.where(m, d2 - d6, 1.0)
    res = np.where(m[..., None], A + AC * t[..., None], res)
    m = (d6 >= 0) & (d5 <= d6)
    res = np.where(m[..., None], A + AC, res)
    m = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
    t = d1 / np.where(m, d1 - d3, 1.0)
    res = np.where(m[..., None], A + AB * t[..., None], res)
    m = (d3 >= 0) & (d4 <= d3)
    res = np.where(m[..., None], A + AB, res)
    m = (d1 <= 0) & (d2 <= 0)
    res = np.where(m[..., None], np.broadcast_to(A, res.shape), res)
    return res


# ==========================================================================
#  Dünya (tek bir yerde tutulur, Body'ler buradan okur)
# ==========================================================================
class _World:
    mesh = None
    ladders = []


WORLD = _World()


def set_world(mesh, ladders=()):
    WORLD.mesh = mesh
    WORLD.ladders = [(np.array(lo), np.array(hi)) for lo, hi in ladders]


RADIUS = 0.38        # GoldSrc hull 32 birim = 0.8 m
STEP = 0.46          # GoldSrc basamak yüksekliği 18 birim
STAND_H = 1.8
CROUCH_H = 0.95
GRAVITY = 20.0
JUMP_V = 6.8
_GROUND_OFFS = ((0.0, 0.0), (0.26, 0.0), (-0.26, 0.0), (0.0, 0.26), (0.0, -0.26))
_GO = np.array([[x, 0.0, z] for x, z in _GROUND_OFFS])
_DOWN = np.tile(np.array([0.0, -1.0, 0.0]), (len(_GROUND_OFFS), 1))


class Body:
    """
    Karakter kontrolcüsü: dikey bir kapsül yerine üst üste 2-3 küre.
      * duvarlarda kayar, köşelerde takılmaz
      * 0.46 m'ye kadar basamağı otomatik çıkar, rampada yapışır
      * kenarda yarım ayakla durabilir
      * func_ladder merdivenlerini tırmanır
    """

    def __init__(self, pos, height=STAND_H):
        from ursina import Vec3
        self._Vec3 = Vec3
        self.pos = Vec3(*pos)
        self.hv = Vec3(0, 0, 0)
        self.vy = 0.0
        self.grounded = False
        self.height = height
        self.radius = RADIUS
        self.on_ladder = False
        self.trapped = 0
        self._cache = None
        self.last_blocked = False

    # ------------------------------------------------------------ yardımcılar
    def _offsets(self):
        h = self.height
        if h < 1.3:
            return np.array([0.45, max(0.5, h - RADIUS)])
        return np.array([0.5, 0.95, h - RADIUS])

    def _tris(self, p, lo_y, hi_y, pad):
        mesh = WORLD.mesh
        c = self._cache
        if c is None or np.abs(p - c.c).max() > 1.2:
            c = self._cache = mesh.local(p, 3.6)
        lo = np.array([p[0] - pad, lo_y, p[2] - pad])
        hi = np.array([p[0] + pad, hi_y, p[2] + pad])
        return c.box(lo, hi)

    def _resolve(self, p, tris):
        """p'yi üçgenlerden dışarı iter. -> (p, duvar_çarptı, tavan_çarptı, sorunsuz)"""
        V0, E1, E2, N = tris
        if len(V0) == 0:
            return p, False, False, True
        offs = self._offsets()
        S = len(offs)
        r = RADIUS
        p = p.copy()
        C = np.empty((S, 3))
        wall = ceil = False
        for _ in range(6):
            C[:] = p
            C[:, 1] += offs
            Q = closest_points(C, V0, E1, E2)
            D = C[:, None, :] - Q
            d2 = np.einsum("snk,snk->sn", D, D)
            m = d2 < r * r
            if not m.any():
                return p, wall, ceil, True
            dist = np.sqrt(d2)
            ny = D[..., 1] / np.maximum(dist, 1e-9)
            m &= ~((ny > 0.7) & (dist > 1e-6))        # zemin gibi yüzeyler: zemin mantığı halleder
            if not m.any():
                return p, wall, ceil, True
            d2m = np.where(m, d2, INF)
            fi = int(np.argmin(d2m))
            s, n = divmod(fi, d2m.shape[1])
            dd = float(dist[s, n])
            if dd < 1e-6:
                nv = N[n]
                nx, nyv, nz = float(nv[0]), float(nv[1]), float(nv[2])
                dd = 0.0
            else:
                nx, nyv, nz = (float(D[s, n, 0]) / dd, float(D[s, n, 1]) / dd, float(D[s, n, 2]) / dd)
            pen = r - dd
            if nyv < -0.7:                           # tavan
                p[1] += nyv * pen
                ceil = True
            else:                                    # duvar
                hl = math.hypot(nx, nz)
                if hl < 1e-6:
                    nx, nz, hl = 1.0, 0.0, 1.0
                amt = min(pen / hl, 0.5)
                p[0] += nx / hl * amt
                p[2] += nz / hl * amt
                wall = True
        return p, wall, ceil, False

    def _ground(self, p, tris, up, down):
        """Ayağın altındaki en yüksek zemin yüksekliği (yoksa None)."""
        V0, E1, E2, N = tris
        if len(V0) == 0:
            return None
        o = np.empty((len(_GO), 3))
        o[:] = p
        o += _GO
        o[:, 1] += up
        L = np.full(len(_GO), up + down)
        t, idx = batch_cast(V0, E1, E2, o, _DOWN, L)
        ok = t < INF
        if not ok.any():
            return None
        good = ok & (np.abs(N[idx][:, 1]) > 0.7)
        if not good.any():
            return None
        return float((o[good, 1] - t[good]).max())

    def _ladder(self, p):
        for lo, hi in WORLD.ladders:
            if (lo[0] - 0.45 <= p[0] <= hi[0] + 0.45 and lo[2] - 0.45 <= p[2] <= hi[2] + 0.45
                    and lo[1] - 0.2 <= p[1] + 0.9 <= hi[1] + 0.75):
                return True
        return False

    # ------------------------------------------------------------ dışa açık
    def can_stand(self):
        mesh = WORLD.mesh
        if mesh is None or self.height >= 1.3:
            return True
        p = np.array([self.pos.x, self.pos.y, self.pos.z])
        tris = self._tris(p, p[1] - 0.2, p[1] + STAND_H + 0.3, RADIUS + 0.3)
        V0, E1, E2, N = tris
        if len(V0) == 0:
            return True
        offs = np.array([0.5, 0.95, STAND_H - RADIUS])
        C = np.empty((3, 3))
        C[:] = p
        C[:, 1] += offs
        Q = closest_points(C, V0, E1, E2)
        D = C[:, None, :] - Q
        d2 = np.einsum("snk,snk->sn", D, D)
        ny = D[..., 1] / np.maximum(np.sqrt(d2), 1e-9)
        return not ((d2 < (RADIUS - 0.02) ** 2) & (ny < 0.7)).any()

    def set_crouch(self, want):
        if want:
            self.height = CROUCH_H
        elif self.height < 1.3 and self.can_stand():
            self.height = STAND_H

    def settle(self, max_drop=4.0):
        """Doğma noktasını gerçek zemine oturtur."""
        mesh = WORLD.mesh
        if mesh is None:
            return
        p = np.array([self.pos.x, self.pos.y, self.pos.z])
        tris = self._tris(p, p[1] - max_drop - 0.5, p[1] + 3.0, 1.0)
        g = self._ground(p, tris, 1.0, max_drop + 0.5)
        if g is not None:
            self.pos.y = g
            self.grounded = True
            self.vy = 0.0

    def teleport(self, pos):
        self.pos = self._Vec3(*pos)
        self.hv = self._Vec3(0, 0, 0)
        self.vy = 0.0
        self._cache = None
        self.trapped = 0
        self.settle()

    def step(self, dt, wish, speed, jump=False, climb=0.0):
        """
        dt: saniye, wish: yatay yön (Vec3, uzunluk<=1), speed: m/s
        climb: merdivendeyken -1..1 (W/S)
        Dönüş: yatay harekette engel yüzünden belirgin yavaşlama oldu mu
        """
        if WORLD.mesh is None:
            return False
        dt = min(dt, 0.1)
        n = 1 if dt <= 0.034 else int(math.ceil(dt / 0.0334))
        h = dt / n
        blocked = False
        for i in range(n):
            blocked = self._step(h, wish.x, wish.z, speed, jump and i == 0, climb) or blocked
        self.last_blocked = blocked
        return blocked

    def _step(self, dt, wx, wz, speed, jump, climb):
        p = np.array([self.pos.x, self.pos.y, self.pos.z])
        hvx, hvz = self.hv.x, self.hv.z
        acc = 14.0 if self.grounded else 3.5
        k = min(1.0, acc * dt)
        hvx += (wx * speed - hvx) * k
        hvz += (wz * speed - hvz) * k
        mvx, mvz = hvx * dt, hvz * dt
        pad = RADIUS + 0.35 + abs(mvx) + abs(mvz)
        tris = self._tris(p, p[1] - 1.2 - max(0.0, -self.vy * dt), p[1] + self.height + STEP + 0.4, pad)

        # ---- yatay hareket + basamak çıkma
        old = p.copy()
        cand = p + np.array([mvx, 0.0, mvz])
        p1, wall, _, ok = self._resolve(cand, tris)
        moved = math.hypot(mvx, mvz)
        if wall and self.grounded and moved > 1e-6:
            up = p + np.array([0.0, STEP, 0.0])
            pu, _, _, _ = self._resolve(up, tris)
            if pu[1] >= up[1] - 0.03:
                p2, _, _, ok2 = self._resolve(pu + np.array([mvx, 0.0, mvz]), tris)
                dx, dz = mvx / moved, mvz / moved
                g1 = (p1[0] - old[0]) * dx + (p1[2] - old[2]) * dz
                g2 = (p2[0] - old[0]) * dx + (p2[2] - old[2]) * dz
                if g2 > g1 + 0.01 and p2[1] >= up[1] - 0.03:
                    p1, ok = p2, ok2
        p = p1
        got = math.hypot(p[0] - old[0], p[2] - old[2])
        blocked = moved > 1e-6 and got < moved * 0.45
        hvx, hvz = (p[0] - old[0]) / dt, (p[2] - old[2]) / dt

        # ---- dikey hareket
        vy = self.vy
        on_lad = self._ladder(p)
        if on_lad and (abs(climb) > 0.05 or not self.grounded):
            vy = climb * 3.2
            self.grounded = False
            if jump:
                vy = 4.5
                on_lad = False
            p[1] += vy * dt
            hvx *= 0.5
            hvz *= 0.5
        else:
            if jump and self.grounded:
                vy = JUMP_V
                self.grounded = False
            if self.grounded:
                g = self._ground(p, tris, STEP, STEP + 0.05)
                if g is None:
                    self.grounded = False
                    vy = 0.0
                else:
                    p[1] = g
                    vy = 0.0
            if not self.grounded:
                vy = max(vy - GRAVITY * dt, -55.0)
                ny = p[1] + vy * dt
                if vy <= 0:
                    g = self._ground(p, tris, 0.25, 0.25 + (p[1] - ny) + 0.02)
                    if g is not None and g >= ny - 0.02:
                        p[1] = g
                        vy = 0.0
                        self.grounded = True
                    else:
                        p[1] = ny
                else:
                    p[1] = ny
        self.on_ladder = on_lad

        # ---- son düzeltme (zıplarken tavan / duvar)
        p, _, ceil, ok3 = self._resolve(p, tris)
        if ceil and vy > 0:
            vy = 0.0
        self.trapped = 0 if (ok and ok3) else self.trapped + 1

        self.pos.x, self.pos.y, self.pos.z = float(p[0]), float(p[1]), float(p[2])
        self.hv.x, self.hv.z = float(hvx), float(hvz)
        self.vy = float(vy)
        return blocked
