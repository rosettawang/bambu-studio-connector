#!/usr/bin/env python3
"""Inspect, preview, and extract parts from 3MF/STL model files (print prep).

Usage:
  python3 model_tools.py inspect <file>
  python3 model_tools.py render  <file> --out preview.png [--parts SEL]
  python3 model_tools.py extract <file> --parts SEL --out part.stl

SEL = comma-separated part indices and/or case-insensitive name substrings,
e.g. --parts 0,2  or  --parts base  or  --parts "roof,brood box"

inspect uses only the standard library. render needs numpy + matplotlib
(pip install numpy matplotlib --break-system-packages).
"""
import argparse
import datetime
import json
import math
import os
import re
import struct
import sys
import zipfile
import xml.etree.ElementTree as ET


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _parse_tf(s):
    if not s:
        return None
    try:
        v = [float(x) for x in s.replace(",", " ").split()]
    except ValueError:
        return None
    return v if len(v) == 12 else None


def _apply(tf, p):
    # 3MF row-vector convention: [x y z 1] * [[m00..m02],[m10..],[m20..],[m30..]]
    x, y, z = p
    return (
        x * tf[0] + y * tf[3] + z * tf[6] + tf[9],
        x * tf[1] + y * tf[4] + z * tf[7] + tf[10],
        x * tf[2] + y * tf[5] + z * tf[8] + tf[11],
    )


class ThreeMF:
    def __init__(self, path):
        self.z = zipfile.ZipFile(path)
        self.models = {}
        self.names = self._bambu_names()
        root = "3D/3dmodel.model"
        if root not in self.z.namelist():
            cand = [n for n in self.z.namelist() if n.endswith(".model")]
            if not cand:
                raise SystemExit("No .model document found inside this 3MF")
            root = cand[0]
        self.root = root

    def _bambu_names(self):
        """Bambu Studio stores object/part names in Metadata/model_settings.config."""
        names = {}
        try:
            data = self.z.read("Metadata/model_settings.config")
        except KeyError:
            return names
        try:
            cfg = ET.fromstring(data)
        except ET.ParseError:
            return names
        for el in cfg.iter():
            if _local(el.tag) in ("object", "part") and el.get("id"):
                for md in el:
                    if _local(md.tag) == "metadata" and md.get("key") == "name":
                        names.setdefault(el.get("id"), md.get("value"))
        return names

    def model(self, zpath):
        zpath = zpath.lstrip("/")
        if zpath in self.models:
            return self.models[zpath]
        xmlroot = ET.fromstring(self.z.read(zpath))
        objects, build = {}, []
        for el in xmlroot.iter():
            t = _local(el.tag)
            if t == "object":
                obj = {"id": el.get("id"), "name": el.get("name"), "mesh": None, "components": []}
                for ch in el:
                    ct = _local(ch.tag)
                    if ct == "mesh":
                        verts, tris = [], []
                        for sub in ch.iter():
                            st = _local(sub.tag)
                            if st == "vertex":
                                verts.append((float(sub.get("x")), float(sub.get("y")), float(sub.get("z"))))
                            elif st == "triangle":
                                tris.append((int(sub.get("v1")), int(sub.get("v2")), int(sub.get("v3"))))
                        obj["mesh"] = (verts, tris)
                    elif ct == "components":
                        for c in ch:
                            if _local(c.tag) != "component":
                                continue
                            p = None
                            for k, v in c.attrib.items():
                                if _local(k) == "path":
                                    p = v
                            obj["components"].append((c.get("objectid"), p, _parse_tf(c.get("transform"))))
                objects[obj["id"]] = obj
            elif t == "item":
                p = None
                for k, v in el.attrib.items():
                    if _local(k) == "path":
                        p = v
                build.append((el.get("objectid"), p, _parse_tf(el.get("transform"))))
        self.models[zpath] = (objects, build)
        return self.models[zpath]


def _parts_3mf(path):
    tm = ThreeMF(path)
    parts = []

    def emit(zpath, oid, tfs, inherited):
        objects, _ = tm.model(zpath)
        obj = objects.get(oid)
        if obj is None:
            return
        name = obj.get("name") or tm.names.get(oid) or inherited
        if obj["mesh"]:
            verts, tris = obj["mesh"]
            w = verts
            for tf in tfs:
                if tf:
                    w = [_apply(tf, p) for p in w]
            parts.append({"name": name or "object-%s" % oid, "object_id": oid, "verts": w, "tris": tris})
        for coid, cpath, ctf in obj["components"]:
            emit(cpath or zpath, coid, [ctf] + tfs, name)

    objects, build = tm.model(tm.root)
    if build:
        for oid, bpath, btf in build:
            emit(bpath or tm.root, oid, [btf], None)
    else:
        for oid in objects:
            emit(tm.root, oid, [], None)
    for i, p in enumerate(parts):
        p["index"] = i
    return parts


