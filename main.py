"""
ŞIRNAK STRIKE  -  CS 1.6 / CZ tarzı FPS  (Python + Ursina)

Bu dosyayı 'şırnak strike' klasörüne koy. Klasörde şunlar otomatik taranır:
    maps/*.bsp           -> oynanabilir haritalar (menüde listelenir)
    *.wad                -> harita dokuları (klasörün neresinde olursa)
    models/v_*.mdl       -> silah modelleri (elde görünen)
    models/player/*/*.mdl-> bot karakter modelleri
    sound/...            -> silah / vuruş sesleri
cstrike alt klasörü içinde olsa da bulur.
"""
import json
import math
import random
from collections import deque
from pathlib import Path

from ursina import *
from panda3d.core import TransparencyAttrib, Filename, loadPrcFileData

import gs_formats as gs
import physics
from physics import Body
from nav import Nav

ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = ROOT / "ayarlar.json"
SCALE = 0.025            # GoldSrc birimi -> metre
GRAVITY = 20.0
VM_OFFSET = Vec3(0.0, 0.0, 0.0)   # elde görünen silahın konumu (gerekirse oynat)
VM_SCALE = 1.0

# ---------------------------------------------------------------- renkler
def C(r, g, b, a=255):
    return color.Color(r / 255, g / 255, b / 255, a / 255)

BG = C(12, 14, 20)
PANEL = C(22, 26, 36, 235)
PANEL2 = C(34, 40, 54, 255)
AMBER = C(255, 176, 0)
RED = C(214, 48, 48)
WHITE = C(235, 238, 245)
GREY = C(150, 158, 172)
GREEN = C(90, 220, 110)
BLUE = C(80, 140, 255)

DEFAULTS = dict(sens=40.0, fov=90.0, volume=0.7, bots=5, difficulty=1,
                kill_limit=30, team="ct", fps=False, fullscreen=False)

def load_settings():
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return s

def save_settings():
    try:
        SETTINGS_FILE.write_text(json.dumps(S, indent=2), encoding="utf-8")
    except Exception:
        pass

S = load_settings()
DIFFS = [("Kolay", 0.35, 14), ("Normal", 0.55, 20), ("Zor", 0.75, 26), ("Uzman", 0.92, 34)]

# ---------------------------------------------------------------- uygulama
# performans: MSAA kapalı, dikey senkron açık, gereksiz GL hata kontrolü kapalı
loadPrcFileData("", "framebuffer-multisample 0\nsync-video 1\ngl-check-errors 0\ntextures-power-2 none\n"
                    "audio-library-name p3openal_audio\n")
app = Ursina(title="ŞIRNAK STRIKE", borderless=False, development_mode=False)
# fare her karede tüm sahneyi çarpışma için taramasın (büyük haritada çok yavaşlatıyordu)
mouse.traverse_target = None
window.exit_button.visible = False
window.fps_counter.enabled = S["fps"]
window.color = BG
if S["fullscreen"]:
    window.fullscreen = True

INDEX = gs.FileIndex(ROOT)
MAPS = INDEX.under("maps", ".bsp")
N_MDL = len(INDEX.ext(".mdl"))
N_WAV = len(INDEX.ext(".wav"))
N_WAD = len(INDEX.ext(".wad"))

# ---------------------------------------------------------------- ses
class Sounds:
    def __init__(self):
        self.cache = {}
        self.music = None

    def _load(self, rel):
        if rel in self.cache:
            return self.cache[rel]
        p = INDEX.find("sound/" + rel)
        lst = None
        if p:
            try:
                lst = [app.loader.loadSfx(Filename.fromOsSpecific(str(p))) for _ in range(3)]
            except Exception as e:
                print("Ses yüklenemedi:", rel, e)
        self.cache[rel] = [lst, 0] if lst else None
        return self.cache[rel]

    def play(self, rels, vol=1.0):
        if isinstance(rels, str):
            rels = [rels]
        rel = random.choice(rels)
        c = self._load(rel)
        if not c:
            return
        lst, i = c
        snd = lst[i % 3]
        c[1] += 1
        try:
            snd.setVolume(max(0.0, min(1.0, vol * S["volume"])))
            snd.play()
        except Exception:
            pass

    def start_music(self):
        p = INDEX.find("media/gamestartup.mp3")
        if not p or self.music:
            return
        try:
            self.music = app.loader.loadMusic(Filename.fromOsSpecific(str(p)))
            self.music.setLoop(True)
            self.music.setVolume(0.35 * S["volume"])
            self.music.play()
        except Exception:
            self.music = None

    def stop_music(self):
        if self.music:
            try:
                self.music.stop()
            except Exception:
                pass
            self.music = None

SND = Sounds()

# ---------------------------------------------------------------- silahlar
WEAPONS = {
    "knife": dict(name="Bıçak", mdl="v_knife", slot=3, price=0, dmg=45, rof=0.45, mag=0, reserve=0,
                  auto=False, melee=True, spread=0, kick=0, fire=["weapons/knife_slash1.wav", "weapons/knife_slash2.wav"],
                  speed=1.1),
    "deagle": dict(name="Desert Eagle", mdl="v_deagle", slot=2, price=650, dmg=58, rof=0.28, mag=7, reserve=35,
                   auto=False, spread=0.006, kick=2.2, fire=["weapons/deagle-1.wav"], speed=1.0,
                   reload=["weapons/de_clipout.wav"], rtime=2.0),
    "mp5": dict(name="MP5", mdl="v_mp5", slot=1, price=1500, dmg=26, rof=0.075, mag=30, reserve=120,
                auto=True, spread=0.012, kick=0.9, fire=["weapons/mp5-1.wav", "weapons/mp5-2.wav"], speed=0.97,
                reload=["weapons/mp5_clipout.wav"], rtime=2.6),
    "ak47": dict(name="AK-47", mdl="v_ak47", slot=1, price=2500, dmg=36, rof=0.1, mag=30, reserve=90,
                 auto=True, spread=0.014, kick=1.5, fire=["weapons/ak47-1.wav", "weapons/ak47-2.wav"], speed=0.93,
                 reload=["weapons/ak47_clipout.wav"], rtime=2.4),
    "m4a1": dict(name="M4A1", mdl="v_m4a1", slot=1, price=3100, dmg=33, rof=0.09, mag=30, reserve=90,
                 auto=True, spread=0.011, kick=1.2, fire=["weapons/m4a1_unsil-1.wav", "weapons/m4a1_unsil-2.wav"],
                 speed=0.95, reload=["weapons/m4a1_clipout.wav"], rtime=2.4),
    "awp": dict(name="AWP", mdl="v_awp", slot=1, price=4750, dmg=115, rof=1.45, mag=10, reserve=30,
                auto=False, spread=0.0015, kick=4.0, fire=["weapons/awp1.wav"], speed=0.85, scope=True,
                reload=["weapons/awp_clipin.wav"], rtime=2.9),
}
BUY_ORDER = ["deagle", "mp5", "ak47", "m4a1", "awp"]

NAMES = ["Tuncay", "Hasan", "Berk", "Emre", "Cem", "Onur", "Sinan", "Kerem", "Barış", "Volkan",
         "Alper", "Murat", "Deniz", "Yiğit", "Arda", "Tolga", "Serkan", "Oğuz"]
CT_MODELS = ["gign", "urban", "gsg9", "sas"]
T_MODELS = ["leet", "terror", "arctic", "guerilla"]

# ---------------------------------------------------------------- yardımcılar
def set_text(t, s):
    """Ursina Text her atamada yeniden çizilir (yavaş): sadece değiştiyse ata."""
    if getattr(t, '_st', None) != s:
        t._st = s
        t.text = s

def vec_yaw(d):
    return math.degrees(math.atan2(d.x, d.z))

def flat(v):
    return Vec3(v.x, 0, v.z)

def ang_diff(a, b):
    return (a - b + 180) % 360 - 180

def ray_sphere(o, d, c, r):
    oc = o - c
    b = oc.dot(d)
    cc = oc.dot(oc) - r * r
    disc = b * b - cc
    if disc < 0:
        return None
    t = -b - math.sqrt(disc)
    if t < 0:
        t = -b + math.sqrt(disc)
        if t < 0:
            return None
    return t

# model önbelleği: ham liste -> her seferinde yeni Mesh
MDL_CACHE = {}

def get_mdl(path):
    key = str(path)
    if key not in MDL_CACHE:
        try:
            MDL_CACHE[key] = gs.load_mdl(path, SCALE)
        except Exception as e:
            print("MDL okunamadı:", path, e)
            MDL_CACHE[key] = None
    return MDL_CACHE[key]

