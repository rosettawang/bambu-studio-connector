#!/usr/bin/env python3
"""Color blends ("purge gradients") for Bambu Lab sliced plates.

After a filament change, the old color doesn't vanish at once: it fades out of the nozzle over the
next few tens of mm^3. Bambu purges that into the chute and the prime tower. This tool skips most of
that purge and instead prints a chosen zone first, ordered so the fading old color lands as a
gradient: darkest where the zone should be darkest, clean where it should be clean.

Subcommands
  inspect      list layers, filament changes and the settings a blend depends on
  prep         patch an unsliced Bambu project (.3mf): per-layer filament order(s), arc fitting off.
               Slice it afterwards (Bambu Studio or its CLI). Prefer the spec's per-change "purge" over
               --zero-flush, which changes that pair's purge for the whole print.
  blend        reorder one filament change in a sliced plate (.gcode.3mf) and write a new one,
               plus an HTML dashboard with predicted colors
  report       (re)build the dashboard for an original/blended pair
  make-strip   write a calibration strip model (2 layers, top painted with the second filament)
  calibrate    save a measured clear-out rate for a printer + color pair
  hook         Bambu Studio post-processing entry: reorder the G-code file Studio passes, in place.
               Put it in the project's Process > Others > Post-processing Scripts so clicking
               "Slice plate" keeps the blend.

Needs numpy; the dashboard also needs matplotlib. Nothing here talks to a printer.
"""
import argparse, base64, datetime, hashlib, html, io, json, math, os, re, sys, zipfile
import numpy as np

FIL_AREA = math.pi * 0.875 ** 2           # mm^2, 1.75 mm filament
CAL_PATH = os.path.expanduser('~/.bambu-studio-connector/blend-calibration.json')
DEFAULT_HALF_LIVES = (24.0, 41.0, 66.0)    # mm^3; fast / typical / slow until calibrated
PLAIN_ONSET = 10.0                         # mm^3 that come out nearly pure old color
HERE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------- files

def read_plate(path, plate=1):
    z = zipfile.ZipFile(path)
    gname = f'Metadata/plate_{plate}.gcode'
    if gname not in z.namelist():
        sys.exit(f'{path}: no {gname} - is this a sliced plate (.gcode.3mf)?')
    cfg = {}
    if 'Metadata/project_settings.config' in z.namelist():
        cfg = json.loads(z.read('Metadata/project_settings.config'))
    return dict(path=path, gcode=z.read(gname).decode('utf-8', 'replace'), config=cfg, plate=plate)


def write_plate(src, out, gcode, plate=1):
    gname = f'Metadata/plate_{plate}.gcode'
    gb = gcode.encode()
    zi = zipfile.ZipFile(src)
    tmp = out + '.tmp'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zo:
        for info in zi.infolist():
            b = zi.read(info.filename)
            if info.filename == gname:
                b = gb
            elif info.filename == gname + '.md5':
                b = hashlib.md5(gb).hexdigest().upper().encode()
            zo.writestr(info, b)
    os.replace(tmp, out)

# ----------------------------------------------------------------------------- G-code helpers

MOVE = re.compile(r'^\s*G([0-3])\s(.*)$')
TOOL = re.compile(r'^\s*T(\d{1,2})\s*$')


def params(rest):
    d = {}
    for tok in rest.split(';')[0].split():
        if len(tok) > 1 and tok[0] in 'XYZEFIJ':
            try:
                d[tok[0]] = float(tok[1:])
            except ValueError:
                pass
    return d


def layer_heights(lines):
    """[(z, first_line, last_line_exclusive)] from Bambu's '; Z_HEIGHT:' tags"""
    marks = [(i, float(l.split(':')[1])) for i, l in enumerate(lines) if l.startswith('; Z_HEIGHT:')]
    out = []
    for k, (i, z) in enumerate(marks):
        out.append((z, i, marks[k + 1][0] if k + 1 < len(marks) else len(lines)))
    return out


def tool_changes(lines):
    """[(line, z, from_tool, to_tool)] - tools are 0-based as in G-code"""
    z, tool, out = 0.0, None, []
    for i, l in enumerate(lines):
        if l.startswith('; Z_HEIGHT:'):
            z = float(l.split(':')[1])
        m = TOOL.match(l)
        if m:
            t = int(m.group(1))
            out.append((i, z, tool, t))
            tool = t
    return out


def hex_rgb(h):
    h = h.lstrip('#')[:6]
    return np.array([int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)])


def filament_colors(cfg, n=16):
    cols = cfg.get('filament_colour') or []
    if isinstance(cols, str):
        cols = cols.split(';')
    out = [hex_rgb(c) if c else np.array([.5, .5, .5]) for c in cols]
    while len(out) < n:
        out.append(np.array([.6, .6, .6]))
    return out


def flush_value(cfg, frm, to):
    m = cfg.get('flush_volumes_matrix')
    if not m:
        return None
    n = int(round(math.sqrt(len(m))))
    try:
        return float(m[frm * n + to]) * float((cfg.get('flush_multiplier') or ['1'])[0])
    except (IndexError, ValueError):
        return None

# ----------------------------------------------------------------------------- zones