def _read_stl(path):
    with open(path, "rb") as f:
        data = f.read()
    is_ascii = data[:5].lower() == b"solid"
    if is_ascii and len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if len(data) == 84 + n * 50:
            is_ascii = False  # binary file whose header happens to start with 'solid'
    tris = []
    if is_ascii:
        txt = data.decode("ascii", "ignore")
        nums = re.findall(r"vertex\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)", txt)
        for i in range(0, len(nums) - 2, 3):
            tris.append(tuple(tuple(float(c) for c in nums[j]) for j in range(i, i + 3)))
        return tris
    if len(data) < 84:
        raise SystemExit("STL file too small / corrupt")
    n = struct.unpack_from("<I", data, 80)[0]
    off = 84
    for _ in range(n):
        if off + 50 > len(data):
            break
        f12 = struct.unpack_from("<12f", data, off)
        tris.append(((f12[3], f12[4], f12[5]), (f12[6], f12[7], f12[8]), (f12[9], f12[10], f12[11])))
        off += 50
    return tris


def _parts_stl(path):
    raw = _read_stl(path)
    vmap, verts, tris = {}, [], []
    for t in raw:
        idx = []
        for v in t:
            key = (round(v[0], 4), round(v[1], 4), round(v[2], 4))
            i = vmap.get(key)
            if i is None:
                i = len(verts)
                vmap[key] = i
                verts.append(v)
            idx.append(i)
        tris.append(tuple(idx))

    parent = list(range(len(verts)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b, c in tris:
        ra, rb, rc = find(a), find(b), find(c)
        if rb != ra:
            parent[rb] = ra
        if rc != ra:
            parent[find(c)] = ra
    groups = {}
    for t in tris:
        groups.setdefault(find(t[0]), []).append(t)

    base = os.path.splitext(os.path.basename(path))[0]
    parts = []
    ordered = sorted(groups.values(), key=len, reverse=True)
    for gi, gtris in enumerate(ordered):
        used = sorted({i for t in gtris for i in t})
        remap = {old: new for new, old in enumerate(used)}
        parts.append(
            {
                "index": gi,
                "name": base if len(ordered) == 1 else "%s-shell-%d" % (base, gi),
                "object_id": str(gi),
                "verts": [verts[i] for i in used],
                "tris": [(remap[a], remap[b], remap[c]) for a, b, c in gtris],
            }
        )
    return parts


def load_parts(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".3mf":
        return _parts_3mf(path)
    if ext == ".stl":
        return _parts_stl(path)
    raise SystemExit("Unsupported file type %r — this tool reads .stl and .3mf" % ext)


def part_stats(p):
    V = p["verts"]
    if not V:
        return {"size_mm": [0, 0, 0], "bbox_min": [0, 0, 0], "triangles": 0, "volume_cm3": 0}
    xs = [v[0] for v in V]
    ys = [v[1] for v in V]
    zs = [v[2] for v in V]
    vol = 0.0
    for a, b, c in p["tris"]:
        ax, ay, az = V[a]
        bx, by, bz = V[b]
        cx, cy, cz = V[c]
        vol += ax * (by * cz - bz * cy) + bx * (cy * az - cz * ay) + cx * (ay * bz - az * by)
    return {
        "size_mm": [round(max(xs) - min(xs), 1), round(max(ys) - min(ys), 1), round(max(zs) - min(zs), 1)],
        "bbox_min": [round(min(xs), 1), round(min(ys), 1), round(min(zs), 1)],
        "triangles": len(p["tris"]),
        "volume_cm3": round(abs(vol) / 6.0 / 1000.0, 2),
    }


def select_parts(parts, sel):
    if not sel:
        return parts
    tokens = [t.strip() for t in sel.split(",") if t.strip()]
    out = []
    for p in parts:
        for t in tokens:
            if (t.isdigit() and int(t) == p["index"]) or (
                not t.isdigit() and t.lower() in (p["name"] or "").lower()
            ):
                out.append(p)
                break
    return out


def cmd_inspect(args):
    parts = load_parts(args.file)
    report = {
        "file": args.file,
        "format": os.path.splitext(args.file)[1].lower().lstrip("."),
        "part_count": len(parts),
        "parts": [
            dict(index=p["index"], name=p["name"], object_id=p["object_id"], **part_stats(p)) for p in parts
        ],
        "notes": [
            "size_mm is the bounding box [x, y, z] in the file's placed orientation",
            "volume_cm3 assumes closed meshes; treat as approximate",
            "select parts by index or name substring in render/extract --parts",
        ],
    }
    print(json.dumps(report, indent=2))


def cmd_render(args):
    try:
        import numpy as np
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    except ImportError:
        raise SystemExit("Missing deps. Run: pip install numpy matplotlib --break-system-packages")

    parts = select_parts(load_parts(args.file), args.parts)
    if not parts:
        raise SystemExit("No parts matched selection %r" % (args.parts,))
    combined = parts
    MAX_PANELS = 11
    if len(parts) > MAX_PANELS:
        parts = sorted(parts, key=lambda p: part_stats(p)["volume_cm3"], reverse=True)[:MAX_PANELS]
        print(
            "Note: %d parts total; showing individual panels for the %d largest by volume "
            "(tiny shells are often engraved text/details)" % (len(combined), MAX_PANELS)
        )
    n = len(parts) + (1 if len(combined) > 1 else 0)
    cols = min(3, n)
    rows = math.ceil(n / cols)
    fig = plt.figure(figsize=(4.6 * cols, 4.2 * rows))
    cmap = plt.get_cmap("tab10")

    def draw(ax, plist, title):
        pts = []
        for k, p in enumerate(plist):
            V = np.asarray(p["verts"])
            T = np.asarray(p["tris"])
            if len(T) > 4000:
                T = T[np.random.RandomState(0).choice(len(T), 4000, replace=False)]
            col = cmap(p["index"] % 10)
            ax.add_collection3d(Poly3DCollection(V[T], alpha=0.9, facecolor=col, edgecolor="none"))
            pts.append(V)
        P = np.vstack(pts)
        mins, maxs = P.min(0), P.max(0)
        span = float((maxs - mins).max()) or 1.0
        ctr = (maxs + mins) / 2
        ax.set_xlim(ctr[0] - span / 2, ctr[0] + span / 2)
        ax.set_ylim(ctr[1] - span / 2, ctr[1] + span / 2)
        ax.set_zlim(ctr[2] - span / 2, ctr[2] + span / 2)
        ax.set_box_aspect((1, 1, 1))
        ax.set_title(title, fontsize=9)

    slot = 1
    if len(combined) > 1:
        ax = fig.add_subplot(rows, cols, slot, projection="3d")
        slot += 1
        draw(ax, combined[:60], "all parts (as placed in file)")
    for p in parts:
        ax = fig.add_subplot(rows, cols, slot, projection="3d")
        slot += 1
        s = part_stats(p)
        draw(ax, [p], "[%d] %s\n%s x %s x %s mm" % (p["index"], p["name"], *s["size_mm"]))
    fig.tight_layout()
    fig.savefig(args.out, dpi=110)
    print("Wrote %s (%d part(s))" % (args.out, len(parts)))


def _normal(a, b, c):
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    ln = math.sqrt(nx * nx + ny * ny + nz * nz)
    return (nx / ln, ny / ln, nz / ln) if ln > 0 else (0.0, 0.0, 0.0)


def _write_stl(tris, path):
    with open(path, "wb") as f:
        f.write(b"extracted by model_tools".ljust(80, b" "))
        f.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            f.write(struct.pack("<12fH", *_normal(a, b, c), *a, *b, *c, 0))


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


# 3MF core spec metadata names Bambu Studio reads for its Project panel. Title shows as
# the project name, Description as the body; Designer/Copyright/LicenseTerms carry the
# original author's attribution through an extract instead of silently dropping it.
def _read_3mf_meta(path):
    """Pull the <metadata> block out of a 3MF's root model document."""
    try:
        z = zipfile.ZipFile(path)
    except Exception:
        return {}
    name = "3D/3dmodel.model"
    if name not in z.namelist():
        cand = [n for n in z.namelist() if n.endswith(".model")]
        if not cand:
            return {}
        name = cand[0]
    try:
        root = ET.fromstring(z.read(name))
    except Exception:
        return {}
    out = {}
    for el in root:
        if _local(el.tag) == "metadata" and el.get("name") and el.text:
            out[el.get("name")] = el.text
    return out


def _write_3mf(parts, offsets, path, meta=None):
    """Write a multi-object 3MF; each part is its own object, positioned by its offset.

    `meta` becomes the project metadata Bambu Studio shows in its Project panel.
    Per the 3MF core spec these elements must precede <resources>.
    """
    obj_xml, item_xml = [], []
    for i, (p, (ox, oy, oz)) in enumerate(zip(parts, offsets)):
        oid = i + 1
        vs = "".join('<vertex x="%.4f" y="%.4f" z="%.4f"/>' % v for v in p["verts"])
        ts = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % t for t in p["tris"])
        obj_xml.append(
            '<object id="%d" name="%s" type="model"><mesh><vertices>%s</vertices><triangles>%s</triangles></mesh></object>'
            % (oid, _esc(p["name"] or "part-%d" % oid), vs, ts)
        )
        item_xml.append('<item objectid="%d" transform="1 0 0 0 1 0 0 0 1 %.4f %.4f %.4f"/>' % (oid, ox, oy, oz))
    meta_xml = "".join(
        '<metadata name="%s">%s</metadata>' % (k, _esc(str(v)))
        for k, v in (meta or {}).items()
        if v not in (None, "")
    )
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        "%s<resources>%s</resources><build>%s</build></model>"
        % (meta_xml, "".join(obj_xml), "".join(item_xml))
    )
    ct = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel-1" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)


def _tri_geom(V, T):
    """Per-triangle unit normal and area, dropping degenerate slivers.

    Returns (tris, norms, areas) as parallel lists so callers can index across them.
    Winding is assumed CCW-outward (both STL and 3MF specify that), so normals
    point out of the solid.
    """
    tris, norms, areas = [], [], []
    for a, b, c in T:
        ax, ay, az = V[a]
        bx, by, bz = V[b]
        cx, cy, cz = V[c]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        m = math.sqrt(nx * nx + ny * ny + nz * nz)
        if m < 1e-12:
            continue  # zero-area triangle carries no orientation information
        tris.append((a, b, c))
        norms.append((nx / m, ny / m, nz / m))
        areas.append(m / 2.0)
    return tris, norms, areas


def _centroid(V, T):
    """Center of mass assuming uniform density: volume-weighted for closed meshes,
    area-weighted for open shells, bbox-average as a last resort."""
    vol = 0.0
    sx = sy = sz = 0.0
    for a, b, c in T:
        ax, ay, az = V[a]
        bx, by, bz = V[b]
        cx, cy, cz = V[c]
        d = ax * (by * cz - bz * cy) + bx * (cy * az - cz * ay) + cx * (ay * bz - az * by)
        vol += d
        sx += d * (ax + bx + cx)
        sy += d * (ay + by + cy)
        sz += d * (az + bz + cz)
    if abs(vol) > 1e-9:
        s = 1.0 / (4.0 * vol)
        return (sx * s, sy * s, sz * s)
    tot = 0.0
    sx = sy = sz = 0.0
    shell_tris, _, shell_areas = _tri_geom(V, T)
    for (a, b, c), area in zip(shell_tris, shell_areas):
        ax, ay, az = V[a]
        bx, by, bz = V[b]
        cx, cy, cz = V[c]
        tot += area
        sx += area * (ax + bx + cx) / 3.0
        sy += area * (ay + by + cy) / 3.0
        sz += area * (az + bz + cz) / 3.0
    if tot > 1e-12:
        return (sx / tot, sy / tot, sz / tot)
    n = max(1, len(V))
    return (sum(v[0] for v in V) / n, sum(v[1] for v in V) / n, sum(v[2] for v in V) / n)


_AXIS_DOWNS = [(1.0, 0, 0), (-1.0, 0, 0), (0, 1.0, 0), (0, -1.0, 0), (0, 0, 1.0), (0, 0, -1.0)]


def _candidate_downs(norms, areas, limit):
    """Directions worth testing as "this face goes against the bed".

    A part at rest sits on one of its own faces, so the candidates are the mesh's
    face normals, merged into area-weighted buckets and ranked by total area. The
    six axis directions are always included so boxy parts keep a sane fallback.
    """
    buckets = {}
    q = 24  # ~2.4 deg per bucket step
    for (nx, ny, nz), area in zip(norms, areas):
        key = (round(nx * q), round(ny * q), round(nz * q))
        b = buckets.get(key)
        if b is None:
            buckets[key] = [nx * area, ny * area, nz * area, area]
        else:
            b[0] += nx * area
            b[1] += ny * area
            b[2] += nz * area
            b[3] += area
    out = list(_AXIS_DOWNS)
    for b in sorted(buckets.values(), key=lambda b: -b[3])[:limit]:
        m = math.sqrt(b[0] * b[0] + b[1] * b[1] + b[2] * b[2])
        if m < 1e-12:
            continue
        d = (b[0] / m, b[1] / m, b[2] / m)
        if not any(d[0] * o[0] + d[1] * o[1] + d[2] * o[2] > 0.9995 for o in out):
            out.append(d)
    return out


def _rot_to_down(d):
    """Rotation matrix taking unit vector d onto (0, 0, -1) — that face ends up on the bed."""
    dx, dy, dz = d
    c = -dz  # cos of the angle between d and straight down
    if c > 1 - 1e-9:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    if c < -1 + 1e-9:
        return [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]]  # 180 deg about X
    s = math.sqrt(dx * dx + dy * dy)  # |d x (0,0,-1)| = sin of that angle
    ax, ay = -dy / s, dx / s  # rotation axis (unit, lies in the XY plane)
    k = 1.0 - c
    return [
        [c + k * ax * ax, k * ax * ay, s * ay],
        [k * ax * ay, c + k * ay * ay, -s * ax],
        [-s * ay, s * ax, c],
    ]