def build_model_entity(groups, parent, **kw):
    root = Entity(parent=parent if parent is not None else scene, **kw)
    for g in groups:
        if not g["verts"]:
            continue
        e = Entity(parent=root, model=Mesh(vertices=g["verts"], uvs=g["uvs"], mode="triangle"),
                   texture=Texture(g["img"]), double_sided=True)
        if g["masked"]:
            e.setTransparency(TransparencyAttrib.MBinary)
    return root

# ---------------------------------------------------------------- dünya
class World:
    """Harita: görünür parçalar (Ursina Mesh) + kendi hızlı çarpışma ızgarası (physics.TriMesh)."""

    def __init__(self):
        self.root = None
        self.mesh = None
        self.spawns = {"ct": [], "t": [], "any": []}
        self.bounds = ((-50, -50, -50), (50, 50, 50))
        self.missing = []
        self._tex = {}

    def _texture(self, img):
        t = self._tex.get(id(img))
        if t is None:
            t = Texture(img)
            try:
                t.filtering = "mipmap"
            except Exception:
                pass
            self._tex[id(img)] = t
        return t

    def build_steps(self, path):
        """Jeneratör: her yield bir durum mesajı; ağır işler yield'ler arasında yapılır."""
        yield "Harita ve dokular okunuyor..."
        wads = gs.WadSet(INDEX.ext(".wad"))
        res = gs.load_bsp(path, wads, SCALE)
        yield "Çarpışma ızgarası kuruluyor..."
        self.mesh = physics.TriMesh(res["coll"], cell=4.0)
        physics.set_world(self.mesh, res["ladders"])
        groups = res["groups"]
        n = len(groups)
        self.root = Entity(add_to_scene_entities=False)
        pal = [color.Color(v, v, v, 1) for v in
               [0.35 + 0.65 * i / (res["levels"] - 1) for i in range(res["levels"])]]
        for i, g in enumerate(groups):
            if i % 40 == 0:
                yield f"Harita çiziliyor... {i}/{n}"
            cols = [pal[k] for k in g["levels"]]
            e = Entity(parent=self.root, model=Mesh(vertices=g["verts"], uvs=g["uvs"], colors=cols, mode="triangle"),
                       texture=self._texture(g["img"]), double_sided=True, add_to_scene_entities=False)
            if g["masked"]:
                e.setTransparency(TransparencyAttrib.MBinary)
            elif g["alpha"] < 1:
                e.setTransparency(TransparencyAttrib.MAlpha)
                e.color = color.Color(1, 1, 1, g["alpha"])
            g["verts"] = g["uvs"] = g["levels"] = None
        self.spawns = res["spawns"]
        self.bounds = res["bounds"]
        self.missing = res["missing"]
        if not self.spawns["any"]:
            lo, hi = self.bounds
            self.spawns["any"] = [(((lo[0] + hi[0]) / 2, hi[1] + 1, (lo[2] + hi[2]) / 2), 0)]
        self.spawns["ct"] = self.spawns["ct"] or self.spawns["any"]
        self.spawns["t"] = self.spawns["t"] or self.spawns["any"]
        for k in ("ct", "t", "any"):
            self.spawns[k] = [(Vec3(*pos) - Vec3(0, 0.88, 0), yaw) for pos, yaw in self.spawns[k]]
        print(f"[harita] {path.name}: {res['face_count']} yüzey, {n} çizim parçası, "
              f"{self.mesh.count} çarpışma üçgeni, {len(self.spawns['any'])} doğma noktası, "
              f"{len(res['ladders'])} merdiven, eksik doku: {len(self.missing)}")

    def ray(self, origin, direction, dist):
        """Hit(hit, distance, world_point(Vec3), world_normal(Vec3))"""
        if self.mesh is None:
            return physics.NOHIT
        h = self.mesh.raycast((origin[0], origin[1], origin[2]),
                              (direction[0], direction[1], direction[2]), dist)
        if h.hit:
            h.world_point = Vec3(*h.world_point)
            h.world_normal = Vec3(*h.world_normal)
        return h

    def destroy(self):
        if self.root:
            destroy(self.root)
        self.root = None
        self.mesh = None
        self._tex = {}
        physics.set_world(None)

W = World()

# ---------------------------------------------------------------- karakter görünümü
def make_character(team):
    names = CT_MODELS if team == "ct" else T_MODELS
    random.shuffle(names)
    for n in names:
        p = INDEX.find(f"models/player/{n}/{n}.mdl")
        if p:
            groups = get_mdl(p)
            if groups:
                return build_model_entity(groups, None)
    # yedek: kutulardan insan
    col = C(60, 90, 200) if team == "ct" else C(200, 140, 50)
    root = Entity()
    Entity(parent=root, model="cube", scale=(0.55, 0.75, 0.3), y=1.15, color=col)
    Entity(parent=root, model="sphere", scale=0.28, y=1.68, color=C(220, 180, 150))
    Entity(parent=root, model="cube", scale=(0.2, 0.75, 0.22), x=-0.14, y=0.38, color=C(40, 44, 60))
    Entity(parent=root, model="cube", scale=(0.2, 0.75, 0.22), x=0.14, y=0.38, color=C(40, 44, 60))
    Entity(parent=root, model="cube", scale=(0.16, 0.6, 0.16), x=-0.36, y=1.15, color=col)
    Entity(parent=root, model="cube", scale=(0.16, 0.6, 0.16), x=0.36, y=1.15, color=col)
    Entity(parent=root, model="cube", scale=(0.08, 0.1, 0.5), x=0.3, y=1.15, z=0.28, color=C(30, 30, 30))
    return root

