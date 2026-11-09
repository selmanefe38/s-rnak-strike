"""
Bot yol ağı (waypoint grafiği).

Doğma noktalarından başlayıp zemin boyunca yayılır (flood fill). Her kenar, karakterin gerçekten
yürüyebileceği şekilde doğrulanır:
  * zemin kesintisiz mi (boşluk / uçurum yok), basamak ve rampa serbest
  * baş hizasında yeterli boşluk var mı
  * karakter genişliğinde (yanlardan 0.32 m) duvar yok mu
Tüm testler numpy ile toplu yapılır; bir düğüm genişletmesi ~1 ms sürer.
Yükleme sırasında zaman dilimleriyle (work) çalışır, oyun donmaz.
"""
import heapq
import math
import random
import time
from collections import deque

import numpy as np

from physics import INF, batch_cast

_DIRS = []
for _a in range(8):
    _ang = math.radians(_a * 45.0)
    _DIRS.append((math.cos(_ang), math.sin(_ang), 1.0 if _a % 2 == 0 else math.sqrt(2.0)))
_DIRS = np.array(_DIRS)                       # (8,3): dx, dz, uzunluk çarpanı

MAX_RISE = 0.47          # physics.STEP (0.46) + küçük tolerans
_LAT = (-0.32, 0.32)
_HGT = (0.5, 1.35)
_UP = np.array([0.0, 1.0, 0.0])
_DOWN = np.array([0.0, -1.0, 0.0])


