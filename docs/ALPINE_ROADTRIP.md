# Alpine Roadtrip — multi-map session

**Status (2026-09-18):** first hard switch Hahntennjoch ↔ Fernpass is in the repo.  
Not yet: segment-time UI, driving-time countdown, more than two gates, traffic, vehicle damage.

Related: [CONCEPT.md](CONCEPT.md) (geodata → level), [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md) (a single level).

The two test maps **do not share a geographic edge**. The boxes are logical gates (east end of Hahntennjoch ↔ south end of Fernpass / toward Imst), not a landscape seam.

The product name is **Alpine Roadtrip**. Internal id is `alpine_rt` (mod folder `autoroad_alpine_rt`, session file `settings/alpine_rt/session.json`).

---

## Current state

### Layers

- **Maps:** Python injects visible volumes + arrival spawns. The terrain pipeline stays independent.
- **Session:** GE extension `alpine_rt` with `setExtensionUnloadMode(..., "manual")` in `scripts/modScript.lua` (not `mainLevel.lua`).
- **Source of truth on switch:** `settings/alpine_rt/session.json` (user folder). In-memory: countdown / grace only.

### Files

| Path | Role |
|------|------|
| [config/alpine_rt/portals.yaml](../config/alpine_rt/portals.yaml) | Portal graph (edit this) |
| `mods/autoroad_alpine_rt/` | GE mod (Lua + cube/arc art) |
| `lua/ge/extensions/alpine_rt/portals.json` | Generated from the YAML by `build_portals.py` |
| `settings/alpine_rt/session.json` | Runtime, BeamNG user folder only |

### Persistence (today)

Snapshot before `startLevel`, restore after `onWorldReadyState == 2` (once a player vehicle exists):

- Environment: `core_environment.getState` / `setState` (time of day, clouds, fog, precipitation, wind, …)
- Weather preset: `core_weather.getCurrentWeatherPreset` + `activate` if present
- Vehicle: model + `partConfig` (same config). `startLevel` gets `{model, {config}}`; position is then set with `spawn.safeTeleport` to the arrival point, because Freeroam would otherwise overwrite `options.pos` with the default spawn
- Deliberately dropped: damage, fuel, speed, camera, traffic

Without `pending_restore` in the session file: normal Freeroam, no auto-restore.

Anti ping-pong: arrival sits ~20 m further **into the map** than the destination box; then 8 s grace with no new dwell. `startLevel` runs on the **next** `onUpdate` (not on the trigger stack).

### In-map

- Red, **very** transparent TSStatic box, `collisionType: None`, 12×8×6 m. Logic = Lua OBB.
- While the vehicle is in the box: 20 m-wide ballistic arc toward the **DGM bbox centre of the destination map** (full CRS distance, end at `center_z_m` in the current map’s Z scale). `visibleDistance` is raised for that (template otherwise 7.5 km). Later the same mesh sits in front of the backdrop panorama — [BACKDROP.md](BACKDROP.md).

| Gate | from | to | Box (BeamNG m) |
|------|------|-----|----------------|
| `hahntenn_to_fernpass` | `autoroad_m28_test` | `autoroad_fernpass_8192` | ~485, 357 (east end of M28) |
| `fernpass_to_hahntenn` | `autoroad_fernpass_8192` | `autoroad_m28_test` | ~3030, 30 (south end of B179) |

---

## Build / deploy

Quit the game, then rebuild Hahntennjoch if needed (already run once):

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/hahntennjoch.yaml"; python tools\fetch_dgm.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:PYTHONIOENCODING = "utf-8"; $env:AUTOROAD_SITE = "config/sites/hahntennjoch.yaml"; python tools\build_smoke.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:PYTHONIOENCODING = "utf-8"; python tools\setup_beamng_level.py autoroad_m28_test --site config/sites/hahntennjoch.yaml --force --title "Hahntennjoch (autoroad)"
```

**World Editor (Hahntennjoch, once after `--force`):** Freeroam → load `autoroad_m28_test` → F11 → Terrain Tools → Import Terrain → Load `/levels/autoroad_m28_test/import/terrainPreset.json` → Position 0,0,0 → Import → Save Level. Details: [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md).

Do not rebuild Fernpass Mega for a portal-only change. Then guardrails + portals + mod:

```powershell
cd C:\temp\beamng_autoroad; $env:PYTHONIOENCODING = "utf-8"; $env:AUTOROAD_SITE = "config/sites/hahntennjoch.yaml"; python tools\build_guardrails.py
```

```powershell
cd C:\temp\beamng_autoroad; python tools\build_portals.py
```

```powershell
cd C:\temp\beamng_autoroad; python tools\deploy_alpine_rt_mod.py --link
```

`--link` creates a junction to `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\mods\unpacked\autoroad_alpine_rt\`.

Lua / `portals.json` / injected TSStatics: **quit BeamNG entirely and start it again** (not only Freeroam → load map, not only World Editor reload). Do not Save Level in the World Editor if inject is the source of the portal objects.

New gate: add YAML, run `build_portals.py`, reload the level. `dwell_s` will later be replaced by routed driving time.

---

## Session JSON

```text
settings/alpine_rt/session.json
  session_id, started_at
  settings: { environment: { time, play, cloudCover, fogDensity, ... }, weather_preset }
  vehicle: { model, config }
  pending_restore: { to_level, from_gate, arrive: { pos, tangent } }   # only during load
  segments: []   # placeholder, no summary UI
```

---

## Product goal

The player drives pass segments and, at the edge, continues on purpose. Hard switch, session stays:

- Segment times + total time (not yet)
- Environment + same vehicle (today)
- Overview at the end (not yet)

No engine world streaming.

## What BeamNG can / cannot do

| Reality | Consequence |
|---------|-------------|
| No seamless streaming | One map = one load |
| `core_levels.startLevel("/levels/<name>/main/")` | Hard switch |
| Stock timers do not survive the switch | Own timing later |
| Weather / car do not persist magically | Snapshot in session.json |
| GE extension `unloadMode=manual` survives the switch | Session core lives there |

## Non-goals (for now)

- Real streaming / one mega heightmap of every pass
- Official race UI across maps
- Online leaderboards
- Traffic, damage persistence

Reference: level change via `core_levels.startLevel`; persistence via `jsonWriteFile` / `jsonReadFile` (not `settings` as a dumping ground).