def _hull2d(pts):
    """Convex hull of 2D points, counter-clockwise (monotone chain)."""
    P = sorted(set(pts))
    if len(P) < 3:
        return P

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2:
                (ox, oy), (bx, by) = out[-2], out[-1]
                if (bx - ox) * (p[1] - oy) - (by - oy) * (p[0] - ox) > 1e-12:
                    break
                out.pop()
            out.append(p)
        return out

    lower, upper = half(P), half(reversed(P))
    return lower[:-1] + upper[:-1]


def _inside_hull2d(pt, hull, margin=0.0):
    """True if pt is inside a CCW convex hull, requiring `margin` mm of clearance."""
    if len(hull) < 3:
        return False
    x, y = pt
    n = len(hull)
    for i in range(n):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        elen = math.sqrt(ex * ex + ey * ey)
        if elen < 1e-12:
            continue
        # signed distance from the edge, positive on the interior side of a CCW hull
        if (ex * (y - ay) - ey * (x - ax)) / elen < margin:
            return False
    return True


def _min_bbox_spin(pts):
    """Angle (radians) to rotate about Z so the footprint's bounding box is smallest.

    Only affects how tightly _arrange can pack parts; it never changes which face
    is on the bed. Exact via rotating calipers: an optimal box shares a hull edge.
    """
    hull = _hull2d([(round(x, 4), round(y, 4)) for x, y in pts])
    if len(hull) < 3:
        return 0.0
    best_area, best_ang = None, 0.0
    for i in range(len(hull)):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % len(hull)]
        ang = -math.atan2(by - ay, bx - ax)
        ca, sa = math.cos(ang), math.sin(ang)
        xs = [x * ca - y * sa for x, y in hull]
        ys = [x * sa + y * ca for x, y in hull]
        area = (max(xs) - min(xs)) * (max(ys) - min(ys))
        if best_area is None or area < best_area - 1e-9:
            best_area, best_ang = area, ang
    return best_ang