class Nav:
    def __init__(self, mesh, seeds, step=1.3, cap=9000):
        from ursina import Vec3
        self._Vec3 = Vec3
        self.mesh = mesh
        self.step = step
        self.cap = cap
        self.pts = []          # np.array([x,y,z])
        self.adj = []
        self.done = []
        self.hash = {}
        self.queue = deque()
        self.arr = np.zeros((0, 3))
        self._arr_n = 0
        self.t_spent = 0.0
        for s in seeds:
            self.add_seed(s)

    # ------------------------------------------------------------ düğümler
    @staticmethod
    def _hk(x, z):
        return (int(math.floor(x / 0.8)), int(math.floor(z / 0.8)))

    def _find(self, x, y, z):
        cx, cz = self._hk(x, z)
        best, bd = None, 1e9
        for ix in (cx - 1, cx, cx + 1):
            for iz in (cz - 1, cz, cz + 1):
                for i in self.hash.get((ix, iz), ()):
                    p = self.pts[i]
                    if abs(p[1] - y) < 0.9:
                        d = (p[0] - x) ** 2 + (p[2] - z) ** 2
                        if d < 0.45 and d < bd:
                            best, bd = i, d
        return best

    def _new(self, x, y, z):
        i = len(self.pts)
        self.pts.append(np.array([x, y, z]))
        self.adj.append([])
        self.done.append(False)
        self.hash.setdefault(self._hk(x, z), []).append(i)
        self.queue.append(i)
        return i

    def add_seed(self, pos):
        """Verilen noktanın altındaki zemini bulup başlangıç düğümü yapar."""
        x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
        loc = self.mesh.local((x, y, z), 2.0)
        V0, E1, E2, N = loc.box(np.array([x - 1.5, y - 4.0, z - 1.5]), np.array([x + 1.5, y + 3.0, z + 1.5]))
        if len(V0) == 0:
            return None
        o = np.array([[x, y + 1.0, z]])
        t, idx = batch_cast(V0, E1, E2, o, _DOWN[None], np.array([5.0]))
        if t[0] == INF:
            return None
        fy = y + 1.0 - float(t[0])
        i = self._find(x, fy, z)
        return i if i is not None else self._new(x, fy, z)

    # ------------------------------------------------------------ genişleme
    def _expand(self, i):
        p = self.pts[i]
        st = self.step
        pad = st * 1.5 + 0.9
        loc = self.mesh.local(p, pad + 0.5)
        V0, E1, E2, N = loc.box(np.array([p[0] - pad, p[1] - 2.0, p[2] - pad]),
                                np.array([p[0] + pad, p[1] + 3.2, p[2] + pad]))
        if len(V0) == 0:
            return
        L = st * _DIRS[:, 2]
        tx = p[0] + _DIRS[:, 0] * L
        tz = p[2] + _DIRS[:, 1] * L

        # komşusu zaten genişletilmiş olan yönleri atla (kenar o taraftan test edildi)
        use = []
        known = {}
        for k in range(8):
            j = self._find(tx[k], p[1], tz[k])
            if j is not None and self.done[j]:
                continue
            use.append(k)
            known[k] = j
        if not use:
            return
        use = np.array(use)
        K = len(use)

        # ---- faz 1: hedef noktaların zemini
        o = np.stack([tx[use], np.full(K, p[1] + 1.3), tz[use]], 1)
        t, idx = batch_cast(V0, E1, E2, o, np.tile(_DOWN, (K, 1)), np.full(K, 3.1))
        ok = (t < INF) & (np.abs(N[idx][:, 1]) > 0.7)
        qy = o[:, 1] - np.where(ok, t, 0.0)
        ok &= np.abs(qy - p[1]) <= 1.3
        if not ok.any():
            return
        sel = np.nonzero(ok)[0]
        use, o, qy = use[sel], o[sel], qy[sel]
        K = len(use)
        Q = np.stack([o[:, 0], qy, o[:, 2]], 1)
        chord = Q - p
        clen = np.linalg.norm(chord, axis=1)
        cdir = chord / clen[:, None]
        perp = np.stack([-cdir[:, 2], np.zeros(K), cdir[:, 0]], 1)
        perp /= np.maximum(np.linalg.norm(perp, axis=1), 1e-9)[:, None]

        # ---- faz 2: tüm doğrulama ışınları tek seferde
        O_list, D_list, L_list = [], [], []
        mids = []
        for f in (1.0 / 3.0, 2.0 / 3.0):
            m = p + chord * f
            O_list.append(np.stack([m[:, 0], m[:, 1] + 0.8, m[:, 2]], 1))
            D_list.append(np.tile(_DOWN, (K, 1)))
            L_list.append(np.full(K, 1.8))
            mids.append(m[:, 1])
        O_list.append(Q + np.array([0.0, 0.05, 0.0]))          # baş boşluğu
        D_list.append(np.tile(_UP, (K, 1)))
        L_list.append(np.full(K, 1.75))
        for h in _HGT:
            for lat in _LAT:
                O_list.append(p + np.array([0.0, h, 0.0]) + perp * lat)
                D_list.append(cdir)
                L_list.append(clen + 0.32)
        O = np.concatenate(O_list)
        D = np.concatenate(D_list)
        Lr = np.concatenate(L_list)
        t, idx = batch_cast(V0, E1, E2, O, D, Lr)
        hit = t < INF
        good = np.ones(K, bool)
        # zemin kesintisiz olmalı: p -> ara1 -> ara2 -> q arasında en fazla basamak yüksekliği kadar fark
        prev = np.full(K, p[1])
        for m_i in range(2):
            sl = slice(m_i * K, (m_i + 1) * K)
            fy = O[sl, 1] - np.where(hit[sl], t[sl], 0.0)
            good &= hit[sl] & (np.abs(fy - prev) <= MAX_RISE)
            prev = fy
        good &= np.abs(Q[:, 1] - prev) <= MAX_RISE
        good &= ~hit[2 * K:3 * K]
        for w in range(4):
            sl = slice((3 + w) * K, (4 + w) * K)
            good &= ~hit[sl]

        for n in range(K):
            if not good[n]:
                continue
            x, y, z = float(Q[n, 0]), float(Q[n, 1]), float(Q[n, 2])
            j = self._find(x, y, z)
            if j is None:
                if len(self.pts) >= self.cap:
                    continue
                j = self._new(x, y, z)
            if j != i and j not in self.adj[i]:
                self.adj[i].append(j)
                self.adj[j].append(i)

    def work(self, seconds=0.03):
        """Kuyruktaki düğümleri verilen süre kadar genişletir. True = bitti."""
        t0 = time.perf_counter()
        while self.queue:
            i = self.queue.popleft()
            if not self.done[i]:
                self.done[i] = True
                self._expand(i)
            if time.perf_counter() - t0 > seconds:
                break
        self.t_spent += time.perf_counter() - t0
        if not self.queue:
            self._finish()
            return True
        return False

    def _finish(self):
        if self._arr_n != len(self.pts):
            self.arr = np.array(self.pts) if self.pts else np.zeros((0, 3))
            self._arr_n = len(self.pts)

    @property
    def count(self):
        return len(self.pts)

    # ------------------------------------------------------------ sorgular
    def vec(self, i):
        p = self.pts[i]
        return self._Vec3(float(p[0]), float(p[1]), float(p[2]))

    def nearest(self, pos):
        if not self.pts:
            return None
        self._finish()
        a = self.arr
        d = (a[:, 0] - pos[0]) ** 2 + (a[:, 2] - pos[2]) ** 2 + ((a[:, 1] - pos[1]) * 3.0) ** 2
        return int(np.argmin(d))

    def random_node(self):
        return random.randrange(len(self.pts)) if self.pts else None

    def path(self, a, b, limit=6000):
        """A*. Dönüş: Vec3 listesi (başlangıç hariç). Yol yoksa []."""
        if a is None or b is None or a == b:
            return [self.vec(b)] if (b is not None and a == b) else []
        tgt = self.pts[b]
        openq = [(0.0, a)]
        came = {a: None}
        g = {a: 0.0}
        n = 0
        while openq and n < limit:
            _, c = heapq.heappop(openq)
            n += 1
            if c == b:
                break
            pc = self.pts[c]
            for nb in self.adj[c]:
                pn = self.pts[nb]
                ng = g[c] + float(np.linalg.norm(pn - pc))
                if nb not in g or ng < g[nb]:
                    g[nb] = ng
                    came[nb] = c
                    heapq.heappush(openq, (ng + float(np.linalg.norm(pn - tgt)), nb))
        if b not in came:
            return []
        out = []
        c = b
        while c is not None:
            out.append(c)
            c = came[c]
        out.reverse()
        return [self.vec(k) for k in out[1:]]

    def component(self, i):
        """i düğümünden ulaşılabilen düğüm sayısı (test/teşhis için)."""
        seen = {i}
        q = deque([i])
        while q:
            c = q.popleft()
            for nb in self.adj[c]:
                if nb not in seen:
                    seen.add(nb)
                    q.append(nb)
        return seen
