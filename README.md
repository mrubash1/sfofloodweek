# SFO flood week

Static page served at https://sfofloodweek.com: hour by hour bay level at SFO, Pier 14 and Mission Creek for the Thanksgiving week 2026 king tides and storm, with a ground-height map.

Unofficial estimate, not a flood warning.

- `index.html` is the built, self-contained page that Vercel serves.
- `src/page.html` is the template; `src/*.js` hold the embedded map and elevation data.
- `python3 build.py` rebuilds `index.html` from `src/`.

Data: NOAA CO-OPS tide predictions and datums, NWS forecast, USGS 3DEP lidar, US Census TIGER, OpenStreetMap, FAA.

Credit: a remix of the Oakland Flood Week map shared on Reddit, https://claude.ai/artifact/7udMTTZs1kN4kNBM3pfFgo