# Relative weights for the orientation score. Contact area and overhang dominate
# print success; the center-of-mass term is what distinguishes a part from its
# 180-degree flip (both have identical height and identical bounding box).
_W_CONTACT, _W_OVERHANG, _W_COM, _W_HEIGHT = 1.0, 0.8, 0.55, 0.25


def _lay_flat(p):
    """Rotate the part so its best real face rests on the bed.

    Returns (part, info). Candidates are the mesh's own face normals rather than
    24 axis-aligned rotations, and each is scored on bed contact area, how much
    downward-facing area would need support, how low the center of mass sits, and
    overall height. Orientations whose center of mass falls outside the contact
    patch are treated as tipping over and only used if nothing else qualifies.
    """
    V = p["verts"]
    tris, norms, areas = _tri_geom(V, p["tris"])
    info = {"contact_mm2": 0.0, "height_mm": 0.0, "overhang_mm2": 0.0, "com_frac": 0.0,
            "stable": False, "tilted": False, "spin_deg": 0.0, "candidates": 0}
    if not tris or not V:
        return dict(p), info

    # A closed mesh wound inside-out has negative signed volume; its normals all point
    # into the solid, which would hide every downward face. Exporters do ship these,
    # so flip rather than give up. (Open shells sum to ~0 and are left alone.)
    sv = 0.0
    for a, b, c in tris:
        ax, ay, az = V[a]
        bx, by, bz = V[b]
        cx, cy, cz = V[c]
        sv += ax * (by * cz - bz * cy) + bx * (cy * az - cz * ay) + cx * (ay * bz - az * by)
    if sv < 0:
        norms = [(-n[0], -n[1], -n[2]) for n in norms]
        info["reversed_winding"] = True

    total_area = sum(areas)
    com = _centroid(V, p["tris"])
    limit = 28 if len(tris) <= 150000 else 10  # keep the sweep affordable on dense meshes
    downs = _candidate_downs(norms, areas, limit)
    info["candidates"] = len(downs)

    scored = []
    for d in downs:
        R = _rot_to_down(d)
        r0, r1, r2 = R
        zs = [r2[0] * x + r2[1] * y + r2[2] * z for x, y, z in V]
        zmin, zmax = min(zs), max(zs)
        height = zmax - zmin
        if height < 1e-9:
            continue
        band = zmin + max(0.05, 0.0015 * height)  # a real bed touches within a layer or so

        contact = 0.0
        overhang = 0.0
        touch = []
        for i, (a, b, c) in enumerate(tris):
            nz = r2[0] * norms[i][0] + r2[1] * norms[i][1] + r2[2] * norms[i][2]
            if nz > -0.7071:  # not facing downward enough to matter
                continue
            za, zb, zc = zs[a], zs[b], zs[c]
            if max(za, zb, zc) <= band and nz <= -0.9:
                contact += areas[i]
                touch.extend((a, b, c))
            elif min(za, zb, zc) > band:
                overhang += areas[i]  # steep downward face off the bed: needs support

        hull = _hull2d(
            [
                (round(r0[0] * V[k][0] + r0[1] * V[k][1] + r0[2] * V[k][2], 4),
                 round(r1[0] * V[k][0] + r1[1] * V[k][1] + r1[2] * V[k][2], 4))
                for k in set(touch)
            ]
        )
        com_xy = (
            r0[0] * com[0] + r0[1] * com[1] + r0[2] * com[2],
            r1[0] * com[0] + r1[1] * com[1] + r1[2] * com[2],
        )
        com_z = r2[0] * com[0] + r2[1] * com[1] + r2[2] * com[2]
        scored.append({
            "R": R, "d": d, "contact": contact, "overhang": overhang, "height": height,
            "com_frac": min(1.0, max(0.0, (com_z - zmin) / height)),
            "stable": contact > 0 and _inside_hull2d(com_xy, hull),
        })

    if not scored:
        return dict(p), info

    pool = [s for s in scored if s["stable"]] or scored
    max_contact = max(s["contact"] for s in pool) or 1.0
    max_height = max(s["height"] for s in pool) or 1.0
    for s in pool:
        s["score"] = (
            _W_CONTACT * (s["contact"] / max_contact)
            + _W_OVERHANG * (1.0 - (s["overhang"] / total_area if total_area else 0.0))
            + _W_COM * (1.0 - s["com_frac"])
            + _W_HEIGHT * (1.0 - s["height"] / max_height)
        )
    best = max(pool, key=lambda s: s["score"])

    R = best["R"]
    W = [
        (
            R[0][0] * x + R[0][1] * y + R[0][2] * z,
            R[1][0] * x + R[1][1] * y + R[1][2] * z,
            R[2][0] * x + R[2][1] * y + R[2][2] * z,
        )
        for x, y, z in V
    ]
    spin = _min_bbox_spin([(v[0], v[1]) for v in W])
    if abs(spin) > 1e-6:
        ca, sa = math.cos(spin), math.sin(spin)
        W = [(x * ca - y * sa, x * sa + y * ca, z) for x, y, z in W]

    dx, dy, dz = best["d"]
    info.update(
        contact_mm2=best["contact"],
        height_mm=best["height"],
        overhang_mm2=best["overhang"],
        com_frac=best["com_frac"],
        stable=best["stable"],
        # a non-axis-aligned resting face is something the old 90-degree search could never find
        tilted=max(abs(dx), abs(dy), abs(dz)) < 0.999,
        spin_deg=math.degrees(spin),
    )
    p = dict(p)
    p["verts"] = W
    return p, info


