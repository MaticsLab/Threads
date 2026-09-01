# StitchForge

Upload an image — or type text — and get a machine-ready embroidery file.
The original StitchForge digitizing pipeline (colour separation → satin
columns + tatami fills → QA + auto-tune) wrapped in a web UI, with the
Ink/Stitch family of projects integrated end to end.

![example](docs_example.png)

## Run it

```bash
./run.sh            # then open http://localhost:8000
```

Or manually:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Tests: `pytest`.

## Deploy

### Railway

The repo ships a `Dockerfile` and `railway.json`, so it deploys as a single
web service with no extra configuration:

1. Railway dashboard → **New Project → Deploy from GitHub repo** → pick this
   repo (or `railway init && railway up` with the CLI).
2. Railway builds the Dockerfile, injects `PORT`, and health-checks `/`.
3. **Settings → Networking → Generate Domain** to make it public.

If the service was created with Railway's Railpack/Nixpacks builder instead
of the Dockerfile, it still boots: the `Procfile` and the `main.py` shim
cover both `uvicorn app:app` and Railpack's `uvicorn main:app` default. If a
deploy loops with "Could not import module", clear any custom **Start
Command** in the service settings (or set it to
`uvicorn app:app --host 0.0.0.0 --port $PORT`) and make sure the service
deploys the `main` branch.

Keep it at one replica/worker: digitized patterns are held in process memory
between the digitize call and the export/worksheet calls, and jobs live on
the container's ephemeral disk (they don't survive redeploys — by design,
there's no persistence layer).

### Cloudflare

Two options, in order of effort:

**1. Cloudflare in front of Railway (recommended, no code changes).**
The app sends `Cache-Control` on static assets and per-job artifacts and
gzips its big payloads, so Cloudflare's CDN caches them at the edge:

1. Add your domain to Cloudflare (free plan is fine).
2. Railway service → **Settings → Networking → Custom Domain** — e.g.
   `stitch.yourdomain.com`; Railway shows a CNAME target.
3. In Cloudflare DNS, create that CNAME with the proxy (orange cloud) **on**,
   and set SSL/TLS mode to **Full (strict)**.

Static files, stitch plans, density maps and stitch JSON are then served
from Cloudflare's edge after first hit.

**2. Full migration to Cloudflare Containers** (Workers Paid plan): the
`cloudflare/` directory has a ready `wrangler.jsonc` + Worker. Static files
are served from Cloudflare's edge (Workers assets); everything else is
routed to one container instance built from the repo's Dockerfile.

With the GitHub repo connected to Cloudflare (Workers Builds), create the
Worker from the repo and every push to `main` deploys automatically:

1. Dashboard → **Workers & Pages → Create → Import a repository** → pick
   this repo.
2. Build settings: **root directory** `cloudflare`,
   **build command** `npm install`,
   **deploy command** `npx wrangler deploy`.
3. First deploy builds the Docker image on Cloudflare's builders and gives
   you `stitchforge.<account>.workers.dev`; add a custom domain on the
   Worker if you want one.

Manual deploys work too (needs Docker locally):

```bash
cd cloudflare && npm install && npx wrangler deploy
```

Notes for the Containers runtime:
- The container sleeps after 1h idle (`sleepAfter` in `worker.js`); jobs
  live in instance memory/tmp, so download/worksheet links from before a
  sleep return 404 — re-digitize, or raise `sleepAfter`.
- `instance_type: standard` (4 GiB / ½ vCPU) — digitizing runs a bit slower
  than on a full core; bump the instance type if Cloudflare's plan offers
  larger ones.
- Keep `max_instances: 1` — the pattern cache is in-process.

## Performance notes

