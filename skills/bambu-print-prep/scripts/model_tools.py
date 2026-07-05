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


def _write_3mf(parts, offsets, path):
    """Write a multi-object 3MF; each part is its own object, positioned by its offset."""
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
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        "<resources>%s</resources><build>%s</build></model>" % ("".join(obj_xml), "".join(item_xml))
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


def _all_axis_rotations():
    import itertools

    mats = []
    for perm in itertools.permutations(range(3)):
        # permutation parity
        parity = 1
        pl = list(perm)
        for i in range(3):
            for j in range(i + 1, 3):
                if pl[i] > pl[j]:
                    parity = -parity
        for signs in itertools.product((1, -1), repeat=3):
            if parity * signs[0] * signs[1] * signs[2] != 1:
                continue  # keep proper rotations only (no mirroring)
            M = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
            for row in range(3):
                M[row][perm[row]] = signs[row]
            mats.append(M)
    return mats


_ROTS = _all_axis_rotations()


def _lay_flat(p):
    """Rotate the part (axis-aligned 90-degree steps) to minimize height, then maximize footprint."""
    best_key, best_verts = None, p["verts"]
    for M in _ROTS:
        V = [
            (
                M[0][0] * x + M[0][1] * y + M[0][2] * z,
                M[1][0] * x + M[1][1] * y + M[1][2] * z,
                M[2][0] * x + M[2][1] * y + M[2][2] * z,
            )
            for x, y, z in p["verts"]
        ]
        xs = [v[0] for v in V]
        ys = [v[1] for v in V]
        zs = [v[2] for v in V]
        key = (round(max(zs) - min(zs), 3), -round((max(xs) - min(xs)) * (max(ys) - min(ys)), 3))
        if best_key is None or key < best_key:
            best_key, best_verts = key, V
    p = dict(p)
    p["verts"] = best_verts
    return p


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


def cmd_extract(args):
    parts = select_parts(load_parts(args.file), args.parts)
    if not parts:
        raise SystemExit("No parts matched selection %r — run inspect to see indices/names" % (args.parts,))
    scale = float(args.scale)
    ext = os.path.splitext(args.out)[1].lower()
    names = ", ".join(p["name"] for p in parts)

    if ext == ".3mf":
        if args.lay_flat:
            parts = [_lay_flat(p) for p in parts]
        norm = [_normalize_part(p, scale) for p in parts]
        offsets, (fw, fd) = _arrange(norm, float(args.bed), float(args.gap))
        _write_3mf(norm, offsets, args.out)
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
    for name, fn, needs_out in (("inspect", cmd_inspect, False), ("render", cmd_render, True), ("extract", cmd_extract, True)):
        sp = sub.add_parser(name)
        sp.add_argument("file")
        sp.add_argument("--parts", default=None, help="comma-separated indices and/or name substrings")
        if needs_out:
            sp.add_argument("--out", required=True)
        if name == "extract":
            sp.add_argument("--scale", default="1.0", help="uniform scale factor, e.g. 0.2 for 20%%")
            sp.add_argument("--bed", default="380", help="usable bed width/depth in mm for .3mf arrangement")
            sp.add_argument("--gap", default="8", help="spacing between arranged parts in mm")
            sp.add_argument("--lay-flat", dest="lay_flat", action="store_true", help="rotate each part (90-degree steps) to lie flat before arranging (.3mf only)")
        sp.set_defaults(fn=fn)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