def _normalize_part(p, scale):
    """Scale, then move each part so its own min corner is at origin (z=0)."""
    V = [(v[0] * scale, v[1] * scale, v[2] * scale) for v in p["verts"]]
    xs = [v[0] for v in V]
    ys = [v[1] for v in V]
    zs = [v[2] for v in V]
    mx, my, mz = min(xs), min(ys), min(zs)
    p = dict(p)
    p["verts"] = [(v[0] - mx, v[1] - my, v[2] - mz) for v in V]
    p["size"] = (max(xs) - mx, max(ys) - my, max(zs) - mz)
    return p


def _arrange(parts, bed, gap):
    """Shelf-pack part footprints into rows, centered on the plate (origin = plate center)."""
    order = sorted(range(len(parts)), key=lambda i: -(parts[i]["size"][0] * parts[i]["size"][1]))
    rows, row, row_w, row_d = [], [], 0.0, 0.0
    for i in order:
        w, d, _ = parts[i]["size"]
        if row and row_w + gap + w > bed:
            rows.append((row, row_w, row_d))
            row, row_w, row_d = [], 0.0, 0.0
        row_w += (gap if row else 0) + w
        row_d = max(row_d, d)
        row.append(i)
    if row:
        rows.append((row, row_w, row_d))
    total_d = sum(r[2] for r in rows) + gap * (len(rows) - 1)
    offsets = [None] * len(parts)
    y = -total_d / 2
    for row, row_w, row_d in rows:
        x = -row_w / 2
        for i in row:
            w, d, _ = parts[i]["size"]
            offsets[i] = (x, y + (row_d - d) / 2, 0.0)
            x += w + gap
        y += row_d + gap
    footprint = (max(r[1] for r in rows), total_d)
    return offsets, footprint