def point_in_poly(x, y, poly):
    """vectorised even-odd test; x, y arrays"""
    poly = np.asarray(poly, float)
    inside = np.zeros(np.shape(x), bool)
    xj, yj = poly[-1]
    for xi, yi in poly:
        cond = ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / ((yj - yi) + 1e-12) + xi)
        inside ^= cond
        xj, yj = xi, yi
    return inside


def dist_to_polyline(x, y, pts):
    pts = np.asarray(pts, float)
    if len(pts) == 1:
        return np.hypot(x - pts[0, 0], y - pts[0, 1])
    best = np.full(np.shape(x), np.inf)
    for (ax, ay), (bx, by) in zip(pts[:-1], pts[1:]):
        dx, dy = bx - ax, by - ay
        t = np.clip(((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy + 1e-12), 0, 1)
        best = np.minimum(best, np.hypot(x - (ax + t * dx), y - (ay + t * dy)))
    return best


class Zone:
    """Where the blend goes and in what order.

    key: higher = should be cleaner = printed later. Moves are grouped into bins of `bin` key units;
    inside a bin, groups (e.g. left and right wing) alternate so mirrored parts get the same shade.
    Spec (JSON):
      {"layer_z": 0.32, "from": 1, "to": 2,          # filaments 1-based, as Bambu Studio numbers them
       "zones": [{"polygon": [[x,y],...], "group": "left"}, ...],   # optional; default: whole layer
       "clean_edge": [[x,y],[x,y],...]  or  "clean_point": [x,y],    # cleanest end (plate mm)
       "key_npz": "field.npz",          # alternative: arrays key (H,W), optional group (H,W), x0, y0, px_mm
       "key_min": 0.12, "bin": 0.5,
       "purge": 0,        # optional: purge (mm3) for this one change only; others keep theirs
       "split_mm": 1.5}   # optional: cut lines into pieces so lines crossing the gradient are ordered too
    Several blends: {"blends": [{...}, {...}]} - each item takes the keys above; top-level keys are defaults.
    """

    def __init__(self, spec, spec_dir='.'):
        self.spec = spec
        self.polys = [(z['polygon'], str(z.get('group', k))) for k, z in enumerate(spec.get('zones', []))]
        self.npz = None
        if spec.get('key_npz'):
            p = spec['key_npz']
            p = p if os.path.isabs(p) else os.path.join(spec_dir, p)
            f = np.load(p)
            kn, gn = spec.get('key_name', 'key'), spec.get('group_name', 'group')
            self.npz = dict(key=f[kn], group=f[gn] if gn in f else None,
                            x0=float(f['x0']), y0=float(f['y0']), px=float(f['px_mm']))
            self.key_min = float(spec.get('key_min', 0.0))
            self.bin = float(spec.get('bin', 0.05))
        else:
            if 'clean_edge' in spec:
                self.clean = spec['clean_edge']
            elif 'clean_point' in spec:
                self.clean = [spec['clean_point']]
            else:
                sys.exit('zone spec needs clean_edge, clean_point or key_npz')
            self.bin = float(spec.get('bin', 0.5))

    def classify(self, x, y):
        """-> (key or None, group)"""
        if self.npz is not None:
            n = self.npz
            j = int(round((x - n['x0']) / n['px']))
            i = int(round((y - n['y0']) / n['px']))
            if not (0 <= i < n['key'].shape[0] and 0 <= j < n['key'].shape[1]):
                return None, None
            k = float(n['key'][i, j])
            if not np.isfinite(k) or k <= self.key_min:
                return None, None
            g = str(int(n['group'][i, j])) if n['group'] is not None else '0'
            return k, g
        group = '0'
        if self.polys:
            for poly, g in self.polys:
                if point_in_poly(np.array([x]), np.array([y]), poly)[0]:
                    group = g
                    break
            else:
                return None, None
        return -float(dist_to_polyline(np.array([x]), np.array([y]), self.clean)[0]), group

# ----------------------------------------------------------------------------- the reorder

def find_change(lines, z, frm, to):
    for i, cz, a, b in tool_changes(lines):
        if abs(cz - z) < 1e-3 and a == frm and b == to:
            return i
    seen = [(round(cz, 3), a, b) for _, cz, a, b in tool_changes(lines) if abs(cz - z) < 1e-3]
    sys.exit(f'no change from filament {frm + 1} to {to + 1} on layer z={z}. Changes on that layer: '
             + ', '.join(f'{(a + 1) if a is not None else "-"}->{b + 1}' for _, a, b in seen)
             + '. Set the layer\'s filament order with `prep --order` and re-slice.')


def trim_purge(L, tc, stop, target_mm3):
    """Scale the purge (FLUSH blocks) of the change at line tc to target_mm3. Returns filament mm removed."""
    blocks, inside = [], False
    for n in range(tc, stop):
        s = L[n].strip()
        if s == '; FLUSH_START':
            inside = True
        elif s == '; FLUSH_END':
            inside = False
        elif inside:
            m = MOVE.match(L[n])
            if m and set(params(m.group(2))) <= {'E', 'F'} and params(m.group(2)).get('E', 0) > 0:
                blocks.append(n)
    total = sum(params(MOVE.match(L[n]).group(2))['E'] for n in blocks)
    if not blocks or total <= 0:
        return 0.0, 0.0
    k = max(0.0, min(1.0, target_mm3 / (total * FIL_AREA)))
    removed = 0.0
    for n in blocks:
        p = params(MOVE.match(L[n]).group(2)); e = p['E']; ne = e * k; removed += e - ne
        L[n] = (f'G1 E{ne:.5f}' + (f" F{p['F']:g}" if 'F' in p else '')) if ne > 1e-5 else '; (purge trimmed for color blend)'
    for n in range(tc, stop):     # tell the firmware the shorter flush length too
        mm = re.match(r'^(M620\.10 A1 .*?\bL)([\d.]+)(.*)$', L[n])
        if mm:
            L[n] = f'{mm.group(1)}{float(mm.group(2)) * k:g}{mm.group(3)}'
    return removed, total * FIL_AREA


def blend_gcode(text, zone, z, frm, to, log, purge=None, split_mm=None):
    L = text.split('\n')
    tc = find_change(L, z, frm, to)
    # prime tower that the slicer prints right after the change: from the end of the change G-code
    # ('M1007 S1' / 'M621 S..A' on Bambu printers) to '; WIPE_TOWER_END'
    wt_end = next((n for n in range(tc, min(tc + 4000, len(L))) if L[n].startswith('; WIPE_TOWER_END')), None)
    obj = next(n for n in range(tc, len(L)) if L[n].startswith('; FEATURE:') and 'Prime' not in L[n]
               and n > (wt_end or tc))
    tw0 = tw1 = None
    if wt_end is not None and wt_end < obj:
        ends = [n for n in range(tc, wt_end) if L[n].strip() == 'M1007 S1' or re.match(r'^M621 S\d+A', L[n].strip())]
        if ends:
            tw0, tw1 = ends[-1] + 1, wt_end + 1
    removed = 0.0
    if purge is not None:
        removed, was = trim_purge(L, tc, tw0 or obj, float(purge))
        log.append(f'purge for {frm + 1}->{to + 1} at z={z:g}: {was:.0f} -> {was - removed * FIL_AREA:.0f} mm3 (other changes untouched)')
    start = obj
    end = next((n for n in range(start + 1, len(L)) if TOOL.match(L[n]) or L[n].startswith('; WIPE_TOWER_START')
                or L[n].startswith('; CHANGE_LAYER') or L[n].startswith('; FEATURE: Prime')), len(L))
    blk = L[start:end]
    if any(re.match(r'^\s*G[23]\s', l) for l in blk):
        sys.exit('arc moves (G2/G3) in this section - slice with arc fitting off (`prep --no-arcs`) and retry')
    # head position at the start of the section
    x = y = 0.0
    for l in L[:start]:
        m = MOVE.match(l)
        if m:
            d = params(m.group(2)); x = d.get('X', x); y = d.get('Y', y)
    x0, y0 = x, y
    band, rest, fe = [], [], 'F1200'
    for l in blk:
        m = MOVE.match(l)
        if not m:
            rest.append(('raw', l)); continue
        d = params(m.group(2))
        nx, ny, e = d.get('X', x), d.get('Y', y), d.get('E', 0.0)
        xy = 'X' in d or 'Y' in d
        if 'F' in d and e > 0:
            fe = f"F{d['F']:g}"
        if e > 0 and xy:
            # optionally cut a line into pieces so a line that crosses the gradient is ordered piece by piece
            seg_len = math.hypot(nx - x, ny - y)
            npc = max(1, math.ceil(seg_len / split_mm)) if split_mm else 1
            pieces = []
            for q in range(npc):
                ax_, ay_ = x + (nx - x) * q / npc, y + (ny - y) * q / npc
                bx_, by_ = x + (nx - x) * (q + 1) / npc, y + (ny - y) * (q + 1) / npc
                k, g = zone.classify((ax_ + bx_) / 2, (ay_ + by_) / 2)
                kb = None if k is None else (math.floor(k / zone.bin), g)
                if pieces and pieces[-1][0] == kb:
                    pieces[-1][1][2:4] = [bx_, by_]; pieces[-1][1][4] += e / npc
                else:
                    pieces.append([kb, [ax_, ay_, bx_, by_, e / npc, fe], k, g])
            for kb, mv, k, g in pieces:
                mv = tuple(mv)
                (band.append((k, g, len(band), mv)) if kb is not None else rest.append(('ext', mv)))
        elif xy and e < 0:      # wipe: keep it only if it follows a kept extrusion
            rest.append(('wipe', (l, nx, ny)) if rest and rest[-1][0] == 'ext' else ('raw', f'G1 E{e:.5f} F1800'))
        elif xy:
            rest.append(('trav', (nx, ny)))
        else:
            rest.append(('raw', l))
        x, y = nx, ny
    xe, ye = x, y
    if not band:
        sys.exit('no extrusion of the target filament falls inside the zone on that layer')
    # order: bins of key (low first), groups alternating inside each bin, slicer order inside a group
    bins = {}
    for k, g, i, mv in band:
        bins.setdefault((math.floor(k / zone.bin), g), []).append((i, mv))
    groups = sorted({g for _, g in bins})
    order = []
    for n, b in enumerate(sorted({b for b, _ in bins})):
        for g in (groups if n % 2 == 0 else groups[::-1]):
            order += [mv for _, mv in sorted(bins.get((b, g), []))]

    def emit(moves, px, py):
        o = []
        for ax, ay, bx, by, e, f in moves:
            if abs(ax - px) > 1e-3 or abs(ay - py) > 1e-3:
                far = math.hypot(ax - px, ay - py) > 2.0
                o += (['G1 E-.8 F1800'] if far else []) + [f'G1 X{ax:.3f} Y{ay:.3f} F12000'] + (['G1 E.8 F1800'] if far else [])
            o.append(f'G1 X{bx:.3f} Y{by:.3f} E{e:.5f} {f}')
            px, py = bx, by
        return o, px, py

    fx, fy = order[0][0], order[0][1]
    if tw0 is not None:
        tower = L[tw0:tw1]
        unret = next((params(MOVE.match(l).group(2))['E'] for l in tower
                      if MOVE.match(l) and set(params(MOVE.match(l).group(2))) <= {'E', 'F'}
                      and params(MOVE.match(l).group(2)).get('E', 0) > 0), 0.8)
        head = ['; COLOR_BLEND_START', f'G1 X{fx:.3f} Y{fy:.3f} F30000', f'G1 Z{z:.3f} F3000',
                f'G1 E{unret:g} F1800', '; FEATURE: Top surface']
        body, _, _ = emit(order, fx, fy)
        tail = [f'G1 E-{unret:g} F1800', f'G1 Z{z + 0.4:.3f} F3000', '; COLOR_BLEND_END', '; FEATURE: Prime tower']
        rest_out, px, py = [], x0, y0          # L[tw1:start] already travels to the section start
        pre, mid = L[:tw0], L[tw1:start]
        log.append(f'prime tower after the change moved behind the blend zone (lines {tw0}-{tw1})')
    else:
        tower, pre, mid = [], L[:start], []
        head = ['; COLOR_BLEND_START', 'G1 E-.8 F1800', f'G1 X{fx:.3f} Y{fy:.3f} F12000', 'G1 E.8 F1800',
                '; FEATURE: Top surface']
        body, _, _ = emit(order, fx, fy)
        tail = ['; COLOR_BLEND_END']
        rest_out, px, py = [], order[-1][2], order[-1][3]
        log.append('no prime tower right after the change; blend zone printed first')
    for kind, v in rest:
        if kind == 'raw':
            rest_out.append(v)
        elif kind == 'wipe':
            rest_out.append(v[0]); px, py = v[1], v[2]
        elif kind == 'trav':
            if abs(v[0] - px) > 1e-3 or abs(v[1] - py) > 1e-3:
                rest_out.append(f'G1 X{v[0]:.3f} Y{v[1]:.3f} F12000')
            px, py = v
        else:
            o, px, py = emit([v], px, py); rest_out += o
    if abs(xe - px) > 1e-3 or abs(ye - py) > 1e-3:
        rest_out.append(f'G1 X{xe:.3f} Y{ye:.3f} F12000')
    out = pre + head + body + tail + tower + mid + rest_out + L[end:]
    info = dict(change_line=tc, moves=len(order), zone_volume=sum(m[4] for m in order) * FIL_AREA,
                rest_moves=sum(1 for k, _ in rest if k == 'ext'), tower_moved=tw0 is not None,
                purge_removed_mm=removed, layer_z=z, frm=frm + 1, to=to + 1)
    return '\n'.join(out), info


def blend_items(spec):
    base = {k: v for k, v in spec.items() if k != 'blends'}
    return [dict(base, **b) for b in spec['blends']] if 'blends' in spec else [base]


def apply_blends(text, spec, spec_dir, log):
    infos = []
    for b in blend_items(spec):
        zone = Zone(b, spec_dir)
        text, info = blend_gcode(text, zone, float(b['layer_z']), int(b['from']) - 1, int(b['to']) - 1, log,
                                 purge=b.get('purge'), split_mm=b.get('split_mm'))
        infos.append(info)
    return text, infos


def net_e(text):
    tot = 0.0
    for l in text.split('\n'):
        m = MOVE.match(l)
        if m:
            tot += params(m.group(2)).get('E', 0.0)
    return tot


def checks(orig, new, zs, removed=0.0):
    res = []
    d = net_e(new) - net_e(orig) + removed
    res.append(('Same filament as the slicer\'s file, apart from the trimmed purge', abs(d) < 0.01,
                f'difference {d:+.3f} mm after {removed:.1f} mm of purge removed'))
    zs = zs if isinstance(zs, (list, tuple)) else [zs]
    # no long extrusion that starts somewhere unexpected (e.g. from the purge chute)
    x = y = 0.0; bad = []
    cz = 0.0; feat = ''
    for n, l in enumerate(new.split('\n')):
        if l.startswith('; Z_HEIGHT:'):
            cz = float(l.split(':')[1])
        if l.startswith('; FEATURE:'):
            feat = l
        m = MOVE.match(l)
        if not m:
            continue
        p = params(m.group(2)); nx, ny = p.get('X', x), p.get('Y', y)
        if any(abs(cz - z) < 1e-3 for z in zs) and p.get('E', 0) > 0 and ('X' in p or 'Y' in p) and 'Prime' not in feat \
                and (x < 0 or y < 0 or math.hypot(nx - x, ny - y) > 120):
            bad.append(n)
        x, y = nx, ny
    res.append(('No stray lines from the purge chute or across the plate', not bad,
                f'{len(bad)} found' + (f' (first at line {bad[0]})' if bad else '')))
    return res

# ----------------------------------------------------------------------------- simulation + pictures

def layer_segments(text, z):
    """segments on layer z: (x0,y0,x1,y1, tool, prev_tool, mm3_since_change, feature, seq)"""
    segs, x, y, cz, tool, prev, V, feat, flush, seq, tower, zone = [], 0., 0., 0., None, None, 0., '', False, 0, False, False
    for l in text.split('\n'):
        if l.startswith('; Z_HEIGHT:'):
            cz = float(l.split(':')[1])
        elif l.startswith('; FEATURE:'):
            feat = l[10:].strip()
        elif l.strip() == '; FLUSH_START':
            flush = True
        elif l.strip() == '; FLUSH_END':
            flush = False
        elif l.startswith('; WIPE_TOWER_START'):
            tower = True
        elif l.startswith('; WIPE_TOWER_END') or l.startswith('; COLOR_BLEND_START'):
            tower = False
        if l.startswith('; COLOR_BLEND_START'):
            zone = True
        elif l.startswith('; COLOR_BLEND_END'):
            zone = False
        mt = TOOL.match(l)
        if mt:
            prev, tool, V = tool, int(mt.group(1)), 0.0
            continue
        m = MOVE.match(l)
        if not m:
            continue
        p = params(m.group(2)); nx, ny, e = p.get('X', x), p.get('Y', y), p.get('E', 0.0)
        xy = 'X' in p or 'Y' in p
        if e > 0 and xy and abs(cz - z) < 1e-3 and 'Prime' not in feat and 'Custom' not in feat and not tower:
            segs.append((x, y, nx, ny, tool, prev, V, feat if not zone else feat + ' [blend zone]', seq)); seq += 1
        if e > 0 and (xy or flush or 'Compensate' in l):
            V += e * FIL_AREA
        x, y = nx, ny
    return segs


def old_fraction(V, half_life):
    return math.exp(-max(V - PLAIN_ONSET, 0.0) * math.log(2) / half_life)


def lifted(colors, idx):
    """dark filaments (black, navy) are invisible on screen: brighten the dark ones in the blends"""
    lum = lambda c: 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    top = max(lum(colors[i]) for i in idx)
    if top >= 0.25:
        return colors, 1.0
    f = 0.32 / max(top, 0.02)
    return [np.clip(c * f, 0, 1) if (i in idx and lum(c) < 0.25) else c for i, c in enumerate(colors)], f


def render(segs, colors, pair_h, mode, bbox=None, out=None):
    """pair_h: {(from, to): half_life} for the blended changes (0-based tools)"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    lines, cols = [], []
    in_zone = lambda s: s[7].endswith('[blend zone]')
    blend_seq = [s[8] for s in segs if in_zone(s)]
    lo, hi = (min(blend_seq), max(blend_seq)) if blend_seq else (0, 1)
    cmap = matplotlib.colormaps['viridis']
    for x0, y0, x1, y1, t, p, V, feat, seq in segs:
        c = colors[t] if t is not None else np.array([.5, .5, .5])
        if mode == 'predict' and (p, t) in pair_h:
            f = old_fraction(V, pair_h[(p, t)])
            c = colors[p] * f + colors[t] * (1 - f)
        elif mode == 'order':
            if feat.endswith('[blend zone]'):
                c = np.array(cmap((seq - lo) / max(hi - lo, 1))[:3])
            else:
                c = c * 0.25 + 0.75 * np.array([.92, .92, .92])
        lines.append([(x0, y0), (x1, y1)]); cols.append(c)
    fig = plt.figure(figsize=(6, 6 * (bbox[3] - bbox[2]) / (bbox[1] - bbox[0])))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.add_collection(LineCollection(lines, colors=cols, linewidths=1.6, capstyle='round'))
    ax.set_xlim(bbox[0], bbox[1]); ax.set_ylim(bbox[2], bbox[3]); ax.set_aspect('equal'); ax.axis('off')
    fig.patch.set_alpha(0)
    buf = io.BytesIO(); fig.savefig(buf, format='png', dpi=130, transparent=True); plt.close(fig)
    data = buf.getvalue()
    if out:
        open(out, 'wb').write(data)
    return 'data:image/png;base64,' + base64.b64encode(data).decode()


def layer_bbox(segs, margin=3.0):
    xs = [v for s in segs for v in (s[0], s[2])]; ys = [v for s in segs for v in (s[1], s[3])]
    xs = [v for v in xs if v > 0]; ys = [v for v in ys if v > 0]
    return (min(xs) - margin, max(xs) + margin, min(ys) - margin, max(ys) + margin)

# ----------------------------------------------------------------------------- calibration store

def cal_key(printer, c_from, c_to):
    return f'{printer}|{c_from.upper()}|{c_to.upper()}'


def cal_load():
    try:
        return json.load(open(CAL_PATH))
    except (OSError, ValueError):
        return {}


def printer_name(cfg):
    return cfg.get('printer_settings_id') or cfg.get('printer_model') or 'unknown printer'

# ----------------------------------------------------------------------------- dashboard

def build_report(orig, new, spec, infos, chk, out_html, plate_out, log):
    cfg = new.get('config') or orig['config']
    pairs = [(i['frm'] - 1, i['to'] - 1) for i in infos]
    z = infos[0]['layer_z']                      # the dashboard shows the (first) blended layer
    raw_colors = filament_colors(cfg)
    colors, lift = lifted(raw_colors, sorted({t for pr in pairs for t in pr}))
    so, sn = layer_segments(orig['gcode'], z), layer_segments(new['gcode'], z)
    bbox = layer_bbox([s for s in sn if s[0] > 0])
    printer = printer_name(cfg)
    hexes = (cfg.get('filament_colour') or ['#888888'] * 16)
    cals = {pr: cal_load().get(cal_key(printer, hexes[pr[0]], hexes[pr[1]])) for pr in pairs}
    any_cal = any(cals.values())
    def ph(h):                                   # calibrated pairs keep their own rate
        return {pr: (cals[pr]['half_life'] if cals[pr] else h) for pr in pairs}
    panels = ([('Calibrated', None)] if any_cal else []) + list(zip(('Fast clear-out', 'Typical', 'Slow clear-out'), DEFAULT_HALF_LIVES))
    main = ph(DEFAULT_HALF_LIVES[1])
    def cap(lbl, h):
        if h is None:
            return 'calibrated rates: ' + ', '.join(f'{a + 1}→{b + 1} {cals[(a, b)]["half_life"]:.0f} mm³' for a, b in pairs if cals[(a, b)])
        return f'half of the old color gone every {h:.0f} mm³' + (' (calibrated pairs use their own rate)' if any_cal else '')
    preds = ''.join(
        f'<figure class="card{" cal" if h is None else ""}"><img src="{render(sn, colors, ph(h if h else DEFAULT_HALF_LIVES[1]), "predict", bbox)}" alt="Predicted colors, {lbl.lower()}">'
        f'<figcaption><b>{html.escape(lbl)}</b> · {cap(lbl, h)}</figcaption></figure>'
        for lbl, h in panels)
    m = re.search(r'total estimated time: ([^\n;]+)', new['gcode'])
    w = re.search(r'total filament weight \[g\] : ([^\n]+)', new['gcode'])
    rows = [('Plate file', html.escape(os.path.basename(plate_out))), ('Printer profile', html.escape(printer))]
    for i in infos:
        a_, b_ = i['frm'] - 1, i['to'] - 1
        fv = flush_value(cfg, a_, b_)
        purge_now = (fv - i['purge_removed_mm'] * FIL_AREA) if fv is not None else None
        rows.append((f'Blend {i["frm"]} → {i["to"]}',
                     f'<span class="sw" style="background:{hexes[a_]}"></span>filament {a_ + 1} → '
                     f'<span class="sw" style="background:{hexes[b_]}"></span>filament {b_ + 1} at {i["layer_z"]:g} mm · '
                     f'{i["zone_volume"]:.0f} mm³ zone, {i["moves"]} pieces · purge '
                     + (f'{max(purge_now, 0):.0f} mm³ (was {fv:.0f})' if purge_now is not None else 'unknown')
                     + (' · tower moved after the zone' if i['tower_moved'] else '')))
    rows += [('Print time (slicer estimate)', html.escape(m.group(1).strip()) if m else '—'),
             ('Filament (g, per slot)', html.escape(w.group(1).strip()) if w else '—')]
    table = ''.join(f'<tr><th>{a}</th><td>{b}</td></tr>' for a, b in rows)
    chk_html = ''.join(f'<li class="{"ok" if ok else "bad"}"><span>{"✓" if ok else "✗"}</span> {html.escape(t)} '
                       f'<small>{html.escape(d)}</small></li>' for t, ok, d in chk)
    note = (f'Dark filaments on this page are brightened {lift:.1f}× so they can be told apart on screen.'
            if lift > 1 else 'Colors are the filament colors set in the project.')
    tpl = open(os.path.join(HERE, '..', 'assets', 'blend_report.html'), encoding='utf-8').read()
    fill = {
        'TITLE': html.escape(spec.get('title') or os.path.splitext(os.path.basename(plate_out))[0]),
        'DATE': datetime.date.today().strftime('%d %b %Y'),
        'PLATE_FILE': html.escape(plate_out),
        'MAIN_LABEL': 'calibrated' if any_cal else 'typical (not calibrated yet)',
        'MAIN_PRED': render(sn, colors, main, 'predict', bbox),
        'STUDIO_VIEW': render(sn, colors, {}, 'flat', bbox),
        'ORDER_VIEW': render(sn, colors, main, 'order', bbox),
        'UNORDERED': render(so, colors, main, 'predict', bbox),
        'PREDICTIONS': preds,
        'TABLE': table,
        'CHECKS': chk_html,
        'COLOR_NOTE': html.escape(note),
        'LOG': html.escape(' · '.join(log)),
        'CAL_STATUS': ('Pairs marked calibrated use the rate measured from a test print.' if any_cal else
                       'Not calibrated for this printer and these colors yet, so three clear-out rates are shown.'),
    }
    for k, v in fill.items():
        tpl = tpl.replace('{{' + k + '}}', v)
    open(out_html, 'w', encoding='utf-8').write(tpl)

# ----------------------------------------------------------------------------- 3MF project prep

def prep_project(src, out, layer=None, order=None, zero_flush=None, no_arcs=False, flush_value_=0, orders=None):
    """orders: [(layer, [filaments...]), ...] - several per-layer filament orders (1-based)"""
    if layer and order:
        orders = (orders or []) + [(layer, order)]
    zi = zipfile.ZipFile(src)
    tmp = out + '.tmp'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zo:
        for info in zi.infolist():
            b = zi.read(info.filename)
            if info.filename == 'Metadata/project_settings.config':
                cfg = json.loads(b)
                if zero_flush:
                    a, c = zero_flush
                    mtx = cfg['flush_volumes_matrix']; n = int(round(math.sqrt(len(mtx))))
                    mtx[(a - 1) * n + (c - 1)] = str(flush_value_)
                if no_arcs:
                    cfg['enable_arc_fitting'] = '0'
                    dd = cfg.get('different_settings_to_system')
                    if dd:
                        dd[0] = ';'.join(sorted(set(filter(None, dd[0].split(';'))) | {'enable_arc_fitting'}))
                b = json.dumps(cfg, indent=4).encode()
            elif info.filename == 'Metadata/model_settings.config' and orders:
                s = b.decode()
                s = re.sub(r'\s*<metadata key="other_layers_print_sequence(_nums)?" value="[^"]*"/>', '', s)
                seq = ' '.join(f'{ly} {ly} ' + ' '.join(str(o) for o in od) for ly, od in sorted(orders))
                s = s.replace('<metadata key="plater_id"', f'<metadata key="other_layers_print_sequence" value="{seq}"/>\n'
                              f'    <metadata key="other_layers_print_sequence_nums" value="{len(orders)}"/>\n    <metadata key="plater_id"', 1)
                b = s.encode()
            zo.writestr(info, b)
    os.replace(tmp, out)

# ----------------------------------------------------------------------------- calibration strip

def paint_code(n):
    """Bambu triangle paint attribute for filament n (1-based)"""
    return {1: '4', 2: '8'}.get(n, f'{n - 3:X}C')


def make_strip(out, length=80.0, width=12.0, first=0.2, second=0.12, to=2):
    """A flat strip, `length` along Y, two layers thick (first + second). In Bambu Studio give the top
    layer filament `to` with a height range modifier (see comment below).
    Slice with the top lines running across the strip (along X), print the strip as a blend zone with
    clean_point at the far Y end, and read the clear-out rate off the photo."""
    t = first + second
    V = [(0, 0, 0), (width, 0, 0), (width, length, 0), (0, length, 0),
         (0, 0, t), (width, 0, t), (width, length, t), (0, length, t)]
    T = [((0, 2, 1), None), ((0, 3, 2), None),
         ((4, 5, 6), paint_code(to)), ((4, 6, 7), paint_code(to)),
         ((0, 1, 5), None), ((0, 5, 4), None), ((1, 2, 6), None), ((1, 6, 5), None),
         ((2, 3, 7), None), ((2, 7, 6), None), ((3, 0, 4), None), ((3, 4, 7), None)]
    vx = ''.join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in V)
    tx = ''.join(f'<triangle v1="{a}" v2="{b}" v3="{c}"' + (f' paint_color="{p}"' if p else '') + '/>'
                 for (a, b, c), p in T)
    # Plain 3MF: Bambu Studio ignores paint_color here (and crashes if the file claims to be a Bambu
    # project without the rest of one), so the top layer gets its filament in Studio: right-click the
    # strip > Add height range modifier > from the first layer height to the top > filament `to`.
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<resources><object id="1" type="model"><mesh><vertices>{vx}</vertices><triangles>{tx}</triangles>'
             '</mesh></object></resources><build><item objectid="1"/></build></model>')
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr('3D/3dmodel.model', model)

# ----------------------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('inspect'); p.add_argument('plate'); p.add_argument('--plate-index', type=int, default=1)
    p = sub.add_parser('prep'); p.add_argument('project'); p.add_argument('--out', required=True)
    p.add_argument('--layer', type=int, help='1-based layer number for --order'); p.add_argument('--order', help='filament order on that layer, e.g. 1,2,3,4')
    p.add_argument('--layer-order', action='append', default=[], help='LAYER:ORDER, repeatable, e.g. 2:2,3,4,1 --layer-order 3:2,1,3,4')
    p.add_argument('--zero-flush', help='FROM:TO filaments (1-based) whose purge is set to --flush (default 0)')
    p.add_argument('--flush', type=float, default=0); p.add_argument('--no-arcs', action='store_true')
    p = sub.add_parser('blend'); p.add_argument('plate'); p.add_argument('--spec', required=True)
    p.add_argument('--out', required=True); p.add_argument('--report'); p.add_argument('--plate-index', type=int, default=1)
    p = sub.add_parser('report'); p.add_argument('original'); p.add_argument('blended'); p.add_argument('--spec', required=True)
    p.add_argument('--out', required=True)
    p = sub.add_parser('make-strip'); p.add_argument('--out', required=True)
    p.add_argument('--length', type=float, default=80); p.add_argument('--width', type=float, default=12)
    p.add_argument('--to', type=int, default=2)
    p = sub.add_parser('hook'); p.add_argument('--spec', required=True); p.add_argument('gcode')
    p = sub.add_parser('calibrate'); p.add_argument('--plate', help='sliced plate, to read printer + colors from')
    p.add_argument('--printer'); p.add_argument('--from-color'); p.add_argument('--to-color')
    p.add_argument('--from', dest='frm', type=int); p.add_argument('--to', type=int)
    p.add_argument('--half-life', type=float, required=True)
    a = ap.parse_args()

    if a.cmd == 'inspect':
        pl = read_plate(a.plate, a.plate_index); cfg = pl['config']; L = pl['gcode'].split('\n')
        hexes = cfg.get('filament_colour') or []
        out = dict(printer=printer_name(cfg), filaments=hexes, arc_fitting=cfg.get('enable_arc_fitting'),
                   prime_tower=cfg.get('enable_prime_tower'), changes=[])
        for i, z, f, t in tool_changes(L):
            if f is None:
                continue
            out['changes'].append(dict(layer_z=z, frm=f + 1, to=t + 1, line=i, purge_mm3=flush_value(cfg, f, t)))
        print(json.dumps(out, indent=1))
    elif a.cmd == 'prep':
        zf = tuple(int(v) for v in a.zero_flush.split(':')) if a.zero_flush else None
        order = [int(v) for v in a.order.split(',')] if a.order else None
        orders = [(int(v.split(':')[0]), [int(x) for x in v.split(':')[1].split(',')]) for v in a.layer_order]
        prep_project(a.project, a.out, a.layer, order, zf, a.no_arcs, a.flush, orders)
        print('wrote', a.out)
    elif a.cmd in ('blend', 'report'):
        spec_path = a.spec; spec = json.load(open(spec_path)); sdir = os.path.dirname(os.path.abspath(spec_path))
        log = []
        if a.cmd == 'blend':
            orig = read_plate(a.plate, a.plate_index)
            for b in blend_items(spec):
                fv = flush_value(orig['config'], int(b['from']) - 1, int(b['to']) - 1)
                if b.get('purge') is None and fv is not None and fv > 30:
                    log.append(f"warning: purge for {b['from']}->{b['to']} is still {fv:.0f} mm3 - set \"purge\" in the spec")
            text, infos = apply_blends(orig['gcode'], spec, sdir, log)
            removed = sum(i['purge_removed_mm'] for i in infos)
            chk = checks(orig['gcode'], text, [i['layer_z'] for i in infos], removed)
            if all(ok for _, ok, _ in chk):
                write_plate(a.plate, a.out, text, a.plate_index)
            new = dict(orig, gcode=text)
            report_out = a.report
        else:
            orig, new = read_plate(a.original), read_plate(a.blended)
            _, infos = apply_blends(orig['gcode'], spec, sdir, log)
            chk = checks(orig['gcode'], new['gcode'], [i['layer_z'] for i in infos], sum(i['purge_removed_mm'] for i in infos))
            report_out = a.out
        if report_out:
            build_report(orig, new, spec, infos, chk, report_out, a.out if a.cmd == 'blend' else a.blended, log)
        print(json.dumps(dict(blends=infos, checks=[dict(check=t, ok=ok, detail=d) for t, ok, d in chk], log=log,
                              out=a.out, report=report_out), indent=1))
        if not all(ok for _, ok, _ in chk):
            sys.exit(2)
    elif a.cmd == 'hook':
        spec = json.load(open(a.spec))
        orig = open(a.gcode, encoding='utf-8', errors='replace').read()
        if '; COLOR_BLEND_START' in orig:
            print('already blended'); return
        log = []
        text, infos = apply_blends(orig, spec, os.path.dirname(os.path.abspath(a.spec)), log)
        chk = checks(orig, text, [i['layer_z'] for i in infos], sum(i['purge_removed_mm'] for i in infos))
        if not all(ok for _, ok, _ in chk):
            sys.exit('color blend: safety check failed, G-code left unchanged: '
                     + '; '.join(f'{t}: {d}' for t, ok, d in chk if not ok))
        open(a.gcode, 'w', encoding='utf-8').write(text)
        print('color blend: ' + '; '.join(f"{i['frm']}->{i['to']} at z={i['layer_z']:g}: {i['moves']} pieces, {i['zone_volume']:.0f} mm3" for i in infos))
    elif a.cmd == 'make-strip':
        make_strip(a.out, a.length, a.width, to=a.to); print('wrote', a.out)
    elif a.cmd == 'calibrate':
        printer, cf, ct = a.printer, a.from_color, a.to_color
        if a.plate:
            cfg = read_plate(a.plate)['config']; hexes = cfg.get('filament_colour') or []
            printer = printer or printer_name(cfg)
            cf = cf or hexes[a.frm - 1]; ct = ct or hexes[a.to - 1]
        if not (printer and cf and ct):
            sys.exit('need --plate with --from/--to, or --printer --from-color --to-color')
        data = cal_load()
        data[cal_key(printer, cf, ct)] = dict(half_life=a.half_life, date=datetime.date.today().isoformat())
        os.makedirs(os.path.dirname(CAL_PATH), exist_ok=True)
        json.dump(data, open(CAL_PATH, 'w'), indent=1)
        print('saved', cal_key(printer, cf, ct), '->', a.half_life, 'mm3')


if __name__ == '__main__':
    main()
