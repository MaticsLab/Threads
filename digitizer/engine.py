"""Layers -> stitches -> DST, with a QA pass that feeds back into the settings."""
from dataclasses import dataclass, asdict, field
import numpy as np
import cv2
import pystitch
from shapely.ops import unary_union

from . import core, segment
from inkstitchlib import fills as fill_methods


@dataclass
class Params:
    target_width_mm: float = 100.0
    hoop_w_mm: float = 100.0
    hoop_h_mm: float = 100.0
    row_spacing: float = 0.35        # tatami density
    satin_spacing: float = 0.175     # sample advance (half the edge density)
    max_satin: float = 8.0           # widest safe satin stitch
    max_stitch: float = 3.5
    min_stitch: float = 0.5
    pull_comp: float = 0.12
    trim_dist: float = 2.0
    underlay_spacing: float = 2.5
    min_fill_area: float = 0.20
    min_blob_area: float = 2.0    # below this, satin-ing a blob isn't worth it
    border_width: float = 1.0     # satin outline band around blobs
    fill_method: str = 'tatami'   # tatami | contour | circular (Ink/Stitch fills)
    fill_angle: float = 65.0
    heavy_underlay: bool = True


# ------------------------------------------------------------------ build
def build_pattern(layers, canvas_mm, width_px, p: Params):
    core.configure(p)
    scale = canvas_mm / width_px
    max_half_px = (p.max_satin / 2 + 1.0) / scale
    k = max(3, int(round(0.2 / scale)) | 1)
    min_area_px = max(40, int(round(0.35 / (scale * scale))))

    s = core.Sewer()
    stats = []
    for idx, L in enumerate(layers):
        m = core.clean(L['mask'], k=k, min_area_px=min_area_px)
        th = pystitch.EmbThread()
        th.color = L['hex_int']
        th.description = L['name']
        if idx > 0:
            s.color_break(th)
        else:
            s.pattern.add_thread(th)

        n_lab, lab = cv2.connectedComponents((m > 0).astype(np.uint8))
        comps = []
        for c in range(1, n_lab):
            comp = (lab == c).astype(np.uint8) * 255
            if comp.sum() / 255 < min_area_px:
                continue
            ys, xs = np.where(comp > 0)
            comps.append((comp, (xs.mean() * scale, ys.mean() * scale)))

        ncol = nfill = nblob = 0
        start_count = s.count
        # finish each shape before moving on — travelling back and forth
        # across the design is what generates jump stitches
        for comp, _c in core.order_by_nearest(comps, lambda t: t[1], s.pos):
            travel = unary_union(core.mask_to_polys(comp, scale))
            if travel.is_empty:
                continue
            columns = core.build_columns(comp, scale, max_half_px)

            covered = np.zeros_like(comp)
            th_px = max(3, int(round(0.42 / scale)))
            for col in columns:
                for l, r, _ in col:
                    cv2.line(covered, (int(l[0] / scale), int(l[1] / scale)),
                             (int(r[0] / scale), int(r[1] / scale)), 255, th_px)
            leftover = cv2.bitwise_and(comp, cv2.bitwise_not(covered))
            leftover = cv2.morphologyEx(leftover, cv2.MORPH_OPEN,
                                        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
            grow = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (th_px + 1,) * 2)
            leftover = cv2.bitwise_and(cv2.dilate(leftover, grow), comp)
            fills = [q for q in core.mask_to_polys(leftover, scale, simplify_mm=0.05)
                     if q.area > p.min_fill_area]

            for q in core.order_by_nearest(
                    fills, lambda z: (z.centroid.x, z.centroid.y), s.pos):
                g = q.buffer(0.25)
                if g.geom_type == 'MultiPolygon':
                    g = max(g.geoms, key=lambda z: z.area)

                # A rounded, blocky region (a head, a torso) reads badly as
                # plain tatami: the edge goes ragged and it never looks round.
                # If it is narrow enough to satin, give it the full treatment —
                # outline underlay, open underfill, satin core, satin outline.
                mw = core.poly_max_width(g) if g.area >= p.min_blob_area else 0.0
                if mw and mw <= p.max_satin:
                    core.sew_blob(s, g, p.row_spacing, p.max_satin,
                                  min(p.border_width, mw * 0.30),
                                  travel=travel,
                                  heavy_underlay=p.heavy_underlay)
                    nblob += 1
                else:
                    if q.area >= 3.0:
                        core.sew_edge_run(s, g, travel=travel)
                        if p.heavy_underlay:
                            core.sew_fill(s, g, 110.0, p.underlay_spacing, 3.0,
                                          stagger=False, start=s.pos, travel=travel)
                            core.sew_fill(s, g, 20.0, p.underlay_spacing, 3.0,
                                          stagger=False, start=s.pos, travel=travel)
                    fill_methods.sew_area(s, g, p.fill_method, p.fill_angle,
                                   p.row_spacing, p.max_stitch, travel=travel)
                    # too wide to satin across, but it still needs a hard edge:
                    # a satin outline over the fill is what stops a torso or a
                    # head reading as ragged
                    if q.area >= p.min_blob_area * 2:
                        ring = core.satin_ring(g, p.border_width, p.row_spacing)
                        if ring:
                            core.sew_ring(s, ring, travel)
                    nfill += 1

            for col in core.order_columns(columns, s.pos):
                core.sew_column(s, col, travel)
                ncol += 1
        stats.append({'name': L['name'], 'hex': L['hex'], 'columns': ncol,
                      'blobs': nblob, 'fills': nfill,
                      'stitches': s.count - start_count})

    s.tie_off()
    s.pattern.end()
    pat = s.pattern

    out, last, dropped = [], None, 0
    for x, y, cmd in pat.stitches:
        c = cmd & 0xFF
        if c == pystitch.STITCH:
            if last is not None and np.hypot(x - last[0], y - last[1]) < p.min_stitch * 10:
                dropped += 1
                continue
            out.append([x, y, cmd]); last = (x, y)
        else:
            out.append([x, y, cmd])
            last = (x, y) if c == pystitch.JUMP else None
    pat.stitches = out
    pat.move_center_to_origin()
    return pat, stats


