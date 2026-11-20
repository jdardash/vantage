# Reference checkouts

Third-party projects cloned here for capability comparison against this
repository. The clones themselves are ignored by git (`reference/*/` in
`.gitignore`); only this file and the comparison notes are tracked. Every clone
is `--depth 1`, so the pinned commit below is the only history present.

Re-fetch any of them with:

```bash
cd reference && git clone --depth 1 https://github.com/<owner>/<repo>.git <dir>
```

Comparison results: [COMPARISON.md](COMPARISON.md).

## Inventory (cloned 2026-09-02; the five MIT clones we take code from were pulled 2026-10-09)

| Dir | Repo | Pinned | Stars | License | Stack | Why it is here |
| --- | --- | --- | ---: | --- | --- | --- |
| `gods-eye-view/` | [bilawalsidhu/gods-eye-view](https://github.com/bilawalsidhu/gods-eye-view) | 95fa816 (2026-10-07) | 48.9k | MIT | Vite + CesiumJS, vanilla JS | The OSINT globe this project's WorldView was modelled on. Primary comparison target. |
| `keeptrack.space/` | [thkruz/keeptrack.space](https://github.com/thkruz/keeptrack.space) | 15a14f1 (2026-08-08) | 1.6k | AGPL-3.0 | TypeScript, WebGL | Most mature open-source space situational awareness app: sensors, lookangles, breakups, close-approach screening. |
| `worldmonitor/` | [koala73/worldmonitor](https://github.com/koala73/worldmonitor) | ec335cd (2026-09-02) | 85k | AGPL-3.0 | TypeScript, Tauri | The "open-source Palantir" intelligence dashboard. Breadth-of-layers benchmark. |
| `Shadowbroker/` | [BigBodyCobain/Shadowbroker](https://github.com/BigBodyCobain/Shadowbroker) | cd6395f (2026-08-23) | 11k | AGPL-3.0 | Python | OSINT for jets, spy satellites, seismic and more. |
| `osiris/` | [simplifaisoul/osiris](https://github.com/simplifaisoul/osiris) | 8edb376 (2026-10-08) | 10.6k | MIT | Next.js, MapLibre | "Palantir alternative" OSINT dashboard; the parent of the many osiris forks. |
| `ThingsInSpace/` | [jeyoder/ThingsInSpace](https://github.com/jeyoder/ThingsInSpace) | 4f56a32 (2019-12-15) | 834 | none | JavaScript, WebGL | stuffin.space, the original real-time full-catalog globe. Archived-quality baseline. |
| `satvis/` | [Flowm/satvis](https://github.com/Flowm/satvis) | 1a63cc0 (2026-10-08, branch `main`; `next` is gone upstream) | 409 | MIT | TypeScript, Vue, CesiumJS | 12,000+ satellite tracker and pass predictor on Cesium; closest rendering peer. |
| `CARA_Analysis_Tools/` | [nasa/CARA_Analysis_Tools](https://github.com/nasa/CARA_Analysis_Tools) | 86ba28a (2026-08-06) | 110 | NASA OSA (see repo) | MATLAB | NASA Conjunction Assessment Risk Analysis Pc reference implementations and test vectors. |
| `spacetech-ssa/` | [IBM/spacetech-ssa](https://github.com/IBM/spacetech-ssa) | 26986e5 (2021-02-21) | 122 | Apache-2.0 | Python | IBM ML-augmented orbit prediction for SSA. |
| `satlas/` | [PremaanshVyas/satlas](https://github.com/PremaanshVyas/satlas) | 410f923 (2026-09-10) | 31 | MIT | TypeScript | Real-time SSA with an AI agent front door. |
| `satguard/` | [cesabici-bit/satguard](https://github.com/cesabici-bit/satguard) | 07a86ce (2026-03-23) | 4 | MIT | Python, CesiumJS | Direct competitor: TLE ingest, SGP4, Foster/Chan/Alfano Pc, Cesium globe. |
| `orbveil/` | [ncdrone/orbveil](https://github.com/ncdrone/orbveil) | 6fd454e (2026-02-11) | 1 | Apache-2.0 | Python | Direct competitor: full-catalog conjunction screening engine. |
| `OrbitVoyage/` | [sparshsharmadev/OrbitVoyage](https://github.com/sparshsharmadev/OrbitVoyage) | a076f0e (2026-05-12) | 2 | none | TypeScript | Space traffic control dashboard with conjunction prediction. |
| `worldview/` | [shawn14/worldview](https://github.com/shawn14/worldview) | 9a48c4e (2026-03-02) | 3 | none | TypeScript, CesiumJS | Independent gods-eye-view style clone (Google 3D tiles, aircraft, satellites). |

Repos without a LICENSE file (ThingsInSpace, OrbitVoyage, worldview) are
all-rights-reserved by default: read for comparison only, do not copy code.
AGPL-3.0 repos (keeptrack.space, worldmonitor, Shadowbroker) cannot be mixed
into this MIT codebase without relicensing; treat them as design references.

Searches that produced this list (GitHub, 2026-09-02): "spy satellite",
"space situational awareness", "satellite conjunction", "keeptrack",
"worldmonitor", "satellite tracking cesium globe", "OSIRIS palantir".
Candidates seen and deliberately skipped: `JamesDavid/Overhead` and
`Stugre/crowpanel-800x480-radar-satellites` (ESP32 hardware dashboards),
`shashwatak/satellite-js` (a library this project already uses, not an app),
`ARCLab-MIT/MOCAT-SSEM` (population evolution model, not conjunction
assessment), the osiris forks (all point back to `simplifaisoul/osiris`).

## Added in the 2026-11-19 resweep

Pinned at the commit date shown. `OrbitVoyage` (above) now answers 404 on GitHub; the local
clone at a076f0e is the only copy left.

| Dir | Repo | Pinned | License | Why it is here |
| --- | --- | --- | --- | --- |
| `argus/` | [GoSlowPoke168/Argus](https://github.com/GoSlowPoke168/Argus) | 7a56f8d (2026-08-27) | MIT (Jeremy Hou) | The "cameras on every street" globe: about 229k public traffic and weather cameras in one GeoJSON. Source of the camera catalog (`src/sda/cameras.py`) |
| `OpenTrafficCamMap/` | [AidanWelch/OpenTrafficCamMap](https://github.com/AidanWelch/OpenTrafficCamMap) | 0c9788d (2026-10-29) | MIT | Crowd-kept US traffic camera list; cross-check for camera coverage |
| `gev-worldcams/` | [uhrichsam4/gods-eye-view](https://github.com/uhrichsam4/gods-eye-view) | 5804ba8 (2026-08-31) | MIT | gods-eye-view fork with worldwide webcams |
| `gods-eye-view-free/` | [electronictechshow-netizen/gods-eye-view-free](https://github.com/electronictechshow-netizen/gods-eye-view-free) | 60a89fb (2026-10-06) | MIT | gods-eye-view fork using only keyless feeds |
| `gods-view-sweden/` | [Kajjan7610/Gods-view-sweden](https://github.com/Kajjan7610/Gods-view-sweden) | 981aef2 (2026-11-19) | MIT (Bilawal Sidhu, Kaj Obst) | Swedish fork with an aurora layer; source of the aurora colours and height (`aurora.js`) |
| `godseye/` | [VrushankPatel/godseye](https://github.com/VrushankPatel/godseye) | eb4b8db (2026-09-13) | Apache-2.0 | Java/Spring take on the same OSINT globe |
| `hermes-osint-map/` | [Adolanium/hermes-osint-map](https://github.com/Adolanium/hermes-osint-map) | e67cb7d (2026-11-17) | MIT | OSINT map with camera and event layers |
| `worldview-kevtoe/` | [kevtoe/worldview](https://github.com/kevtoe/worldview) | db607a4 (2026-08-15) | MIT | Another WorldView-style globe |
| `ground-station/` | [sgoudelis/ground-station](https://github.com/sgoudelis/ground-station) | 22b218c (2026-11-19) | GPL | Satellite ground station control (SDR, rotator, pass scheduling) |
| `SIMPLETON/` | [Salad109/SIMPLETON](https://github.com/Salad109/SIMPLETON) | bae7a05 (2026-11-17) | MIT | Conjunction screening service, a direct comparison for the screen |
| `orbtrisk-cockpit/` | [CEnnisgit/orbtrisk-cockpit](https://github.com/CEnnisgit/orbtrisk-cockpit) | 0cf566d (2026-10-22) | MIT | Orbital risk dashboard |
| `orbit-explorer/` | [ryusudol/orbit-explorer](https://github.com/ryusudol/orbit-explorer) | ca2586d (2026-10-14) | none | Orbit visualisation |
| `ssatk/` | [llnl/ssatk](https://github.com/llnl/ssatk) | 5fc690d (2026-11-11) | BSD | LLNL space situational awareness toolkit |

Gap reports from this sweep, in `notes/` (not tracked): `gap-osint-cameras.md`, `gap-sda.md`,
`gap-nasa-gov-palantir.md`.
