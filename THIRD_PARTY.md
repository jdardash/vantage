# Third-party code and data

This project is MIT licensed (see `LICENSE`). Where another project already solved a
problem, its code or design was adapted instead of being rewritten, and every such
piece is listed here with the place it came from. The rule that governs what may be
taken:

- MIT, BSD and Apache-2.0 sources may be adapted into this codebase with their notice.
- AGPL and GPL projects (keeptrack.space, worldmonitor, Shadowbroker) are read for
  ideas only. No code from them is in this repository.
- Projects with no license file (ThingsInSpace, OrbitVoyage, shawn14/worldview) are
  all rights reserved. They are read for comparison only.
- NASA CARA is under the NASA Open Source Agreement 1.3. Its test fixtures are vendored
  verbatim with their notice; none of its MATLAB code is translated here.

The donor checkouts live under `reference/` (not committed; `reference/README.md` has
the inventory and the commit each was read at). "Commit read" below is the donor commit
the adaptation was made from.

## Adapted code

| Our file | Donor | Donor path | Commit read | License | What was taken |
| --- | --- | --- | --- | --- | --- |
| `src/sda/templates/static/js/styles.js` | [bilawalsidhu/gods-eye-view](https://github.com/bilawalsidhu/gods-eye-view) | `src/styles/` | 65bc522 | MIT | Shaders and the stage-registry design for the CRT, night vision, FLIR, noir and snow styles, rewritten to the GLSL subset that runs under both ES 1.00 and 3.00 |
| `src/sda/templates/static/js/tactical.js` | bilawalsidhu/gods-eye-view | trail renderer, label arbiter, tracked camera | 65bc522 | MIT | The design of the fading trails, the screen-space label arbiter and the tracked-camera poses |
| `src/sda/ratelimit.py` | bilawalsidhu/gods-eye-view | server rate limiter | 65bc522 | MIT | Per-IP keying, the refusal to trust client forwarding headers by default, and the bounded key map. The token bucket and the FastAPI integration are ours |
| `src/sda/agent.py` (`_view_band`, `_region_label`, `_tool_summarize_scene`) | bilawalsidhu/gods-eye-view | agent HUD summary | 65bc522 | MIT | Camera standoff bands, coarse region names and the rolling scene summary handed to the model |
| `src/sda/templates/static/js/sgp4-worker.js` | [Flowm/satvis](https://github.com/Flowm/satvis) | `src/modules/util/sgp4Worker.ts` | 06a74fe | MIT | Worker design: a satrec cache as the only state, each request a pure function of its timestamp, one packed typed array per reply, transferable buffers handed back |
| `src/sda/templates/static/sw.js` | Flowm/satvis | PWA configuration | 06a74fe | MIT | The precache-the-shell approach for the static demo |
| `src/sda/passes.py` (`compass_point`, pass quality) | Flowm/satvis | `PassPredictor` | 06a74fe | MIT | The 16-point compass mapping and the 0 to 1 pass quality score |
| `src/sda/passes.py` (`predict_passes`) | [PremaanshVyas/satlas](https://github.com/PremaanshVyas/satlas) | `predict_passes` | f170fef | MIT | The rise, culminate, set structure of a pass. The search itself is ours |
| `src/sda/screening.py` | [cesabici-bit/satguard](https://github.com/cesabici-bit/satguard) | screening module | 07a86ce | MIT | The apogee and perigee shell prefilter and the idea of a per-step spatial index over vectorized SGP4 output, reimplemented on this project's models |
| `src/sda/conjunction.py` (vectorized screen) | cesabici-bit/satguard | vectorized screen | 07a86ce | MIT | The structure of the vectorized coarse screen |
| `src/sda/probability.py` (`regime_default_covariance_rtn`, realism scaling) | cesabici-bit/satguard | `covariance/realism.py` | 07a86ce | MIT | Regime default covariance structure, extended here with a HEO row |
| `src/sda/screening.py` (formations, composite score), `src/sda/conjunction.py` (regime survey) | [ncdrone/orbveil](https://github.com/ncdrone/orbveil) | screening and survey modules | 6fd454e | Apache-2.0 | The formation exclusion table and the composite risk score design, implemented here from the design |
| `src/sda/cdm.py` (`_parse_cdm_xml`) | ncdrone/orbveil | `data/cdm.py` | 6fd454e | Apache-2.0 | The structure of the CCSDS CDM XML reader |
| `src/sda/routes/worldview.py` (`_swpc_kp`) | [simplifaisoul/osiris](https://github.com/simplifaisoul/osiris) | `src/app/api/space-weather/route.ts` | 301e697 | MIT | The `Kp` / `kp_index` key fallback for NOAA's K-index rows |
| `src/sda/templates/static/js/isscam.js` | simplifaisoul/osiris | `src/components/SpaceCam.tsx` | 8edb376 | MIT | The three ISS camera feeds (YouTube video ids, labels, descriptions), the youtube-nocookie embed parameters, the nominal 408 km and 93 minute figures, and the idea of a source link and an enlarge control beside the player. The plain-script panel, the floating frame, the id validation, the Find the ISS button and the selection hook are ours. Feed ids answered HTTP 200 on YouTube's oEmbed endpoint on 2026-10-09 |
| `src/sda/templates/static/js/categories.js` | PremaanshVyas/satlas | `apps/web/src/globe/Globe.ts` (`classifySatellite`, `SatCategory`), `apps/web/src/components/MobileControlsSheet.tsx` | 410f923 | MIT | The name-prefix classifier rules for Starlink, GPS and Iridium, the debris markers, the Starlink / GPS / Iridium / Debris / Other categories with their labels, and the multi-select pill toggle. Added here: OneWeb, Kuiper, Qianfan/Guowang, GLONASS, Galileo, BeiDou, Stations, Weather and Geostationary (by regime), debris tested first and matched as a whole word, an All pill, counts, and the hide and show logic against the globe |
| `src/sda/templates/static/js/conjactions.js` | cesabici-bit/satguard | `web/src/components/ConjunctionBrowser.tsx`, `web/src/components/ObjectInspector.tsx` | 07a86ce | MIT | The Pc presentation rules only: bands at strictly above 1e-4 and above 1e-6, Pc as `toExponential` with N/A for a missing value, the truncate helper and the compact badge-plus-numbers row, restyled in this page's risk colours. The request builders, the panel, the fragment cloud and its lifecycle are ours |
| `src/sda/templates/static/js/satsearch.js` (`noradToInt`, `noradKey`) | PremaanshVyas/satlas | `apps/web/src/lib/norad.ts` | 410f923 | MIT | The alpha-5 alphabet and the decode and key functions for NORAD ids above 99,999, made stricter here (an upper-case letter and exactly four digits) |
| `src/sda/cameras.py`, `src/sda/templates/static/js/cameras.js` | [GoSlowPoke168/Argus](https://github.com/GoSlowPoke168/Argus) | `public/cameras.geojson`, the map's camera layer | 7a56f8d | MIT | The camera catalog itself, imported at run time by `python -m sda.cameras import` into `SDA_CAMERA_DIR` and never committed, and from its map the density view that hands over to single cameras, the random-camera action and the feed preference order. The SQLite store, the ethics filters (scanned devices, credential URLs, home dynamic-DNS hosts, link-only sources), the snapshot proxy and the viewer are ours |
| `src/sda/templates/static/js/aurora.js` | [Kajjan7610/Gods-view-sweden](https://github.com/Kajjan7610/Gods-view-sweden) | `src/layers/aurora/records.js` (`auroraColor`), `src/layers/aurora/index.js` | 981aef2 | MIT | The green, yellow, red colour stops, the alpha that rises with the chance, the 110 km band height and the idea of painting the oval into a texture. The per-hemisphere shell, the wrapped blur, the route and the panel are ours |
| `src/sda/templates/static/js/skyview.js` | Flowm/satvis | SkyView mode | d7fe933 | MIT | The camera handling for looking up from the ground: a level basis that stays defined at the zenith, both screen-space controller flags turned off while the view is up, the horizontal or vertical field of view by viewport shape, and refusing implausible terrain heights. Star, planet and satellite drawing, the frames and the HUD are ours |
| `src/sda/summit.py`, `src/sda/routes/summit.py`, `src/sda/templates/static/js/summit.js` | PeakWatch, in the owner's own trip-runbook repository (`trips/Sierra-Summit-2026`) | `peakwatch` scoring, `peakwatch.toml` limits, the wind and summit odds JSON | read 2026-11-19 | MIT, same owner | The GO / MARG / NO day rules (model median and single-model outliers, chill, snow, precipitation, cloud hours, CAPE, 72 h storm snow, melt-freeze, alerts, avalanche, fire, AQI), the White Mountain ensemble table and the California and Nevada odds calibrations, kept inside a CA/NV polygon. Any-height pressure-level interpolation, the worldwide source selection, batching and caching are new |
| `src/sda/templates/static/js/peaks.js` | PeakWatch, in the owner's own trip-runbook repository (`trips/Sierra-Summit-2026`) | the Sierra summit map | read 2026-11-19 | MIT, same owner | Marker shapes, the GO / MARG / NO colours and the label declutter idea, rewritten for twelve million summits served by viewport from SQLite |
| `src/sda/peaks.py` | [akirmse/mountains](https://github.com/akirmse/mountains) | the prominence and isolation output formats | read 2026-11-19 | MIT | File layouts only (column order, units, the feet-to-metres isolation file); no code copied |
| `src/sda/templates/static/js/weather.js` | [cambecc/earth](https://github.com/cambecc/earth) | `public/libs/earth/1.0.0/earth.js` `animate()` | 5f091f0c3b60 | MIT, Copyright (c) 2014 Cameron Beccario | Particle ages and re-seeding, colour buckets drawn as one path each, and the 0.97 trail fade per 40 ms (made time-based); rewritten, not pasted |
| `src/sda/templates/static/js/weather.js`, `src/sda/routes/weather.py` | [bilawalsidhu/gods-eye-view](https://github.com/bilawalsidhu/gods-eye-view) | `src/layers/wind/model.js`, `server/providers/cyclones.js` | 9423a26487de | MIT, Copyright (c) 2026 Bilawal Sidhu | Bilinear wind sampling that wraps at the antimeridian and the speed colour ramp; the NHC rules (storm id pattern, mph to knots, map layers 5/6/7, geometry matched to storms), rewritten in Python |
| `src/sda/templates/static/js/maptools.js` | [bilawalsidhu/gods-eye-view](https://github.com/bilawalsidhu/gods-eye-view) | `src/search/coordinateParser.js`, `src/search/gridReference.js`, `src/search/plusCode.js`, `src/data/geoFileImport.js`, `src/annotations/drawTool.js` | 9423a26 | MIT, Copyright (c) 2026 Bilawal Sidhu | The coordinate grammar, inverse transverse Mercator with MGRS band and Norway/Svalbard rules, the plus code decoder, GPX to GeoJSON mapping and double-click to finish. Vincenty, the forward transverse Mercator series, KML handling, the sun position and line of sight are ours, checked against GeographicLib 2.1 and PROJ 9 |
| `src/sda/satcat.py` | [ISHANTARE/ASTRA](https://github.com/ISHANTARE/ASTRA) | `astra/spacebook.py` | 74123de | MIT | The Spacebook endpoint paths, the `noradId` / `id` field names, cache age by file and the stale-cache fallback. The streaming parser, compact records, backoff and the Space-Track fallback are ours |
| `src/sda/satcat.py`, `src/sda/templates/static/js/dossier.js` | [PremaanshVyas/satlas](https://github.com/PremaanshVyas/satlas) | `apps/web/src/lib/satcat.ts` | 410f923 | MIT | The fact-card field set, the 24 h cache rule, status from the decay date, a minimum record count before a reply replaces the cache, and owner and site codes mapped to names (tables corrected and extended) |
| `src/sda/satcat.py`, `src/sda/templates/static/js/dossier.js` | keeptrack.space | `sat-info-box-object/object-summary.ts`, `app/data/catalogs/countries.ts` | db3b64e | AGPL-3.0, ideas only | The plain-words summary line and a check of our launch-site names; no code taken |

Copyright holders, from each donor's `LICENSE`: gods-eye-view, Copyright (c) 2026 Bilawal
Sidhu; Argus, Copyright (c) 2026 Jeremy Hou; Gods-view-sweden, Copyright (c) 2026 Bilawal Sidhu
and Copyright (c) 2026 Kaj Obst; satvis, Copyright (c) 2018 Florian Mauracher; satlas, Copyright (c) 2026 Premaansh
Vyas; satguard, Copyright (c) 2026 Genius Lab; osiris, Copyright (c) 2026 simplifaisoul;
orbveil, Apache License 2.0 (no separate copyright line in its `LICENSE`).

### MIT permission notice

The MIT donors above (gods-eye-view, satvis, satlas, satguard, osiris, Argus, Gods-view-sweden) each ship this
text under their own copyright line, quoted in the paragraph before this one. It is
reproduced once here and applies to the portions taken from each of them.

```text
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

orbveil is under the Apache License 2.0 (<https://www.apache.org/licenses/LICENSE-2.0>).
What was taken from it was a design, implemented here in new code, and its repository
carries no `NOTICE` file.

d3-celestial (the star and constellation data in `static/data/sky.json`) is under the BSD
3-Clause license; its notice, as the license requires:

```text
Copyright (c) 2015, Olaf Frohn
All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are
permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of
   conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice, this list of
   conditions and the following disclaimer in the documentation and/or other materials
   provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors may be used
   to endorse or promote products derived from this software without specific prior
   written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS
OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR
TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Vendored data

| Path | Source | License | Notes |
| --- | --- | --- | --- |
| `tests/data/cara/` | [nasa/CARA_Analysis_Tools](https://github.com/nasa/CARA_Analysis_Tools) | NASA Open Source Agreement 1.3 | 53 reference CDMs, the `PcMethodTestConjunctions` values and the Alfano 2009 cases, verbatim, with `NOTICE.md` and one agreement PDF alongside. Imported by `scripts/import_cara_fixtures.py` |
| `src/sda/templates/static/data/sky.json` | [ofrohn/d3-celestial](https://github.com/ofrohn/d3-celestial) `data/` at 7e720a3 | BSD-3-Clause (notice above) | Hipparcos stars to magnitude 5.5 (2,851), proper names to magnitude 3 (156), the IAU constellation lines (150 strips) and label positions (89), rounded and re-packed by `scripts/build_sky_data.py`; the file carries its source and license fields |

## Runtime services and libraries loaded by the page

These are fetched by the browser or proxied by the server at run time; none is bundled.

| What | Where from | Terms |
| --- | --- | --- |
| CesiumJS 1.119 | jsDelivr | Apache-2.0 |
| satellite.js 5.0.0 | jsDelivr | MIT |
| Blue Marble and Black Marble imagery | NASA GIBS | Public, credit to NASA |
| World Imagery, World Street Map, World Topographic Map, Dark Gray Canvas, and the Boundaries and Places and Transportation reference overlays | Esri ArcGIS Online, keyless tile services | Esri terms, attribution shown under the basemap buttons |
| Dated satellite imagery (MODIS Terra and Aqua, VIIRS NOAA-20 and SNPP, Day/Night Band) | NASA GIBS WMTS | Public, credit to NASA |
| Natural events (wildfires, storms, volcanoes, ice) | NASA EONET v3, through `GET /api/eonet` | Public domain |
| Summit forecasts: pressure-level wind and temperature, ECMWF ensemble, air quality, elevation | Open-Meteo (forecast, ensemble, air-quality and elevation APIs), through `/api/summit/*` | Free for non-commercial use, CC BY 4.0 data; requests batched 20 points at a time and cached per model run |
| Weather radar (past two hours, 13 frames) | RainViewer, read by the browser directly | Free API for personal and educational use; the client stays under 100 requests per IP per minute and stops at zoom 7 |
| Global 10 m wind grid | Open-Meteo forecast API, through `GET /api/weather/wind` | Free for non-commercial use, 10,000 calls a day; a 5-degree grid costs 2,232 calls and is rebuilt at most every 12 h, only while the layer is on |
| Tropical cyclones | NOAA NHC CurrentStorms and map layers, GDACS for the other basins, through `GET /api/weather/cyclones` | NHC is public domain; GDACS is a joint UN and European Commission service, free to use with credit; cached 15 minutes |
| Terrain without a key | Esri World Elevation 3D (`elevation3d.arcgis.com` Terrain3D ImageServer), used by the map tools when no Cesium ion token is set | Esri terms of use, credit shown on screen |
| Reverse geocoding for the map tools popup | OpenStreetMap Nominatim, through `GET /api/geocode/reverse` (cached, at most one request a second); the browser asks Nominatim directly only if that route is missing | ODbL data; Nominatim usage policy |
| US alerts, avalanche danger and active fires for summits in the US | NWS api.weather.gov, avalanche.org, NIFC | Public domain (NWS, NIFC); avalanche.org public map layer |
| Element sets | CelesTrak (OMM JSON), optionally Space-Track; SatNOGS DB and COMSPOC Spacebook as keyless fallbacks when CelesTrak delivers nothing for two hours | CelesTrak asks for no more than one download of a group per two-hour update window; the server enforces that when `SDA_TLE_CACHE_DIR` is set; SatNOGS DB data is CC BY-SA 4.0; Spacebook is COMSPOC's free public service, each fallback asked at most once per two hours |
| Object dossier SATCAT | COMSPOC Spacebook (satcat and details), Space-Track satcat as a fallback when credentials are set, GCAT by Jonathan McDowell for manufacturer, bus and operator | Downloaded at most once a day and cached under `SDA_SATCAT_DIR`; Space-Track asks for one satcat query a day after 1700 UTC; GCAT is CC BY 4.0; the card credits "COMSPOC Spacebook; Space-Track; GCAT (J. McDowell)" |
| Space weather (45-day Ap and F10.7 forecast, planetary Kp, NOAA G/S/R scales, F10.7 flux) | NOAA SWPC, through `GET /api/space-weather/summary` and `/api/space-weather/conjunction`, plus the top-bar Kp strip | Public domain |
| World summits | Andrew Kirmse and Jonathan de Ferranti, 2023 prominence list from Copernicus GLO-30 (11,866,713 summits at 100 m prominence or more), and Kirmse's 2017 isolation list; downloaded by `python -m sda.peaks import` onto the server, never bundled | No data licence stated by the authors, credited in the panel and never redistributed; the Copernicus GLO-30 credit (DLR e.V. 2010-2014, Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA) is shown with it, from `sda.peaks.ATTRIBUTION` |
| Summit names, surveyed heights and regions | GeoNames `allCountries` and `admin1CodesASCII`, downloaded by the same import | CC BY 4.0, credited in the panel |
| Aurora oval (OVATION Prime) | NOAA SWPC, through `GET /api/aurora` | Public domain |
| astronomy-engine 2.1.19 | jsDelivr, pinned with SRI, loaded only when the sky view opens | MIT, Copyright (c) 2019-2023 Don Cross |
| hls.js 1.7.4 | jsDelivr, loaded only when an HLS camera plays | Apache-2.0 |
| Traffic and weather camera feeds | the operating agencies, through the Argus catalog | Each agency's terms. Sources whose terms forbid reuse open as a link to the agency page and are never played or proxied here |