# ---------------------------------------------------------------- bot
class Bot:
    def __init__(self, game, team, name):
        self.g = game
        self.team = team
        self.name = name
        self.kills = 0
        self.deaths = 0
        self.alive = False
        self.hp = 100
        self.body = Body(Vec3(0, 0, 0))
        self.yaw = 0.0
        self.ent = make_character(team)
        self.ent.enabled = False
        self.path = []
        self.target = None
        self.target_vis = False
        self.last_seen = None
        self.seen_t = -99
        self.think_t = random.random() * 0.3
        self.next_shot = 0
        self.burst = 0
        self.strafe = random.choice([-1, 1])
        self.strafe_t = 0
        self.stuck_t = 0
        self.last_pos = Vec3(0, 0, 0)
        self.hurt_t = -99
        self.respawn_t = 0
        self.repath_t = 0
        self.stuck_n = 0
        self.skip_t = 0.0
        self.radar_t = -1.0
        self.radar_vis = False
        self.acc = 0.0

    # ---- yardımcılar
    def eye(self):
        return self.body.pos + Vec3(0, 1.6, 0)

    def chest(self):
        return self.body.pos + Vec3(0, 1.2, 0)

    def spawn(self):
        pos, yaw = self.g.pick_spawn(self.team)
        self.body = Body(pos)
        self.body.settle()
        self.yaw = yaw
        self.hp = 100
        self.alive = True
        self.path = []
        self.target = None
        self.last_seen = None
        self.stuck_n = 0
        self.ent.enabled = True
        self.ent.rotation = Vec3(0, 0, 0)
        self.ent.position = self.body.pos
        self.ent.rotation_y = yaw

    def take_damage(self, dmg, attacker, head=False):
        if not self.alive:
            return
        self.hp -= dmg
        self.hurt_t = self.g.now
        if attacker is not None and getattr(attacker, "team", None) != self.team:
            self.last_seen = Vec3(attacker.body.pos) if hasattr(attacker, "body") else Vec3(attacker.player_body.pos)
            self.seen_t = self.g.now
            self.path = []
        if self.hp <= 0:
            self.die(attacker, head)

    def die(self, killer, head):
        self.alive = False
        self.deaths += 1
        self.respawn_t = 3.0
        SND.play(["player/die1.wav", "player/die2.wav", "player/die3.wav"],
                 self.g.vol_at(self.body.pos))
        self.ent.animate("rotation_x", -88, duration=0.45)
        self.g.on_kill(killer, self, head)

    # ---- yapay zekâ
    def enemies(self):
        g = self.g
        out = []
        if g.player_alive and g.player_team != self.team:
            out.append(("p", g.player_body.pos + Vec3(0, 1.2, 0), g))
        for b in g.bots:
            if b.alive and b.team != self.team:
                out.append(("b", b.chest(), b))
        return out

    def think(self):
        g = self.g
        eye = self.eye()
        fwd = Vec3(math.sin(math.radians(self.yaw)), 0, math.cos(math.radians(self.yaw)))
        best = None
        bd = 1e9
        for kind, pos, ref in self.enemies():
            d = pos - eye
            dist = d.length()
            if dist > 80:
                continue
            dn = d.normalized()
            if dist > 6 and flat(dn).normalized().dot(fwd) < 0.35 and g.now - self.hurt_t > 2.0:
                continue
            if W.ray(eye, dn, dist - 0.2).hit:
                continue
            if dist < bd:
                bd, best = dist, (ref, pos)
        if best:
            self.target = best[0]
            self.target_vis = True
            self.last_seen = Vec3(best[1])
            self.seen_t = g.now
        else:
            self.target_vis = False
            if g.now - self.seen_t > 6:
                self.target = None

    def pick_goal(self):
        nav = self.g.nav
        if nav is not None and nav.count > 5:
            start = nav.nearest(self.body.pos)
            if self.last_seen is not None and self.g.now - self.seen_t < 6:
                goal = nav.nearest(self.last_seen)
            else:
                goal = nav.random_node()
            self.path = nav.path(start, goal)
        else:
            self.path = []

    def _skip_ok(self, a, b):
        """a'dan b'ye düz gidilebilir mi (yan duvar yok, yükseklik farkı az)?"""
        if abs(b.y - a.y) > 0.3:
            return False
        d = b - a
        dist = d.length()
        if dist < 0.01:
            return True
        dn = d * (1.0 / dist)
        side = Vec3(dn.z, 0, -dn.x) * 0.3
        for off in (Vec3(0, 0, 0), side, -side):
            if W.ray(a + off + Vec3(0, 0.5, 0), dn, dist + 0.3).hit:
                return False
        return True

    def rescue(self):
        """Sıkışırsa en yakın yol ağı noktasına ışınla (yoksa yeniden doğ)."""
        nav = self.g.nav
        i = nav.nearest(self.body.pos) if (nav is not None and nav.count > 0) else None
        if i is None:
            self.spawn()
        else:
            self.body.teleport(nav.vec(i))
        self.path = []
        self.stuck_n = 0

    def update(self, dt):
        g = self.g
        if not self.alive:
            self.respawn_t -= dt
            if self.respawn_t <= 1.0:
                self.ent.enabled = False
            if self.respawn_t <= 0 and g.running:
                self.spawn()
            return
        # görüntü her karede yumuşak izler, yapay zekâ 30 Hz çalışır
        ep, bp = self.ent.position, self.body.pos
        k = min(1.0, dt * 25.0)
        self.ent.position = Vec3(ep.x + (bp.x - ep.x) * k, ep.y + (bp.y - ep.y) * k, ep.z + (bp.z - ep.z) * k)
        self.ent.rotation_y = self.yaw
        self.acc += dt
        if self.acc < 1 / 30:
            return
        dt = self.acc
        self.acc = 0.0
        self.think_t -= dt
        if self.think_t <= 0:
            self.think_t = 0.18 + random.random() * 0.08
            self.think()
        wish = Vec3(0, 0, 0)
        speed = 5.2
        diff = DIFFS[S["difficulty"]]
        if self.target is not None and self.target_vis and self.last_seen is not None:
            tp = self.last_seen
            d = tp - self.eye()
            want = vec_yaw(flat(d))
            err = ang_diff(want, self.yaw)
            self.yaw += max(-1, min(1, err * dt * 6)) * min(abs(err), 420 * dt * (0.6 + diff[1]))
            # hareket: yakınsa sağa-sola, uzaksa yaklaş
            self.strafe_t -= dt
            if self.strafe_t <= 0:
                self.strafe = random.choice([-1, 1, 0])
                self.strafe_t = 0.5 + random.random() * 0.9
            right = Vec3(math.cos(math.radians(self.yaw)), 0, -math.sin(math.radians(self.yaw)))
            fwd = Vec3(math.sin(math.radians(self.yaw)), 0, math.cos(math.radians(self.yaw)))
            dist = d.length()
            if dist > 22:
                wish = fwd * 0.6 + right * self.strafe * 0.7
            elif dist < 8:
                wish = -fwd * 0.5 + right * self.strafe
            else:
                wish = right * self.strafe
            speed = 3.4
            if abs(err) < 9 and g.now >= self.next_shot:
                self.shoot(d, dist, diff)
        else:
            if not self.path:
                self.repath_t -= dt
                if self.repath_t <= 0:
                    self.pick_goal()
                    self.repath_t = 0.5 if self.path else 1.0
            if self.path:
                # düz gidilebilen ara noktaları atla (zikzak yürümesin)
                self.skip_t -= dt
                if self.skip_t <= 0 and len(self.path) >= 2:
                    self.skip_t = 0.25
                    if self._skip_ok(self.body.pos, self.path[1]):
                        self.path.pop(0)
                tgt = self.path[0]
                d = flat(tgt - self.body.pos)
                if d.length() < 0.8 and abs(tgt.y - self.body.pos.y) < 1.3:
                    self.path.pop(0)
                else:
                    want = vec_yaw(d)
                    err = ang_diff(want, self.yaw)
                    self.yaw += max(-1, min(1, err * dt * 8)) * min(abs(err), 360 * dt)
                    wish = d.normalized() if d.length() > 1e-4 else wish
                    speed = 4.6
        if wish.length() > 1:
            wish = wish.normalized()
        self.body.step(dt, wish, speed, False)
        # ---- takılma kurtarma: zıpla -> yeniden yol bul -> ışınla
        self.stuck_t += dt
        if self.stuck_t > 0.6:
            moved = flat(self.body.pos - self.last_pos).length()
            if wish.length() > 0.1 and moved < 0.2:
                self.stuck_n += 1
                if self.stuck_n == 1:
                    self.body.step(dt, wish, speed, True)
                elif self.stuck_n < 5:
                    self.path = []
                    self.strafe = -self.strafe
                    self.last_seen = None
                else:
                    self.rescue()
            else:
                self.stuck_n = 0
            self.last_pos = Vec3(self.body.pos)
            self.stuck_t = 0
        if self.body.trapped > 40:
            self.rescue()
        if self.body.pos.y < W.bounds[0][1] - 40:
            self.rescue()

    def shoot(self, d, dist, diff):
        g = self.g
        self.burst += 1
        if self.burst >= random.randint(3, 6):
            self.burst = 0
            self.next_shot = g.now + 0.35 + random.random() * 0.5
        else:
            self.next_shot = g.now + 0.11
        SND.play(["weapons/ak47-1.wav", "weapons/m4a1_unsil-1.wav"], g.vol_at(self.body.pos) * 0.9)
        # tracer
        if (self.eye() - camera.world_position).length() < 70:
            g.tracer(self.eye() + Vec3(0, -0.15, 0), self.eye() + d.normalized() * min(dist, 60))
        p = diff[1] * (1.0 - min(dist, 60) / 90.0) * (0.75 if self.body.hv.length() > 2 else 1.0)
        if random.random() > max(0.06, p):
            return
        head = random.random() < 0.07
        dmg = diff[2] * (3.0 if head else 1.0)
        t = self.target
        if t is g:
            g.damage_player(dmg, self.body.pos, self)
        elif isinstance(t, Bot):
            t.take_damage(dmg, self, head)