def pattern_size_mm(pat):
    a = np.array([[q[0], q[1]] for q in pat.stitches]) / 10.0
    return (a[:, 0].max() - a[:, 0].min(), a[:, 1].max() - a[:, 1].min(),
            float(np.abs(a).max()))


# --------------------------------------------------------------------- QA
def qa_report(pat, layers, canvas_mm, width_px, p: Params):
    scale = canvas_mm / width_px
    w, h, reach = pattern_size_mm(pat)
    st = pat.stitches

    # travel events: one per needle move that leaves a thread behind
    events, prev, start, trimmed = [], None, None, False
    for x, y, c in st:
        k = c & 0xFF
        if k == pystitch.TRIM:
            trimmed = True
        elif k == pystitch.JUMP:
            if start is None:
                start = prev
        elif k == pystitch.STITCH:
            if start is not None:
                events.append((float(np.hypot(x - start[0], y - start[1]) / 10)
                               if start else 0.0, trimmed))
                start, trimmed = None, False
            prev = (x, y)
        elif k == pystitch.COLOR_CHANGE:
            prev = start = None; trimmed = False

    lens, prev = [], None
    for x, y, c in st:
        k = c & 0xFF
        if k == pystitch.STITCH:
            if prev is not None:
                lens.append(float(np.hypot(x - prev[0], y - prev[1]) / 10))
            prev = (x, y)
        elif k == pystitch.JUMP:
            prev = (x, y)
        else:
            prev = None

    # coverage: rasterise the thread and compare against each layer mask
    blocks, ci, prev = [[] for _ in layers], 0, None
    for x, y, c in st:
        k = c & 0xFF
        if k == pystitch.COLOR_CHANGE:
            ci += 1; prev = None; continue
        if k == pystitch.STITCH:
            if prev is not None and ci < len(layers):
                blocks[ci].append((prev, (x, y)))
            prev = (x, y)
        elif k == pystitch.JUMP:
            prev = (x, y)
        else:
            prev = None

    union = np.zeros(layers[0]['mask'].shape, np.uint8)
    for L in layers:
        union = np.maximum(union, L['mask'])
    ys, xs = np.where(union > 0)
    cx = (xs.min() + xs.max()) / 2 * scale
    cy = (ys.min() + ys.max()) / 2 * scale
    th_px = max(3, int(round(0.42 / scale)))

    per_layer = []
    for i, L in enumerate(layers):
        m = core.clean(L['mask'], k=max(3, int(round(0.2 / scale)) | 1))
        cov = np.zeros_like(m)
        for (x1, y1), (x2, y2) in blocks[i]:
            cv2.line(cov, (int((x1 / 10 + cx) / scale), int((y1 / 10 + cy) / scale)),
                     (int((x2 / 10 + cx) / scale), int((y2 / 10 + cy) / scale)),
                     255, th_px)
        tot = max(1, (m > 0).sum())
        miss = (m > 0) & (cov == 0)
        n, lb, sd, _ = cv2.connectedComponentsWithStats(miss.astype(np.uint8), 8)
        gaps = [sd[j, cv2.CC_STAT_AREA] * scale * scale for j in range(1, n)]
        gaps = [g for g in gaps if g > 0.25]
        per_layer.append({
            'name': L['name'], 'hex': L['hex'],
            'coverage': round(100 * ((cov > 0) & (m > 0)).sum() / tot, 1),
            'gaps': len(gaps),
            'worst_gap_mm2': round(max(gaps), 2) if gaps else 0.0,
        })

    n_st = sum(1 for q in st if (q[2] & 0xFF) == pystitch.STITCH)
    return {
        'width_mm': round(w, 2), 'height_mm': round(h, 2),
        'width_in': round(w / 25.4, 3),
        'reach_mm': round(reach, 2),
        'fits_hoop': w <= p.hoop_w_mm and h <= p.hoop_h_mm,
        'hoop_clearance_mm': round(min(p.hoop_w_mm - w, p.hoop_h_mm - h) / 2, 2),
        'stitches': n_st,
        'colour_changes': pat.count_color_changes(),
        'travels': len(events),
        'trimmed': sum(1 for e in events if e[1]),
        'floats': sum(1 for e in events if not e[1]),
        'longest_travel_mm': round(max([e[0] for e in events], default=0.0), 1),
        'min_stitch_mm': round(min(lens), 2) if lens else 0.0,
        'max_stitch_mm': round(max(lens), 2) if lens else 0.0,
        'runtime_min': round(n_st / 700.0, 1),
        'layers': per_layer,
        'coverage_min': min([l['coverage'] for l in per_layer], default=0.0),
        'worst_gap_mm2': max([l['worst_gap_mm2'] for l in per_layer], default=0.0),
    }