def _flat_report(p, nfo):
    """One human-checkable line per part, so a bad orientation is visible before slicing."""
    if not nfo.get("contact_mm2"):
        return "lay-flat %r: no resting face found — left as-is" % (p["name"],)
    bits = [
        "%.0f mm2 on the bed" % nfo["contact_mm2"],
        "%.1f mm tall" % nfo["height_mm"],
        "center of mass at %.0f%% of height" % (100 * nfo["com_frac"]),
    ]
    if nfo["overhang_mm2"] > 0.5:
        bits.append("%.0f mm2 needs support" % nfo["overhang_mm2"])
    if nfo["tilted"]:
        bits.append("rests on an angled face")
    if not nfo["stable"]:
        bits.append("WARNING: center of mass overhangs the contact patch — may tip")
    return "lay-flat %r: %s" % (p["name"], "; ".join(bits))


def _3mf_pictures(path):
    """Model/profile pictures already embedded in a 3MF (what Studio's Project panel shows)."""
    try:
        names = zipfile.ZipFile(path).namelist()
    except Exception:
        return []
    return [n for n in names if n.startswith("Auxiliaries/") and not n.startswith("Auxiliaries/.thumbnails")]


def _set_3mf_meta(src, dst, updates, pictures=None):
    """Rewrite project metadata in a 3MF, copying every other archive entry through.

    The root model document is edited as text rather than reparsed, so Bambu's
    namespace declarations, requiredextensions attribute and element order all
    survive — reserializing with ElementTree would rewrite them.
    """
    zin = zipfile.ZipFile(src)
    names = zin.namelist()
    root = "3D/3dmodel.model"
    if root not in names:
        cand = [n for n in names if n.endswith(".model")]
        if not cand:
            raise SystemExit("No .model document found inside this 3MF")
        root = cand[0]
    xml = zin.read(root).decode("utf-8", "replace")

    for key, val in (updates or {}).items():
        if val is None:
            continue
        block = '<metadata name="%s">%s</metadata>' % (key, _esc(str(val)).replace(">", "&gt;"))
        pat = re.compile(r'<metadata\s+name="%s"\s*(?:/>|>.*?</metadata>)' % re.escape(key), re.S)
        if pat.search(xml):
            xml = pat.sub(lambda m: block, xml, count=1)  # lambda: val may contain backslashes
        else:
            m = re.search(r"<model\b[^>]*>", xml)
            if not m:
                raise SystemExit("Malformed 3MF: no <model> element in %s" % root)
            xml = xml[: m.end()] + block + xml[m.end() :]

    added = []
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == root:
                zout.writestr(item.filename, xml.encode("utf-8"))
            else:
                zout.writestr(item, zin.read(item.filename))
        for pic in pictures or []:
            if not os.path.isfile(pic):
                raise SystemExit("Picture not found: %s" % pic)
            arc = "Auxiliaries/Model Pictures/" + os.path.basename(pic)
            if arc in names:
                continue
            with open(pic, "rb") as f:
                zout.writestr(arc, f.read())
            added.append(arc)
    return added