# ---------------------------------------------------------------- oyun
class Game:
    def __init__(self):
        self.running = False
        self.paused = False
        self.now = 0.0
        self.bots = []
        self.nav = None
        self.player_team = S["team"]
        self.player_alive = False
        self.player_body = Body(Vec3(0, 0, 0))
        self.hp = 100
        self.armor = 0
        self.money = 800
        self.kills = 0
        self.deaths = 0
        self.yaw = 0.0
        self.pitch = 0.0
        self.recoil = 0.0
        self.crouch_t = 0.0
        self.eye_h = 1.62
        self.score = {"ct": 0, "t": 0}
        self.kill_limit = S["kill_limit"]
        self.owned = ["knife", "deagle"]
        self.cur = "deagle"
        self.last = "knife"
        self.ammo = {}
        self.next_fire = 0.0
        self.reload_end = 0.0
        self.scoped = False
        self.respawn_t = 0.0
        self.bob = 0.0
        self.step_t = 0.0
        self.decals = deque()
        self.vms = {}
        self.vm_kick = 0.0
        self.vm_draw = 0.0
        self.feed = []
        self.hud = None
        self.round_time = 0.0
        self.ended = False

    # ---- başlat / bitir
    def start_steps(self, mapfile):
        """Jeneratör: Menu her karede bir adım ilerletir, yükleme ekranı canlı kalır."""
        self.__init__()
        self.player_team = S["team"]
        W.destroy()
        for msg in W.build_steps(mapfile):
            yield msg
        yield "Silah modelleri yükleniyor..."
        camera.fov = S["fov"]
        camera.clip_plane_near = 0.03
        camera.clip_plane_far = 500
        try:
            scene.fog_color = C(150, 165, 185)
            scene.fog_density = (40, 220)
        except Exception:
            pass
        window.color = C(130, 150, 175)
        for w in WEAPONS:
            self.ammo[w] = [WEAPONS[w]["mag"], WEAPONS[w]["reserve"]]
        self.make_viewmodels()
        n = S["bots"]
        names = random.sample(NAMES, min(n, len(NAMES)))
        for i in range(n):
            yield f"Botlar hazırlanıyor... {i + 1}/{n}"
            team = "t" if self.player_team == "ct" else "ct"
            if i % 2 == 1:
                team = self.player_team
            self.bots.append(Bot(self, team, names[i % len(names)]))
        yield "Arayüz kuruluyor..."
        self.build_hud()
        self.spawn_player()
        for b in self.bots:
            b.spawn()
        seeds = [p for p, _ in W.spawns["any"]] + [self.player_body.pos + Vec3(0, 0.1, 0)]
        self.nav = Nav(W.mesh, seeds)
        while not self.nav.work(0.04):
            yield f"Bot yol ağı kuruluyor... {self.nav.count} nokta"
        print(f"[yol ağı] {self.nav.count} nokta, {self.nav.t_spent:.1f} sn")
        mouse.locked = True
        SND.stop_music()
        self.running = True

    def stop(self):
        self.running = False
        mouse.locked = False
        for b in self.bots:
            destroy(b.ent)
        self.bots = []
        for vm in self.vms.values():
            destroy(vm)
        self.vms = {}
        if self.hud:
            destroy(self.hud["root"])
            self.hud = None
        for d in self.decals:
            destroy(d)
        self.decals.clear()
        W.destroy()
        camera.position = (0, 0, 0)
        camera.rotation = (0, 0, 0)
        camera.rotation_z = 0
        camera.fov = 90
        window.color = BG
        try:
            scene.fog_density = (9999, 10000)
        except Exception:
            pass

    # ---- spawn
    def pick_spawn(self, team):
        lst = list(W.spawns[team])
        random.shuffle(lst)
        occupied = [b.body.pos for b in self.bots if b.alive]
        if self.player_alive:
            occupied.append(self.player_body.pos)
        for pos, yaw in lst:
            if all((pos - o).length() > 2.5 for o in occupied):
                return pos, yaw
        return lst[0]

    def spawn_player(self):
        pos, yaw = self.pick_spawn(self.player_team)
        self.player_body = Body(pos)
        self.player_body.settle()
        self.yaw = yaw
        self.pitch = 0
        self.hp = 100
        self.armor = 0 if self.deaths else 0
        self.player_alive = True
        self.scoped = False
        self.reload_end = 0
        if self.cur not in self.owned:
            self.cur = "deagle"
        self.select(self.cur, silent=True)
        camera.position = self.player_body.pos + Vec3(0, self.eye_h, 0)

    # ---- viewmodel
    def make_viewmodels(self):
        for key, w in WEAPONS.items():
            vm = None
            p = INDEX.find(f"models/{w['mdl']}.mdl")
            if p:
                groups = get_mdl(p)
                if groups:
                    vm = build_model_entity(groups, camera, scale=VM_SCALE)
            if vm is None:
                vm = Entity(parent=camera)
                body_col = C(40, 42, 48)
                Entity(parent=vm, model="cube", scale=(0.07, 0.1, 0.55), position=(0.22, -0.22, 0.5), color=body_col)
                Entity(parent=vm, model="cube", scale=(0.05, 0.14, 0.07), position=(0.22, -0.3, 0.38), color=body_col)
                Entity(parent=vm, model="cube", scale=(0.09, 0.09, 0.09), position=(0.2, -0.33, 0.28),
                       color=C(210, 170, 140))
            vm.enabled = False
            self.vms[key] = vm
        self.vm_base = {k: Vec3(v.position) for k, v in self.vms.items()}

    def select(self, key, silent=False):
        if key not in self.owned:
            return
        if key != self.cur:
            self.last = self.cur
        self.vms[self.cur].enabled = False
        self.cur = key
        vm = self.vms[key]
        vm.enabled = True
        self.vm_draw = 1.0
        self.reload_end = 0
        self.scoped = False
        camera.fov = S["fov"]
        if not silent:
            SND.play("weapons/de_clipin.wav", 0.5)
        self.refresh_hud()

    # ---- hud
    def build_hud(self):
        root = Entity(parent=camera.ui)
        h = {"root": root}
        # crosshair
        ch = Entity(parent=root)
        h["ch"] = [Entity(parent=ch, model="quad", color=GREEN, scale=(0.012, 0.0015)) for _ in range(2)] + \
                  [Entity(parent=ch, model="quad", color=GREEN, scale=(0.0015, 0.012)) for _ in range(2)]
        h["ch_root"] = ch
        h["hit"] = Text("X", parent=root, origin=(0, 0), scale=2.2, color=C(255, 60, 60, 0))
        # alt sol: can/zırh
        panel = Entity(parent=root, model="quad", color=C(0, 0, 0, 120), scale=(0.5, 0.07), position=(-0.62, -0.44))
        h["hp"] = Text("", parent=root, position=(-0.86, -0.415), scale=2.1, color=AMBER)
        h["ar"] = Text("", parent=root, position=(-0.60, -0.415), scale=2.1, color=AMBER)
        # alt sağ: mermi
        Entity(parent=root, model="quad", color=C(0, 0, 0, 120), scale=(0.34, 0.07), position=(0.62, -0.44))
        h["ammo"] = Text("", parent=root, origin=(0.5, 0), position=(0.78, -0.44), scale=2.4, color=AMBER)
        h["wname"] = Text("", parent=root, origin=(0.5, 0), position=(0.78, -0.395), scale=1.0, color=GREY)
        h["money"] = Text("", parent=root, origin=(0.5, 0), position=(0.84, -0.36), scale=1.5, color=GREEN)
        # üst orta: skor
        Entity(parent=root, model="quad", color=C(0, 0, 0, 130), scale=(0.44, 0.07), position=(0, 0.455))
        h["sct"] = Text("0", parent=root, origin=(0, 0), position=(-0.1, 0.455), scale=2.4, color=BLUE)
        h["st"] = Text("0", parent=root, origin=(0, 0), position=(0.1, 0.455), scale=2.4, color=AMBER)
        h["stime"] = Text("", parent=root, origin=(0, 0), position=(0, 0.455), scale=1.2, color=WHITE)
        # radar
        Entity(parent=root, model="circle", color=C(0, 0, 0, 120), scale=0.22, position=(-0.72, 0.34))
        h["radar"] = Entity(parent=root, position=(-0.72, 0.34))
        h["dots"] = [Entity(parent=h["radar"], model="circle", scale=0.008, color=BLUE, enabled=False)
                     for _ in range(24)]
        Entity(parent=h["radar"], model="circle", scale=0.01, color=WHITE)
        # kill feed
        h["feed"] = Text("", parent=root, origin=(0.5, 0.5), position=(0.88, 0.46), scale=1.0, color=WHITE)
        # mesaj
        h["msg"] = Text("", parent=root, origin=(0, 0), position=(0, 0.2), scale=2.0, color=WHITE)
        # hasar yönü
        h["dmg"] = Entity(parent=root, model="quad", scale=(2, 1), color=C(200, 0, 0, 0), z=1)
        # dürbün
        sc = Entity(parent=root, enabled=False, z=0.5)
        Entity(parent=sc, model="quad", color=C(0, 0, 0), scale=(2, 0.4), y=0.5)
        Entity(parent=sc, model="quad", color=C(0, 0, 0), scale=(2, 0.4), y=-0.5)
        Entity(parent=sc, model="quad", color=C(0, 0, 0), scale=(0.7, 1), x=-0.78)
        Entity(parent=sc, model="quad", color=C(0, 0, 0), scale=(0.7, 1), x=0.78)
        Entity(parent=sc, model="quad", color=C(0, 0, 0), scale=(0.002, 0.6))
        Entity(parent=sc, model="quad", color=C(0, 0, 0), scale=(0.6, 0.002))
        h["scope"] = sc
        # skor tablosu
        sb = Entity(parent=root, enabled=False, z=-0.2)
        Entity(parent=sb, model="quad", color=C(0, 0, 0, 200), scale=(1.1, 0.8))
        h["sb_text"] = Text("", parent=sb, origin=(-0.5, 0.5), position=(-0.5, 0.35), scale=1.2, color=WHITE)
        h["sb"] = sb
        self.hud = h
        self.buy_ui = None
        self.build_buy()
        self.build_pause()
        self.refresh_hud()

    def build_buy(self):
        r = Entity(parent=camera.ui, enabled=False, z=-0.3)
        Entity(parent=r, model="quad", color=PANEL, scale=(0.62, 0.62))
        Text("SİLAH MARKETİ", parent=r, origin=(0, 0), position=(0, 0.26), scale=2, color=AMBER)
        self.buy_btns = []
        y = 0.17
        for i, k in enumerate(BUY_ORDER):
            w = WEAPONS[k]
            b = Button(parent=r, text=f"{i + 1}.  {w['name']}", scale=(0.5, 0.05), position=(0, y),
                       color=PANEL2, highlight_color=C(70, 80, 100), text_color=WHITE)
            b.on_click = (lambda kk=k: self.buy(kk))
            Text(f"${w['price']}", parent=r, origin=(0.5, 0), position=(0.23, y, -0.05), scale=1.0, color=GREEN)
            y -= 0.062
        b = Button(parent=r, text="6.  Kevlar + Kask", scale=(0.5, 0.05), position=(0, y), color=PANEL2,
                   highlight_color=C(70, 80, 100), text_color=WHITE)
        b.on_click = lambda: self.buy("armor")
        Text("$650", parent=r, origin=(0.5, 0), position=(0.23, y, -0.05), scale=1.0, color=GREEN)
        y -= 0.062
        b = Button(parent=r, text="7.  Mermi doldur", scale=(0.5, 0.05), position=(0, y), color=PANEL2,
                   highlight_color=C(70, 80, 100), text_color=WHITE)
        b.on_click = lambda: self.buy("ammo")
        Text("$200", parent=r, origin=(0.5, 0), position=(0.23, y, -0.05), scale=1.0, color=GREEN)
        Text("B / ESC: kapat", parent=r, origin=(0, 0), position=(0, -0.28), scale=0.9, color=GREY)
        self.buy_ui = r

    def build_pause(self):
        r = Entity(parent=camera.ui, enabled=False, z=-0.3)
        Entity(parent=r, model="quad", color=C(0, 0, 0, 180), scale=(2, 1))
        Entity(parent=r, model="quad", color=PANEL, scale=(0.5, 0.42))
        Text("DURAKLATILDI", parent=r, origin=(0, 0), position=(0, 0.15), scale=2, color=AMBER)
        Button(parent=r, text="Devam", scale=(0.4, 0.06), position=(0, 0.06), color=PANEL2,
               highlight_color=C(70, 80, 100), on_click=self.toggle_pause)
        Button(parent=r, text="Ana Menü", scale=(0.4, 0.06), position=(0, -0.02), color=PANEL2,
               highlight_color=C(70, 80, 100), on_click=lambda: goto_menu())
        Button(parent=r, text="Çıkış", scale=(0.4, 0.06), position=(0, -0.10), color=PANEL2,
               highlight_color=C(120, 40, 40), on_click=application.quit)
        self.pause_ui = r

    def refresh_hud(self):
        h = self.hud
        if not h:
            return
        set_text(h["hp"], f"HP {max(0, int(self.hp))}")
        set_text(h["ar"], f"AR {int(self.armor)}")
        w = WEAPONS[self.cur]
        set_text(h["wname"], w["name"])
        if w.get("melee"):
            set_text(h["ammo"], "")
        else:
            m, r = self.ammo[self.cur]
            set_text(h["ammo"], f"{m} / {r}")
        set_text(h["money"], f"${self.money}")
        set_text(h["sct"], str(self.score["ct"]))
        set_text(h["st"], str(self.score["t"]))

    def msg(self, text, dur=2.0, col=WHITE):
        h = self.hud
        if not h:
            return
        h["msg"].text = text
        h["msg"].color = col
        h["msg"].animate_color(C(255, 255, 255, 0), duration=0.3, delay=dur)

    def add_feed(self, a, b, head):
        self.feed.append((self.now + 6, f"{a}  {'[HS] öldürdü' if head else 'öldürdü'}  {b}"))
        self.feed = self.feed[-5:]

    # ---- satın alma
    def buy(self, key):
        if not self.player_alive:
            return
        if key == "armor":
            price = 650
            if self.money >= price and self.armor < 100:
                self.money -= price
                self.armor = 100
                SND.play("items/kevlar.wav")
        elif key == "ammo":
            if self.money >= 200 and not WEAPONS[self.cur].get("melee"):
                self.money -= 200
                self.ammo[self.cur][1] = WEAPONS[self.cur]["reserve"]
                SND.play("items/9mmclip1.wav")
        else:
            w = WEAPONS[key]
            if key in self.owned:
                self.msg("Zaten sende var", 1.0, AMBER)
            elif self.money >= w["price"]:
                self.money -= w["price"]
                # aynı slot varsa değiştir
                self.owned = [o for o in self.owned if WEAPONS[o]["slot"] != w["slot"]]
                self.owned.append(key)
                self.ammo[key] = [w["mag"], w["reserve"]]
                self.select(key)
            else:
                self.msg("Yeterli paran yok", 1.2, RED)
        self.refresh_hud()

    def toggle_buy(self):
        if not self.running or self.paused or not self.player_alive:
            return
        on = not self.buy_ui.enabled
        self.buy_ui.enabled = on
        mouse.locked = not on

    def toggle_pause(self):
        if not self.running:
            return
        self.paused = not self.paused
        self.pause_ui.enabled = self.paused
        self.buy_ui.enabled = False
        mouse.locked = not self.paused

    # ---- ateş
    def reload(self):
        w = WEAPONS[self.cur]
        if w.get("melee") or self.reload_end > self.now:
            return
        m, r = self.ammo[self.cur]
        if m >= w["mag"] or r <= 0:
            return
        self.reload_end = self.now + w.get("rtime", 2.2)
        self.scoped = False
        camera.fov = S["fov"]
        SND.play(w.get("reload", ["weapons/de_clipout.wav"]), 0.8)

    def finish_reload(self):
        w = WEAPONS[self.cur]
        m, r = self.ammo[self.cur]
        take = min(w["mag"] - m, r)
        self.ammo[self.cur] = [m + take, r - take]
        self.reload_end = 0
        self.refresh_hud()

    def fire(self):
        w = WEAPONS[self.cur]
        if self.now < self.next_fire or self.reload_end > self.now or not self.player_alive:
            return
        if w.get("melee"):
            self.next_fire = self.now + w["rof"]
            self.vm_kick = 1.0
            SND.play(w["fire"], 0.8)
            o = camera.world_position
            d = camera.forward
            for b in self.bots:
                if b.alive and b.team != self.player_team:
                    v = b.chest() - o
                    if v.length() < 2.4 and v.normalized().dot(d) > 0.7:
                        b.take_damage(w["dmg"], self)
                        SND.play("weapons/knife_hit1.wav", 0.8)
                        self.hit_marker(False)
            return
        m, r = self.ammo[self.cur]
        if m <= 0:
            self.next_fire = self.now + 0.2
            SND.play("weapons/dryfire_pistol.wav", 0.6)
            self.reload()
            return
        self.ammo[self.cur][0] -= 1
        self.next_fire = self.now + w["rof"]
        SND.play(w["fire"], 1.0)
        self.vm_kick = 1.0
        self.recoil += w["kick"]
        self.refresh_hud()
        # sapma
        moving = flat(self.player_body.hv).length()
        spread = w["spread"] + moving * 0.0035 + (0.02 if not self.player_body.grounded else 0) + self.recoil * 0.0015
        if self.scoped:
            spread *= 0.05
        elif w.get("scope"):
            spread *= 4
        if self.crouch_t > 0.5:
            spread *= 0.7
        o = camera.world_position
        d = camera.forward + camera.right * random.uniform(-spread, spread) + camera.up * random.uniform(-spread, spread)
        d = d.normalized()
        r = W.ray(o, d, 300)
        wall = r.distance if r.hit else 300
        best, bt, bpart = None, wall, None
        for b in self.bots:
            if not b.alive or b.team == self.player_team:
                continue
            p = b.body.pos
            for part, off, rad, mult in (("head", 1.66, 0.2, 4.0), ("body", 1.2, 0.34, 1.0),
                                         ("belly", 0.8, 0.34, 1.0), ("legs", 0.35, 0.3, 0.75)):
                t = ray_sphere(o, d, p + Vec3(0, off, 0), rad)
                if t is not None and t < bt:
                    best, bt, bpart = b, t, (part, mult)
        if best:
            dmg = w["dmg"] * bpart[1]
            head = bpart[0] == "head"
            SND.play(["player/headshot1.wav", "player/headshot2.wav"] if head else
                     ["player/bhit_flesh-1.wav", "player/bhit_flesh-2.wav"], 0.9)
            self.hit_marker(head)
            best.take_damage(dmg, self, head)
        elif r.hit:
            self.add_decal(r.world_point, r.world_normal)
        self.tracer(camera.world_position + camera.forward * 0.6 + camera.right * 0.18 - camera.up * 0.15,
                    o + d * min(bt, 120), 0.04)
        if w.get("scope") and self.scoped:
            self.scope(False)

    def tracer(self, a, b, life=0.05):
        e = Entity(model=Mesh(vertices=[a, b], mode="line", thickness=2), color=C(255, 230, 150))
        destroy(e, delay=life)

    def add_decal(self, p, n):
        e = Entity(model="quad", position=p + n * 0.01, scale=0.09, color=C(15, 15, 15), double_sided=True)
        e.look_at(p - n)
        self.decals.append(e)
        if len(self.decals) > 70:
            destroy(self.decals.popleft())

    def hit_marker(self, head):
        h = self.hud["hit"]
        h.color = C(255, 70, 70) if head else C(255, 255, 255)
        h.animate_color(C(255, 255, 255, 0), duration=0.25)

    def scope(self, on):
        w = WEAPONS[self.cur]
        if not w.get("scope"):
            return
        self.scoped = on
        camera.fov = 22 if on else S["fov"]
        self.hud["scope"].enabled = on
        self.vms[self.cur].enabled = not on
        self.hud["ch_root"].enabled = not on

    # ---- hasar / ölüm
    def damage_player(self, dmg, src_pos, attacker):
        if not self.player_alive:
            return
        if self.armor > 0:
            absorbed = min(self.armor, dmg * 0.5)
            self.armor -= absorbed
            dmg -= absorbed * 0.5
        self.hp -= dmg
        SND.play(["player/bhit_flesh-1.wav", "player/bhit_flesh-3.wav"], 0.9)
        self.hud["dmg"].color = C(200, 0, 0, 120)
        self.hud["dmg"].animate_color(C(200, 0, 0, 0), duration=0.5)
        self.pitch += random.uniform(-1.5, 1.5)
        self.refresh_hud()
        if self.hp <= 0:
            self.hp = 0
            self.player_alive = False
            self.deaths += 1
            self.respawn_t = 3.0
            self.scope(False)
            self.buy_ui.enabled = False
            mouse.locked = True
            SND.play("player/die1.wav", 1.0)
            self.msg("ÖLDÜN - yeniden doğuyorsun...", 3.0, RED)
            self.on_kill(attacker, self, False)

    def on_kill(self, killer, victim, head):
        def nm(x):
            return "Sen" if x is self else getattr(x, "name", "?")
        def team_of(x):
            return self.player_team if x is self else x.team
        if killer is None:
            return
        self.add_feed(nm(killer), nm(victim), head)
        kt = team_of(killer)
        if kt != team_of(victim):
            self.score[kt] += 1
        if killer is self:
            self.kills += 1
            self.money = min(16000, self.money + 300)
        elif isinstance(killer, Bot):
            killer.kills += 1
        self.refresh_hud()
        if self.score[kt] >= self.kill_limit and not self.ended:
            self.end_match(kt)

    def end_match(self, winner):
        self.ended = True
        txt = "KAZANDIN!" if winner == self.player_team else "KAYBETTİN"
        self.msg(f"{txt}  ({self.score['ct']} - {self.score['t']})", 6.0,
                 GREEN if winner == self.player_team else RED)
        invoke(goto_menu, delay=6.0)

    def vol_at(self, pos):
        d = (pos - camera.world_position).length()
        return max(0.05, 1.0 / (1.0 + d / 12.0))

    # ---- güncelle
    def update(self, dt):
        if not self.running or self.paused:
            return
        self.now += dt
        self.round_time += dt
        W_ = self.player_body
        h = self.hud
        # ----- oyuncu
        if self.player_alive:
            if not self.buy_ui.enabled:
                sens = S["sens"] * (0.25 if self.scoped else 1.0)
                self.yaw += mouse.velocity[0] * sens
                self.pitch -= mouse.velocity[1] * sens
                self.pitch = max(-89, min(89, self.pitch))
            fwd = Vec3(math.sin(math.radians(self.yaw)), 0, math.cos(math.radians(self.yaw)))
            right = Vec3(fwd.z, 0, -fwd.x)
            wish = Vec3(0, 0, 0)
            if not self.buy_ui.enabled:
                wish = fwd * (held_keys["w"] - held_keys["s"]) + right * (held_keys["d"] - held_keys["a"])
            if wish.length() > 1:
                wish = wish.normalized()
            crouch = held_keys["left control"] or held_keys["c"]
            walk = held_keys["left shift"]
            sp = 6.2 * WEAPONS[self.cur]["speed"]
            if crouch:
                sp *= 0.45
            elif walk:
                sp *= 0.5
            if self.scoped:
                sp *= 0.5
            W_.set_crouch(bool(crouch))
            self.crouch_t += ((1 if W_.height < 1.3 else 0) - self.crouch_t) * min(1, dt * 12)
            climb = 0.0 if self.buy_ui.enabled else float(held_keys["w"] - held_keys["s"])
            W_.step(dt, wish, sp, bool(held_keys["space"]) and not self.buy_ui.enabled, climb)
            if W_.trapped > 45 or W_.pos.y < W.bounds[0][1] - 40:
                pos, yaw = self.pick_spawn(self.player_team)
                W_.teleport(pos)
                self.yaw = yaw
            # ateş
            if mouse.locked and held_keys["left mouse"] and (WEAPONS[self.cur]["auto"] or False):
                self.fire()
            # reload bitir
            if self.reload_end and self.now >= self.reload_end:
                self.finish_reload()
            # geri tepme toparla
            self.recoil = max(0.0, self.recoil - dt * 6)
            # kamera
            eye = 1.62 - 0.75 * self.crouch_t
            self.eye_h += (eye - self.eye_h) * min(1, dt * 14)
            speed_n = flat(W_.hv).length()
            if W_.grounded and speed_n > 0.5:
                self.bob += dt * speed_n * 1.9
                self.step_t -= dt * speed_n
                if self.step_t <= 0 and not crouch and not walk:
                    SND.play(["player/pl_step1.wav", "player/pl_step2.wav", "player/pl_step3.wav",
                              "player/pl_step4.wav"], 0.35)
                    self.step_t = 4.2
            bob_y = math.sin(self.bob) * 0.025 * min(1, speed_n / 5)
            camera.position = W_.pos + Vec3(0, self.eye_h + bob_y, 0)
            camera.rotation = Vec3(self.pitch - self.recoil * 0.8, self.yaw, 0)
        else:
            self.respawn_t -= dt
            camera.rotation_z = lerp(camera.rotation_z, 25, dt * 3)
            if self.respawn_t <= 0 and not self.ended:
                camera.rotation_z = 0
                self.spawn_player()
                self.refresh_hud()
        # viewmodel animasyonu
        vm = self.vms.get(self.cur)
        if vm and vm.enabled:
            self.vm_kick = max(0.0, self.vm_kick - dt * 9)
            self.vm_draw = max(0.0, self.vm_draw - dt * 3.5)
            base = self.vm_base[self.cur] + VM_OFFSET
            sway = math.sin(self.bob) * 0.012
            rel = 0.0
            if self.reload_end > self.now:
                tt = 1 - (self.reload_end - self.now) / WEAPONS[self.cur].get("rtime", 2.2)
                rel = math.sin(min(1, tt) * math.pi)
            vm.position = base + Vec3(sway * 0.5, -abs(sway) - self.vm_draw * 0.45 - rel * 0.25,
                                      -self.vm_kick * 0.05)
            vm.rotation = Vec3(-self.vm_kick * 2.0 + rel * 35, 0, rel * 10)
        # bots
        for b in self.bots:
            b.update(dt)
        # HUD
        self.update_hud(dt)

    def update_hud(self, dt):
        h = self.hud
        if not h:
            return
        spread_px = 0.008 + flat(self.player_body.hv).length() * 0.0016 + self.recoil * 0.004 + \
                    (0.012 if not self.player_body.grounded else 0)
        for i, e in enumerate(h["ch"]):
            if i == 0:
                e.position = (-spread_px - 0.006, 0)
            elif i == 1:
                e.position = (spread_px + 0.006, 0)
            elif i == 2:
                e.position = (0, spread_px + 0.006)
            else:
                e.position = (0, -spread_px - 0.006)
        # feed
        self.feed = [f for f in self.feed if f[0] > self.now]
        set_text(h["feed"], "\n".join(f[1] for f in self.feed))
        # radar
        yawr = math.radians(self.yaw)
        pp = self.player_body.pos
        k = 0
        for b in self.bots:
            if k >= len(h["dots"]):
                break
            e = h["dots"][k]
            show = b.alive and (b.team == self.player_team or self.enemy_seen(b))
            if show:
                d = b.body.pos - pp
                x = d.x * math.cos(yawr) - d.z * math.sin(yawr)
                z = d.x * math.sin(yawr) + d.z * math.cos(yawr)
                sx, sy = x / 60.0 * 0.11, z / 60.0 * 0.11
                m = math.hypot(sx, sy)
                if m > 0.1:
                    sx, sy = sx / m * 0.1, sy / m * 0.1
                e.position = (sx, sy)
                e.color = BLUE if b.team == self.player_team else RED
                e.enabled = True
            else:
                e.enabled = False
            k += 1
        for e in h["dots"][k:]:
            e.enabled = False
        # skor tablosu
        sb = h["sb"]
        sb.enabled = bool(held_keys["tab"])
        if sb.enabled:
            rows = [("Sen", self.player_team, self.kills, self.deaths)] + \
                   [(b.name, b.team, b.kills, b.deaths) for b in self.bots]
            lines = [f"CT {self.score['ct']}   -   T {self.score['t']}        (limit {self.kill_limit})", ""]
            lines.append(f"{'Oyuncu':<18}{'Takım':<8}{'Öldürme':<10}{'Ölüm':<6}")
            for n, tm, kk, dd in sorted(rows, key=lambda r: -r[2]):
                lines.append(f"{n:<18}{tm.upper():<8}{kk:<10}{dd:<6}")
            set_text(h["sb_text"], "\n".join(lines))

    def enemy_seen(self, b):
        # düşman botu oyuncu görüyorsa radarda göster
        if b.team == self.player_team or not self.player_alive:
            return False
        if self.now - b.radar_t < 0.2:
            return b.radar_vis
        b.radar_t = self.now + random.random() * 0.05
        v = b.chest() - camera.world_position
        dist = v.length()
        b.radar_vis = dist <= 60 and not W.ray(camera.world_position, v.normalized(), dist - 0.3).hit
        return b.radar_vis

    # ---- girdi
    def input(self, key):
        if not self.running:
            return
        if key == "escape":
            if self.buy_ui.enabled:
                self.toggle_buy()
            else:
                self.toggle_pause()
            return
        if self.paused:
            return
        if key == "b":
            self.toggle_buy()
        if not self.player_alive or self.buy_ui.enabled:
            if self.buy_ui.enabled and key in "1234567" and len(key) == 1:
                i = int(key) - 1
                if i < len(BUY_ORDER):
                    self.buy(BUY_ORDER[i])
                elif i == 5:
                    self.buy("armor")
                else:
                    self.buy("ammo")
            return
        if key == "left mouse down":
            if not WEAPONS[self.cur]["auto"]:
                self.fire()
        elif key == "right mouse down":
            if WEAPONS[self.cur].get("scope"):
                self.scope(not self.scoped)
        elif key == "r":
            self.reload()
        elif key == "q":
            self.select(self.last)
        elif key in ("1", "2", "3"):
            slot = int(key)
            for o in self.owned:
                if WEAPONS[o]["slot"] == slot:
                    self.select(o)
        elif key in ("scroll up", "scroll down"):
            i = self.owned.index(self.cur)
            i = (i + (1 if key == "scroll up" else -1)) % len(self.owned)
            self.select(self.owned[i])