- Realistic preview and the stitch player render on a `<canvas>` with
  batched `Path2D` strokes — the SVG realistic file (one lighting filter per
  stitch, exactly Ink/Stitch's) is still available at
  `/api/plan/{job}.svg?realistic=true` for export, but the browser no longer
  has to evaluate thousands of live SVG filters.
- Responses over 1 KB are gzipped; job artifacts are immutable and cached
  (`Cache-Control: public`), so a browser or CDN only fetches them once.

## What you get

- **PNG → DST** — the digitizing pipeline described below, unchanged.
- **Any machine format** — exports via pystitch: DST, PES, JEF, EXP, VP3,
  XXX, U01, PEC, TBF, plus CSV/PNG.
- **Lettering** — type text, pick one of the bundled Ink/Stitch embroidery
  fonts, get satin-stitched letters (each glyph is a hand-digitized set of
  satin columns and running stitches, sewn by the rail-and-rung engine
  ported from Ink/Stitch).
- **Worksheet PDF** — a client overview page and an operator detailed view
  in the layout of Ink/Stitch's print worksheet, including thread names
  matched from real palettes (Madeira, Isacord, Gunold, Brother) and a
  quote sheet ported from embTools.
- **SVG digitizing** — upload an SVG and it's digitized the way Ink/Stitch
  digitizes Inkscape files: fills (even-odd, holes kept), running-stitch /
  zigzag strokes, and real satin columns for paths carrying
  `inkstitch:satin_column` attributes (angle, spacing, bean repeats honoured).
- **Embroidery file import** — read any pystitch-supported machine file
  (DST, PES, JEF, HUS, VP3, …) to preview, play, re-export and print.
- **Fill methods** — tatami with fill-angle control, contour fill, and
  circular fill, per Ink/Stitch's fill family.
- **Previews** — raster preview, stitch-plan SVG (pan/zoom via
  svg.panzoom.js), a *realistic* view (every stitch drawn as a lit thread
  capsule whose sheen follows its angle to the light), a full **3D view**
  (three.js: instanced thread capsules on a woven fabric plane, orbit/zoom),
  a stitch **density map** (green/yellow/red per penetration), and a stitch
  player that sews the design on screen.
- **Thread brands** — all 75 Ink/Stitch brand palettes ship built in, plus
  a **BAI Matte** palette built from the product colour card (each colour
  keeps its Pantone TCX reference). Add your own brand's colour card in the
  UI (＋ Brand) or via `POST /api/palettes`; matching, worksheets and
  previews use it like any built-in. The *true thread colours* toggle
  recolours the realistic/3D/player views with the matched brand threads.
- **Design panel** — a floating, draggable panel (like the Matics builder's):
  click any thread on the canvas to select its colour block, pick a swatch
  from any brand, and *Save changes* persists the recolour into the exports,
  preview, stitch plan and worksheet (`POST /api/recolor/{job}`). Save
  colourways as named **themes** and apply them to any design
  (`/api/themes`).
- **Canvas workspace** — the whole right side is the canvas: a floating
  toolbox at the bottom switches views and opens Details / Export / Design
  as draggable popouts. Trackpad-native navigation: two-finger scroll pans,
  pinch or ⌘-scroll zooms at the cursor, double-click refits.
- **Business tools (embTools)** — client & vendor database, notes / quote
  log / to-do panes, quote sheet on the worksheet, run-time calculator and
  the full unit-conversion set (mm⇄in, cm⇄in, px⇄mm, pt⇄in).
- **Batch export** — one ZIP with every major format, a thread list, the
  stitch plan SVG and the worksheet PDF.

## Integrated projects

| Repo | How it's used here |
|---|---|
| [inkstitch/pystitch](https://github.com/inkstitch/pystitch) | Vendored at `third_party/pystitch` (MIT). All embroidery file I/O — DST writing and every export format. |
| [inkstitch/inkstitch](https://github.com/inkstitch/inkstitch) | Ported (GPL-3.0): realistic stitch rendering (`inkstitchlib/stitch_svg.py`, from `lib/svg/rendering.py`), the print worksheet as a PDF (`inkstitchlib/worksheet.py`, after `print/templates/`), satin rail/rung detection and glyph layout (`inkstitchlib/lettering.py`, from `lib/elements/satin_column.py` and `lib/lettering/`), thread palettes (`palettes/*.gpl`). |
| [inkstitch/embroidery-fonts](https://github.com/inkstitch/embroidery-fonts) | Seven fonts vendored under `fonts/` (each keeps its own licence file): Amitaclo, Emilio 20, Emilio 20 Simple, Geneva Simple Sans, Magnolia KOR, Pacificlo, Sacramarif. Add more by dropping a font directory (`font.json` + `ltr.svg`) into `fonts/`. |
| [inkstitch/embTools-1](https://github.com/inkstitch/embTools-1) | Feature port of the Qt app (GPL-3.0): the quote sheet calculation (`quotesheet.cpp` → `inkstitchlib/worksheet.quote`), the stitch player (UI ▶ Player mode), unit conversion (mm ⇄ inch widget). |
| [inkstitch/svg.panzoom.js](https://github.com/inkstitch/svg.panzoom.js) | Vendored at `static/vendor/svg.panzoom.js` (MIT, adapted to the global svg.js build) — pan/zoom for the stitch plan, realistic preview and player. svg.js itself is vendored alongside. |

## The digitizing pipeline

**1 · Colour separation** (`digitizer/segment.py`)
Alpha channel if present, otherwise the background colour is detected from
the image border and flooded out. The remaining pixels are k-means clustered
into the requested number of thread colours. Layers are ordered lightest to
darkest so darker outline colours sew last and cover the seams.

**2 · Stitch generation** (`digitizer/core.py`, `engine.py`)
For each colour layer, each connected shape is handled on its own:

- The shape's skeleton is extracted and pruned — terminal forks shorter than
  ~1.5× the local stroke width are artefacts, and satin-ing them makes a fan
  of crossed stitches.
- Each surviving branch becomes a satin column. The crossing angle is solved
  per stitch by rotating through ±40° and taking the *shortest* chord, so
  stitches cut the stroke on its true perpendicular instead of a diagonal.
- Spacing is paced off the **outer** edge of each curve; pacing off the
  centreline is what opens gaps on the outside of a bend.
- Any stroke wider than the satin cap breaks the column; those regions, plus
  anything the columns don't cover, get a tatami fill in the same thread,
  sewn first so the satin sits on top.
- Underlay: centre run + zigzag under every satin column; edge walk +
  cross-hatch under every fill.
- Travel is routed inside the shape where a straight line fits; remaining
  travels over the trim distance are cut by the machine.

**3 · Inspection and auto-tune** (`engine.qa_report`, `engine.digitize`)
The finished pattern is rasterised at true thread width and compared back
against the source masks: per-colour coverage, gap sizes, stitch length
range, travel count, finished size, hoop clearance. Then it acts:

| Finding | Action |
|---|---|
| Stitched width off target | rescale the canvas and rebuild |
| Longest stitch over the satin cap | lower the cap |
| Coverage under 96.5% | tighten density by 0.025 mm (floor 0.30) |
| Gap over 1.0 mm² | halve the minimum fill area so corners get stitched |

It loops up to four passes and stops as soon as a pass makes no changes.

## API

```
POST /api/analyze                image + colour count -> layers
POST /api/digitize               digitize an analyzed upload (fill_method, fill_angle, ...)
POST /api/lettering              text + font -> stitched lettering
POST /api/import                 SVG (digitized) or any machine embroidery file
GET  /api/fonts                  bundled fonts (+ /api/fonts/{id}/preview.png)
GET  /api/palettes               thread palettes [{name, custom}]
GET  /api/palettes/{name}        a palette's colours
POST /api/palettes               add a brand {name, threads:[{name,number,hex}]}
DELETE /api/palettes/{name}      remove a custom brand
GET  /api/match/{job}?palette=   re-match a job's colours to another brand
POST /api/recolor/{job}          persist new thread colours everywhere
GET/POST/DELETE /api/themes[/{id}]   saved colourways (design themes)
GET  /api/download/{job}?fmt=    dst pes jef exp vp3 xxx u01 pec tbf csv json txt gcode png
                                 or zip (all formats + threadlist + worksheet + plan)
GET  /api/plan/{job}.svg         stitch plan SVG (?realistic=true for the lit preview)
GET  /api/stitches/{job}         stitch blocks JSON (drives the player + canvas views)
GET  /api/density/{job}.png      stitch density map
GET  /api/threadlist/{job}.txt   thread list export
GET  /api/worksheet/{job}.pdf    print worksheet (client, quote params: setup,
                                 price_per_1000, garment_qty, garment_base, markup_pct,
                                 discount_pct)
GET/POST/PUT/DELETE /api/business/{client|vendor}[/{id}]   contact book (embTools)
GET/PUT  /api/notes/{notes|quotes|todo}                    persisted notes (embTools)
```

## Layout

```
app.py                    FastAPI app
digitizer/                image -> colour layers -> stitches (the original core)
inkstitchlib/             everything ported from the Ink/Stitch family
  stitch_svg.py             stitch plan + realistic SVG rendering
  worksheet.py              print worksheet PDF + embTools quote sheet
  lettering.py              embroidery-fonts lettering engine
  threads.py                thread palette matching
fonts/                    vendored Ink/Stitch fonts (per-font licences inside)
palettes/                 vendored Ink/Stitch thread palettes (.gpl)
third_party/pystitch/     vendored pystitch (MIT)
static/                   UI; vendor/ holds svg.js + svg.panzoom.js
tests/                    end-to-end API tests
```

## Limits worth knowing

- Photographs won't digitize well — the artwork path wants flat-colour,
  vector-style art. Detail under about 1 mm can't be stitched at any density.
- DST carries no colour information; the thread list in the UI/worksheet is
  the sequence you follow at the machine.
- Each Ink/Stitch font only scales within its designed range (shown next to
  the font picker); the engine clamps to it.
- Jobs are written to a temp directory keyed by id, with no cleanup or auth.
  Add both before putting this anywhere public.

## Licence

GPL-3.0 (see `LICENSE`) — required by the code ported from Ink/Stitch and
embTools. Vendored components keep their own licences in their directories:
pystitch (MIT), svg.js / svg.panzoom.js (MIT), each font under `fonts/`
(SIL OFL or similar, see each `LICENSE`).