# -------------------------------------------------------------- auto-tune
def digitize(layers, p: Params, max_passes=4, log=None):
    """Build, inspect, adjust, rebuild.

    Two things get corrected automatically: the canvas scale, so the stitched
    size lands on the requested width rather than near it, and the settings,
    when the QA pass finds thin coverage or over-long stitches.
    """
    log = log if log is not None else []
    width_px = layers[0]['mask'].shape[1]
    canvas = p.target_width_mm * 1.02          # first guess; art is inset
    best = None
    tuned_satin = tuned_gap = False
    for i in range(max_passes):
        pat, stats = build_pattern(layers, canvas, width_px, p)
        rep = qa_report(pat, layers, canvas, width_px, p)
        note = []

        # 1. size calibration
        err = rep['width_mm'] - p.target_width_mm
        if abs(err) > 0.15 and i < max_passes - 1:
            canvas *= p.target_width_mm / max(rep['width_mm'], 1e-6)
            note.append('width %.2f mm vs %.2f target -> rescaling canvas'
                        % (rep['width_mm'], p.target_width_mm))

        # 2. stitches too long for safety. Satin stitches run a little longer
        # than the cap because each one crosses on a diagonal, so only react to
        # a real overshoot, and only once — ratcheting the cap down pushes
        # shapes out of satin and into fill, which is worse.
        if rep['max_stitch_mm'] > p.max_satin + 1.2 and not tuned_satin:
            p.max_satin = max(4.0, p.max_satin - 0.5)
            tuned_satin = True
            note.append('longest stitch %.2f mm -> satin cap down to %.1f mm'
                        % (rep['max_stitch_mm'], p.max_satin))

        # 3. thin coverage or holes
        if rep['coverage_min'] < 96.5 and p.row_spacing > 0.30:
            p.row_spacing = round(max(0.30, p.row_spacing - 0.025), 3)
            p.satin_spacing = round(p.row_spacing / 2, 4)
            note.append('coverage %.1f%% -> density tightened to %.3f mm'
                        % (rep['coverage_min'], p.row_spacing))
        # only chase genuinely visible holes; dropping the floor further just
        # fragments the design into tiny patches, each needing its own travel
        if rep['worst_gap_mm2'] > 2.0 and not tuned_gap:
            p.min_fill_area = round(max(0.08, p.min_fill_area / 2), 3)
            tuned_gap = True
            note.append('gap of %.2f mm2 -> fill floor down to %.2f mm2'
                        % (rep['worst_gap_mm2'], p.min_fill_area))

        log.append({'pass': i + 1, 'report': rep, 'actions': note})
        best = (pat, rep, stats)
        if not note:
            break
    return best[0], best[1], best[2], log