G = Game()

# ================================================================ MENÜ
class Menu:
    def __init__(self):
        self.root = Entity(parent=camera.ui)
        self.page = None
        self.sel_map = 0
        self.map_page = 0
        self.pages = {}
        self.build_background()
        self.build_main()
        self.build_maps()
        self.build_settings()
        self.build_about()
        self.loader = None
        self.load_name = ""
        self.loading = self.build_loading()
        self.show("main")

    # ---- yardımcılar
    def mkbtn(self, parent, text, y, cb, w=0.42, h=0.062, col=None):
        b = Button(parent=parent, text=text, scale=(w, h), position=(0, y), color=col or PANEL2,
                   highlight_color=C(78, 90, 118), pressed_color=AMBER, text_color=WHITE, on_click=cb)
        return b

    def build_background(self):
        bg = Entity(parent=self.root, model="quad", scale=(2.2, 1.2), color=BG, z=2)
        Entity(parent=self.root, model="quad", scale=(2.2, 0.5), y=-0.45, color=C(18, 22, 32), z=1.9)
        # kayan çizgiler
        self.stripes = []
        for i in range(7):
            e = Entity(parent=self.root, model="quad", scale=(0.9, 0.004), color=C(255, 176, 0, 40),
                       position=(-0.9 + i * 0.3, -0.6 + i * 0.2), rotation_z=18, z=1.8)
            self.stripes.append(e)
        Entity(parent=self.root, model="quad", scale=(0.008, 1.2), x=-0.23, color=C(214, 48, 48, 200), z=1.7)

    def build_main(self):
        p = Entity(parent=self.root)
        Text("ŞIRNAK", parent=p, origin=(0, 0), position=(-0.45, 0.30), scale=5.5, color=WHITE)
        Text("STRIKE", parent=p, origin=(0, 0), position=(-0.45, 0.17), scale=5.5, color=RED)
        Text("Python FPS  -  CS 1.6 tarzı", parent=p, origin=(0, 0), position=(-0.45, 0.08), scale=1.1, color=GREY)
        self.status = Text("", parent=p, origin=(-0.5, 0.5), position=(-0.82, -0.2), scale=1.0, color=GREY)
        box = Entity(parent=p, position=(0.30, 0))
        self.mkbtn(box, "OYUNA BAŞLA", 0.15, lambda: self.start(), col=C(180, 120, 0))
        self.mkbtn(box, "HARİTA SEÇ", 0.07, lambda: self.show("maps"))
        self.mkbtn(box, "AYARLAR", -0.01, lambda: self.show("settings"))
        self.mkbtn(box, "HAKKINDA / DOSYALAR", -0.09, lambda: self.show("about"))
        self.mkbtn(box, "ÇIKIŞ", -0.17, application.quit, col=C(120, 36, 36))
        self.pages["main"] = p
        self.refresh_status()

    def refresh_status(self):
        mp = MAPS[self.sel_map].stem if MAPS else "YOK"
        lines = [f"Seçili harita : {mp}",
                 f"Bulunan: {len(MAPS)} harita | {N_MDL} model | {N_WAV} ses | {N_WAD} wad"]
        if not MAPS:
            lines.append("!! 'maps' klasöründe .bsp bulunamadı")
        self.status.text = "\n".join(lines)
        self.status.color = AMBER if MAPS else RED

    def build_maps(self):
        p = Entity(parent=self.root, enabled=False)
        Entity(parent=p, model="quad", color=PANEL, scale=(0.9, 0.78))
        Text("HARİTA SEÇ", parent=p, origin=(0, 0), position=(0, 0.33), scale=2.2, color=AMBER)
        self.map_btns = []
        for i in range(8):
            b = Button(parent=p, text="", scale=(0.78, 0.05), position=(0, 0.23 - i * 0.058), color=PANEL2,
                       highlight_color=C(78, 90, 118), text_color=WHITE)
            b.on_click = (lambda ii=i: self.pick_map(ii))
            self.map_btns.append(b)
        self.map_info = Text("", parent=p, origin=(0, 0), position=(0, -0.205), scale=0.9, color=GREY)
        Button(parent=p, text="ÖNCEKİ", scale=(0.14, 0.05), position=(-0.12, -0.26), color=PANEL2,
               on_click=lambda: self.page_maps(-1))
        Button(parent=p, text="SONRAKİ", scale=(0.14, 0.05), position=(0.12, -0.26), color=PANEL2,
               on_click=lambda: self.page_maps(1))
        self.mkbtn(p, "GERİ", -0.31, lambda: self.show("main"), w=0.2, h=0.05).x = -0.33
        self.mkbtn(p, "OYNA", -0.31, lambda: self.start(), w=0.2, h=0.05, col=C(180, 120, 0)).x = 0.33
        self.pages["maps"] = p
        self.fill_maps()

    def fill_maps(self):
        for i, b in enumerate(self.map_btns):
            idx = self.map_page * 8 + i
            if idx < len(MAPS):
                b.enabled = True
                b.text = ("* " if idx == self.sel_map else "   ") + MAPS[idx].stem
                b.color = C(120, 90, 10) if idx == self.sel_map else PANEL2
            else:
                b.enabled = False
        tot = max(1, math.ceil(len(MAPS) / 8))
        self.map_info.text = f"Sayfa {self.map_page + 1}/{tot}   -   {len(MAPS)} harita" if MAPS else \
            "maps klasörüne .bsp harita dosyalarını koy"

    def pick_map(self, i):
        idx = self.map_page * 8 + i
        if idx < len(MAPS):
            self.sel_map = idx
            self.fill_maps()
            self.refresh_status()
            SND.play("buttons/button9.wav", 0.5)

    def page_maps(self, d):
        tot = max(1, math.ceil(len(MAPS) / 8))
        self.map_page = (self.map_page + d) % tot
        self.fill_maps()

    def build_settings(self):
        p = Entity(parent=self.root, enabled=False)
        Entity(parent=p, model="quad", color=PANEL, scale=(0.9, 0.82))
        Text("AYARLAR", parent=p, origin=(0, 0), position=(0, 0.35), scale=2.2, color=AMBER)
        self.set_rows = {}
        rows = [("sens", "Fare hassasiyeti", 5, 100, 5, "{:.0f}"),
                ("fov", "Görüş açısı (FOV)", 60, 120, 5, "{:.0f}"),
                ("volume", "Ses", 0.0, 1.0, 0.1, "{:.1f}"),
                ("bots", "Bot sayısı", 2, 16, 1, "{:.0f}"),
                ("kill_limit", "Öldürme limiti", 10, 100, 5, "{:.0f}")]
        y = 0.25
        for key, label, lo, hi, st, fmt in rows:
            Text(label, parent=p, origin=(-0.5, 0), position=(-0.38, y), scale=1.2, color=WHITE)
            val = Text("", parent=p, origin=(0, 0), position=(0.2, y), scale=1.2, color=AMBER)
            Button(parent=p, text="-", scale=(0.05, 0.045), position=(0.08, y), color=PANEL2,
                   on_click=lambda k=key, l=lo, h=hi, s=st: self.adj(k, -s, l, h))
            Button(parent=p, text="+", scale=(0.05, 0.045), position=(0.32, y), color=PANEL2,
                   on_click=lambda k=key, l=lo, h=hi, s=st: self.adj(k, s, l, h))
            self.set_rows[key] = (val, fmt)
            y -= 0.07
        # zorluk
        Text("Bot zorluğu", parent=p, origin=(-0.5, 0), position=(-0.38, y), scale=1.2, color=WHITE)
        self.diff_txt = Text("", parent=p, origin=(0, 0), position=(0.2, y), scale=1.2, color=AMBER)
        Button(parent=p, text="-", scale=(0.05, 0.045), position=(0.08, y), color=PANEL2,
               on_click=lambda: self.adj_diff(-1))
        Button(parent=p, text="+", scale=(0.05, 0.045), position=(0.32, y), color=PANEL2,
               on_click=lambda: self.adj_diff(1))
        y -= 0.07
        Text("Takım", parent=p, origin=(-0.5, 0), position=(-0.38, y), scale=1.2, color=WHITE)
        self.team_btn = Button(parent=p, text="", scale=(0.22, 0.045), position=(0.2, y), color=PANEL2,
                               on_click=self.toggle_team)
        y -= 0.07
        Text("FPS göster", parent=p, origin=(-0.5, 0), position=(-0.38, y), scale=1.2, color=WHITE)
        self.fps_btn = Button(parent=p, text="", scale=(0.22, 0.045), position=(0.2, y), color=PANEL2,
                              on_click=self.toggle_fps)
        y -= 0.07
        Text("Tam ekran", parent=p, origin=(-0.5, 0), position=(-0.38, y), scale=1.2, color=WHITE)
        self.fs_btn = Button(parent=p, text="", scale=(0.22, 0.045), position=(0.2, y), color=PANEL2,
                             on_click=self.toggle_fs)
        self.mkbtn(p, "GERİ", -0.37, lambda: (save_settings(), self.show("main")), w=0.3, h=0.05)
        self.pages["settings"] = p
        self.refresh_settings()

    def refresh_settings(self):
        for k, (t, fmt) in self.set_rows.items():
            t.text = fmt.format(S[k])
        self.diff_txt.text = DIFFS[S["difficulty"]][0]
        self.team_btn.text = "Anti-Terrorist (CT)" if S["team"] == "ct" else "Terrorist (T)"
        self.fps_btn.text = "Açık" if S["fps"] else "Kapalı"
        self.fs_btn.text = "Açık" if S["fullscreen"] else "Kapalı"

    def adj(self, k, d, lo, hi):
        S[k] = round(max(lo, min(hi, S[k] + d)), 2)
        if k in ("bots", "kill_limit", "sens", "fov"):
            S[k] = int(S[k])
        self.refresh_settings()

    def adj_diff(self, d):
        S["difficulty"] = max(0, min(len(DIFFS) - 1, S["difficulty"] + d))
        self.refresh_settings()

    def toggle_team(self):
        S["team"] = "t" if S["team"] == "ct" else "ct"
        self.refresh_settings()

    def toggle_fps(self):
        S["fps"] = not S["fps"]
        window.fps_counter.enabled = S["fps"]
        self.refresh_settings()

    def toggle_fs(self):
        S["fullscreen"] = not S["fullscreen"]
        window.fullscreen = bool(S["fullscreen"])
        self.refresh_settings()

    def build_about(self):
        p = Entity(parent=self.root, enabled=False)
        Entity(parent=p, model="quad", color=PANEL, scale=(1.3, 0.85))
        Text("HAKKINDA / DOSYALAR", parent=p, origin=(0, 0), position=(0, 0.36), scale=2.0, color=AMBER)
        txt = (
            "KONTROLLER\n"
            "W A S D  hareket     SPACE  zıpla     CTRL / C  çömel     SHIFT  yavaş yürü\n"
            "Sol tık  ateş        Sağ tık  dürbün (AWP)     R  şarjör     Q  önceki silah\n"
            "1 / 2 / 3  silah slotu    Fare tekerleği  silah değiştir\n"
            "B  silah marketi     TAB  skor tablosu     ESC  duraklat\n\n"
            "OYUN: Takım deathmatch. Botlar sana ve birbirine karşı savaşır.\n"
            "Her öldürme +$300 kazandırır. Limite ilk ulaşan takım kazanır.\n\n"
            "DOSYALAR (oyunun olduğu klasörde otomatik taranır)\n"
            "maps/*.bsp  |  *.wad  |  models/v_*.mdl  |  models/player/*/*.mdl  |  sound/\n"
            "Eksik dosya olursa o parça yedek (basit) modelle çizilir, oyun yine çalışır.\n"
            "Elde görünen silahın konumu kayıksa main.py içindeki VM_OFFSET değerini değiştir."
        )
        Text(txt, parent=p, origin=(-0.5, 0.5), position=(-0.6, 0.3), scale=0.95, color=WHITE)
        self.mkbtn(p, "GERİ", -0.37, lambda: self.show("main"), w=0.3, h=0.05)
        self.pages["about"] = p

    def build_loading(self):
        r = Entity(parent=camera.ui, enabled=False, z=-5)
        Entity(parent=r, model="quad", color=BG, scale=(3, 2), z=1)
        Text("YÜKLENİYOR...", parent=r, origin=(0, 0), position=(0, 0.03), scale=3, color=AMBER)
        self.load_txt = Text("", parent=r, origin=(0, 0), position=(0, -0.05), scale=1.1, color=GREY)
        return r

    def show(self, name):
        for k, p in self.pages.items():
            p.enabled = (k == name)
        self.page = name
        if name == "maps":
            self.fill_maps()
        if name == "settings":
            self.refresh_settings()

    def start(self):
        if not MAPS:
            self.status.text = "Harita yok! 'maps' klasörüne .bsp koy."
            self.status.color = RED
            return
        save_settings()
        mp = MAPS[self.sel_map]
        self.load_name = mp.stem
        self.load_txt.text = f"Harita: {mp.stem}"
        self.loading.enabled = True
        self.root.enabled = False
        self.loader = G.start_steps(mp)

    def tick_loader(self):
        """Yüklemeyi her karede bir adım ilerlet (ekran donmaz, ilerleme görünür)."""
        try:
            msg = next(self.loader)
            set_text(self.load_txt, f"Harita: {self.load_name}\n{msg}")
        except StopIteration:
            self.loader = None
            self.loading.enabled = False
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.loader = None
            try:
                G.stop()
            except Exception:
                pass
            self.root.enabled = True
            self.loading.enabled = False
            self.status.text = f"Harita yüklenemedi: {e}"
            self.status.color = RED
            self.show("main")

    def animate_bg(self, dt):
        for i, e in enumerate(self.stripes):
            e.x += dt * 0.05
            e.y += dt * 0.016
            if e.x > 1.2:
                e.x = -1.2
                e.y = -0.7

MENU = None

def goto_menu():
    G.stop()
    MENU.root.enabled = True
    MENU.show("main")
    MENU.refresh_status()
    SND.start_music()

# ---------------------------------------------------------------- Ursina kancaları
def update():
    if MENU and MENU.loader is not None:
        MENU.tick_loader()
        return
    if G.running:
        G.update(time.dt)
    elif MENU and MENU.root.enabled:
        MENU.animate_bg(time.dt)

def input(key):
    if G.running:
        G.input(key)
    elif key == "escape" and MENU and MENU.page not in (None, "main"):
        MENU.show("main")

MENU = Menu()
SND.start_music()
app.run()