def cmd_project(args):
    """Read or set the Project-panel fields (name, description, pictures) of a 3MF."""
    if os.path.splitext(args.file)[1].lower() != ".3mf":
        raise SystemExit("Project metadata lives in .3mf files only; %r is not one" % args.file)
    cur = _read_3mf_meta(args.file)
    updates = {}
    if args.set_name is not None:
        updates["Title"] = args.set_name
    if args.set_description is not None:
        updates["Description"] = args.set_description
    if args.set_designer is not None:
        updates["Designer"] = args.set_designer
    pics = args.add_picture or []

    if not updates and not pics:
        print(json.dumps({
            "file": args.file,
            "project_name": cur.get("Title", ""),
            "description": cur.get("Description", ""),
            "designer": cur.get("Designer", ""),
            "license": cur.get("License") or cur.get("LicenseTerms", ""),
            "pictures": _3mf_pictures(args.file),
            "all_metadata": cur,
        }, indent=2))
        return

    out = args.out or args.file
    tmp = out + ".tmp-project"
    try:
        added = _set_3mf_meta(args.file, tmp, updates, pics)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    bits = ["%s -> %r" % (k, v) for k, v in updates.items()]
    if added:
        bits.append("added %d picture(s): %s" % (len(added), ", ".join(added)))
    print("Updated %s: %s" % (out, "; ".join(bits)))
    if out == args.file:
        print("(edited in place — every other archive entry copied through unchanged)")


def cmd_extract(args):
    parts = select_parts(load_parts(args.file), args.parts)
    if not parts:
        raise SystemExit("No parts matched selection %r — run inspect to see indices/names" % (args.parts,))
    scale = float(args.scale)
    ext = os.path.splitext(args.out)[1].lower()
    names = ", ".join(p["name"] for p in parts)

    if args.lay_flat:
        laid = [_lay_flat(p) for p in parts]
        parts = [q for q, _ in laid]
        for q, nfo in laid:
            print(_flat_report(q, nfo))

    if ext == ".3mf":
        norm = [_normalize_part(p, scale) for p in parts]
        offsets, (fw, fd) = _arrange(norm, float(args.bed), float(args.gap))
        # Carry the source project's identity and attribution onto the new plate so it
        # opens as a named project in Studio instead of "Untitled".
        src = _read_3mf_meta(args.file) if os.path.splitext(args.file)[1].lower() == ".3mf" else {}
        meta = {
            "Application": "bambu-studio-connector",
            "CreationDate": datetime.date.today().isoformat(),
            "Title": getattr(args, "name", None) or os.path.splitext(os.path.basename(args.out))[0],
            "Description": getattr(args, "description", None)
            or "Parts: %s. Extracted from %s at %d%% scale%s."
            % (names, os.path.basename(args.file), scale * 100, ", laid flat" if args.lay_flat else ""),
            "Designer": src.get("Designer", ""),
            "Copyright": src.get("Copyright", ""),
            "LicenseTerms": src.get("License") or src.get("LicenseTerms", ""),
        }
        _write_3mf(norm, offsets, args.out, meta)
        warn = " WARNING: layout exceeds bed!" if fw > float(args.bed) or fd > float(args.bed) else ""
        print(
            "Wrote %s: %d part(s) [%s] at %d%% scale, arranged in a %.0f x %.0f mm layout (bed %s mm, %s mm gaps).%s"
            % (args.out, len(parts), names, scale * 100, fw, fd, args.bed, args.gap, warn)
        )
        return

    tris = []
    for p in parts:
        V = p["verts"]
        for a, b, c in p["tris"]:
            tris.append(tuple((V[k][0] * scale, V[k][1] * scale, V[k][2] * scale) for k in (a, b, c)))
    xs = [v[0] for t in tris for v in t]
    ys = [v[1] for t in tris for v in t]
    zs = [v[2] for t in tris for v in t]
    dx, dy, dz = -(min(xs) + max(xs)) / 2, -(min(ys) + max(ys)) / 2, -min(zs)
    _write_stl([tuple((v[0] + dx, v[1] + dy, v[2] + dz) for v in t) for t in tris], args.out)
    print(
        "Wrote %s: %d part(s) [%s] at %d%% scale, %d triangles, recentered with bottom at z=0"
        % (args.out, len(parts), names, scale * 100, len(tris))
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, needs_out in (
        ("inspect", cmd_inspect, False),
        ("render", cmd_render, True),
        ("extract", cmd_extract, True),
        ("project", cmd_project, False),
    ):
        sp = sub.add_parser(name)
        sp.add_argument("file")
        if name != "project":
            sp.add_argument("--parts", default=None, help="comma-separated indices and/or name substrings")
        if needs_out:
            sp.add_argument("--out", required=True)
        if name == "project":
            sp.add_argument("--out", default=None, help="write to a new .3mf instead of editing in place")
            sp.add_argument("--set-name", dest="set_name", default=None, help="Project Name shown in Studio")
            sp.add_argument("--set-description", dest="set_description", default=None, help="Description body")
            sp.add_argument("--set-designer", dest="set_designer", default=None, help="Designer/author credit")
            sp.add_argument("--add-picture", dest="add_picture", action="append", default=None,
                            help="image file to embed as a model picture (repeatable)")
        if name == "extract":
            sp.add_argument("--scale", default="1.0", help="uniform scale factor, e.g. 0.2 for 20%%")
            sp.add_argument("--bed", default="380", help="usable bed width/depth in mm for .3mf arrangement")
            sp.add_argument("--gap", default="8", help="spacing between arranged parts in mm")
            sp.add_argument("--name", default=None, help="Project Name for the written .3mf")
            sp.add_argument("--description", default=None, help="Project description for the written .3mf")
            sp.add_argument("--lay-flat", dest="lay_flat", action="store_true", help="rotate each part onto its best printable resting face (most bed contact, least overhang, lowest center of mass) before arranging")
        sp.set_defaults(fn=fn)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
