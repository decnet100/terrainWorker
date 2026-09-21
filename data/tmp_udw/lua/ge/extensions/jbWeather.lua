-- jbWeather: dynamic weather engine with named presets and a random rolling forecast.
-- Each preset is a full target state (sky darkness + colour, sun, fog, grip, rain). The engine eases
-- the live state toward the active preset over a transition, so weather "rolls in" smoothly instead
-- of snapping. A forecast is a queue of weighted-random presets the engine steps through on a timer
-- (the "server tick"). Rain is real editor-style 3D Precipitation; the wet-road look is spread over
-- frames so it never freezes the game.
local M = {}

-- ===================== presets =====================
-- *Mul / tint* values scale the map's OWN captured baseline (so it works on any map). grip is the
-- ground-model strength (1 = stock). rain is a Precipitation drop count (0 = none). tintR/G/B shift
-- the sky colour: lower = darker, unequal channels = a hue shift (e.g. blue-grey thunderstorm).
-- wet = road roughness target (lower = shinier/wetter); scales the track water effect with severity.
local PRESETS = {
  clear             = { fogAdd=0.000, cloud=0.00, tempDrop=0,  skyMul=1.00, sunMul=1.00, ambMul=1.00, brightMul=1.00, grip=1.00, rain=0,    rainLit=0, wet=0,    tintR=1.00, tintG=1.00, tintB=1.00 },
  overcast          = { fogAdd=0.0015,cloud=0.90, tempDrop=2,  skyMul=0.62, sunMul=0.16, ambMul=0.78, brightMul=0.55, grip=0.98, rain=0,    rainLit=0, wet=0,    tintR=0.72, tintG=0.74, tintB=0.78 },
  fog               = { fogAdd=0.013, cloud=0.55, tempDrop=3,  skyMul=0.70, sunMul=0.45, ambMul=0.82, brightMul=0.60, grip=0.97, rain=0,    rainLit=0, wet=0,    tintR=0.82, tintG=0.82, tintB=0.84 },
  drizzle           = { fogAdd=0.003, cloud=0.85, tempDrop=4,  skyMul=0.52, sunMul=0.07, ambMul=0.68, brightMul=0.42, grip=0.95, rain=1200, rainLit=1, wet=0.28, tintR=0.62, tintG=0.65, tintB=0.70 },
  rain              = { fogAdd=0.004, cloud=0.95, tempDrop=6,  skyMul=0.42, sunMul=0.03, ambMul=0.60, brightMul=0.30, grip=0.92, rain=2200, rainLit=1, wet=0.18, tintR=0.54, tintG=0.57, tintB=0.64 },
  thunderstorm      = { fogAdd=0.006, cloud=1.00, tempDrop=8,  skyMul=0.30, sunMul=0.015,ambMul=0.50, brightMul=0.20, grip=0.90, rain=3200, rainLit=1, wet=0.11, tintR=0.42, tintG=0.46, tintB=0.55 },
  heavyThunderstorm = { fogAdd=0.009, cloud=1.00, tempDrop=10, skyMul=0.20, sunMul=0.008,ambMul=0.40, brightMul=0.12, grip=0.88, rain=4200, rainLit=1, wet=0.07, tintR=0.34, tintG=0.38, tintB=0.50 },
}
local PRESET_ORDER = {"clear","overcast","fog","drizzle","rain","thunderstorm","heavyThunderstorm"}
-- cloud = 0.39's real overcast lever (CloudLayer coverage 0..1). The old sky-dimming (skyMul/sunMul/
-- colorize on ScatterSky) is deprecated/gradient-overridden in 0.39, so those stay only as a harmless
-- fallback for older game versions; cloud coverage + fog are what actually cover the sky now.
local EASED = {"fogAdd","cloud","tempDrop","skyMul","sunMul","ambMul","brightMul","grip","tintR","tintG","tintB"}
-- per-preset thunder + wind levels (0..1); custom weather sets its own from the sliders. wind drives BOTH the wind
-- audio bed AND the real aero wind speed (cur.wind * WIND_MAX m/s). A gust factor + drift is added at runtime.
local WEATHER_FX = {
  clear = { wind = 0.00 }, overcast = { wind = 0.18 }, fog = { wind = 0.00 }, drizzle = { wind = 0.22 },
  rain = { wind = 0.38 }, thunderstorm = { wind = 0.62, thunder = 0.6 }, heavyThunderstorm = { wind = 0.85, thunder = 1.0 },
}
local WIND_MAX = 22   -- m/s at wind level 1.0 (about 49 mph, a strong gale)
-- the forecast is a coherent chain (Markov walk), not independent random picks: weather only steps to
-- a NEARBY condition on this calm->stormy scale, so it eases clear->overcast->drizzle->rain->storm
-- (and back) instead of jumping thunderstorm->clear. The step odds are PER-SEVERITY (a small Markov
-- matrix): it drifts freely around clear/cloudy/rain for variety, climbing into a STORM is rare, and
-- a storm carries a strong downward pull so it fades out gradually. RNG-seeded, so runs differ.
local FORECAST_ORDER = {"clear", "overcast", "fog", "drizzle", "rain", "thunderstorm", "heavyThunderstorm"}
local FORECAST_SEV = {}
for i, n in ipairs(FORECAST_ORDER) do FORECAST_SEV[n] = i end
local FORECAST_TRANS = {        -- by current severity (1=clear..7=heavy): {-2,-1,0,+1,+2} step weights
  [1] = { 0, 0, 3, 5, 2 },      -- clear: usually clouds over before long
  [2] = { 0, 3, 3, 3, 1 },      -- overcast (hub): clear / hold / fog / drizzle
  [3] = { 2, 3, 2, 3, 0 },      -- fog: transient, eases back or on to drizzle
  [4] = { 2, 3, 3, 3, 0 },      -- drizzle <-> rain freely
  [5] = { 2, 4, 3, 1, 0 },      -- rain: lingers/eases; only ~10% builds into a storm
  [6] = { 4, 4, 1, 1, 0 },      -- thunderstorm: fades fast, slim chance to intensify
  [7] = { 5, 4, 1, 0, 0 },      -- heavy: fades fast
}
local RAIN_DARK = 0.6   -- rain only shows once cur.skyMul has eased below this (storm "moving in")
local RAIN_DENSITY = 3  -- VISUAL drop multiplier: the Precipitation gets numDrops = logical count * this (MK-style density).
                        -- decoupled from the logical count so audio/gating are unaffected. Tune down if it flashes/lags.
-- wetness accumulator: grip worsens the longer it rains, and recovers when dry. wetness 0..1 rises
-- while raining and falls while dry; it drops grip by up to WETNESS_PENALTY below the preset's base.
local WETNESS_PENALTY = 0.05   -- max extra grip drop at full accumulated wetness
local ACCUM_RATE = 0.004       -- wetness gained per second of rain (~4 min to fully soak)
local DRY_RATE = 0.0025        -- wetness lost per second when dry (~7 min to fully dry out)

-- ===================== state =====================
local cur, tgt, from = {}, {}, {}
local stormExp, stormIrrK = nil, 1   -- storm exposure + sun-irradiance level (set in startTransition, read by the lightning flash)
local activeName = "clear"
local transActive, transElapsed, transDur, lastAppliedP = false, 0, 15, -1
local orig = nil
local rainShown, lastRainCount, lastWetRough = 0, 0, 0
local wetness, lastGripApplied = 0, -1   -- accumulated rain wetness 0..1; last grip we committed
-- forecast
local forecast, fcIndex = {}, 0
local forecastOn, tickElapsed, tickInterval = false, 0, 180

local function lerp(a, b, t) return a + (b - a) * t end
local function clamp01(x) return x < 0 and 0 or (x > 1 and 1 or x) end

-- ===================== live "storm look" knobs (driven from the UI sliders, 0..100 each) =====================
-- These let the player dial the storm's look in real time from the Weather panel -- no console, no reload.
-- Defaults reproduce the current tuning. Mapped to the actual levers by the lk* helpers below.
local LOOK = { dark = 50, cloud = 72, fog = 50, grey = 48 }
local function lkExpFloor() return 1 - 0.80 * (LOOK.dark  / 100) end   -- scene exposure at FULL storm (lower = darker)
local function lkIrrFloor() return 1 - 0.85 * (LOOK.dark  / 100) end   -- sun-light at FULL storm (lower = darker/flatter)
local function lkCloudMul() return (LOOK.cloud / 100) * 4.0 end        -- cloud-coverage multiplier
local function lkFogMul()   return (LOOK.fog   / 100) * 2.0 end        -- storm fog multiplier
local function lkGrey()     return 0.72 - 0.52 * (LOOK.grey / 100) end -- haze/fog tint (lower = greyer + darker)

-- v2 (0.39): drive the game's NATIVE weather engine so storms use the real volumetric clouds + its own
-- smooth lerp, instead of us poking sky objects (which looked flat/murky and fought the per-frame sun).
-- Presets ship in art/weather/jbweather.json. USE_NATIVE=false falls back to the old direct-poke path.
local USE_NATIVE = true
local NATIVE_PRESET = { clear='jb_clear', overcast='jb_overcast', fog='jb_fog', drizzle='jb_rain',
  rain='jb_rain', thunderstorm='jb_storm', heavyThunderstorm='jb_storm', custom='jb_storm' }

local function initClear()
  for k, v in pairs(PRESETS.clear) do cur[k] = v; tgt[k] = v; from[k] = v end
  activeName = "clear"; transActive = false; lastRainCount = 0
end
initClear()

local function scatterSky()
  if not (scenetree and scenetree.findObject) then return nil end
  local o = scenetree.findObject("ScatterSky") or scenetree.findObject("sunsky") or scenetree.findObject("scattersky")
  if o then return o end
  if scenetree.findClassObjects then
    local list = scenetree.findClassObjects('ScatterSky')
    if list and list[1] then return scenetree.findObject(list[1]) end
  end
  return nil
end

local function p4(v)
  if not v then return nil end
  local x = v.x; if x == nil then x = v.r end
  local y = v.y; if y == nil then y = v.g end
  local z = v.z; if z == nil then z = v.b end
  local w = v.w; if w == nil then w = v.a end
  if x == nil then return nil end
  return { x, y, z, w or 1 }
end

-- 0.39: drive the scene objects DIRECTLY instead of core_environment.setFogDensity/setCloudCover.
-- Those wrappers fire onEnvironmentChanged, which makes the game re-apply the map's DEFAULT sky and
-- instantly wipe our weather (that's why fog/clouds "never applied" in 0.39). Setting the object field
-- + postApply ourselves sticks.
local function firstOfClass(cls)
  if not (scenetree and scenetree.findClassObjects) then return nil end
  local l = scenetree.findClassObjects(cls)
  if l and l[1] then return scenetree.findObject(l[1]) end
  return nil
end
local function setFogDirect(dens)
  local li = firstOfClass('LevelInfo'); if not li then return end
  pcall(function() li.fogDensity = dens; li:postApply() end)
end
local function getFogDirect()
  local li = firstOfClass('LevelInfo'); return li and tonumber(li.fogDensity) or nil
end
-- fog COLOR: the map's default is a bright blue-white (sunny haze) -> that's why storms looked sunny in
-- the distance. We tint it toward a stormy blue-grey as weather darkens. Set via setField (it's a color).
local function setFogColorDirect(r, g, b)
  local li = firstOfClass('LevelInfo'); if not li then return end
  pcall(function() li:setField('fogColor', 0, string.format("%.4f %.4f %.4f 1", r, g, b)); li:postApply() end)
end
local function getFogColorDirect()
  local li = firstOfClass('LevelInfo'); if not li then return nil end
  local s = li.getField and li:getField('fogColor', 0)
  if type(s) ~= 'string' then return nil end
  local r, g, b = s:match("([%-%d%.]+)[%s,]+([%-%d%.]+)[%s,]+([%-%d%.]+)")
  if r then return { tonumber(r), tonumber(g), tonumber(b) } end
  return nil
end
local function allOfClass(cls)
  local out = {}
  if scenetree and scenetree.findClassObjects then
    for _, n in ipairs(scenetree.findClassObjects(cls) or {}) do
      local o = scenetree.findObject(n); if o then out[#out+1] = o end
    end
  end
  return out
end
local function setCloudDirect(cov, commit)
  local list = allOfClass('CloudLayer')            -- 0.39 maps can have several cloud objects -> set them ALL
  -- coverage renders live, so set it every frame WITHOUT postApply for a smooth fade; postApply
  -- REGENERATES the cloud pattern (that hard reshuffle was the "jump cuts"), so only do it on `commit`
  -- -- once, at the end of a transition, to lock the final look in.
  local cirrus = 1.6 * clamp01((cur and cur.cloud) or 0)   -- high wispy layer fills the upper sky + helps mask the sun
  for _, c in ipairs(list) do
    pcall(function() c.coverage = cov; c.cirrusCoverage = cirrus; if commit then c:postApply() end end)
  end
end
local function getCloudDirect()
  local l = allOfClass('CloudLayer'); return l[1] and tonumber(l[1].coverage) or nil
end
-- Map the preset's cloud (0..1) to an ACTUAL coverage value, scaled to THIS map's own coverage range
-- (coverage isn't 0..1 -- baselines can be >1). clear ~0.15x .. storm ~1.9x the map's reference cover,
-- so weather visibly clears the sky or fully overcasts it whatever the map's scale is.
local function cloudCoverageTarget()
  local ref = math.max((orig and orig.cloud) or 0, 1.0)
  return ref * (0.12 + lkCloudMul() * clamp01((cur and cur.cloud) or 0))   -- coverage scaled by the live Cloud knob
end

local function captureBaseline()
  orig = {}
  orig.fog      = getFogDirect() or (core_environment and core_environment.getFogDensity and core_environment.getFogDensity()) or 0.001
  orig.cloud    = getCloudDirect() or 0        -- map's own cloud coverage; weather adds ON TOP of this
  orig.fogColor = getFogColorDirect() or { 0.74, 0.82, 0.93 }   -- map's default fog tint (sunny blue-white)
  local sky = scatterSky()
  orig.sky = (sky and sky.skyBrightness) or 1.0
  if sky then
    orig.sunScale = p4(sky.sunScale)
    orig.ambientScale = p4(sky.ambientScale)
    orig.colorize = p4(sky.colorize)
    orig.sunSize = tonumber(sky.sunSize)
    orig.flareScale = tonumber(sky.flareScale)
    orig.brightness = tonumber(sky.brightness)
    -- 0.39 levers (the old ones above are dead now): `exposure` = whole-scene HDR darkness (storm gloom),
    -- `atmoSunAngularSize` = the visible sun-disk size (the "sun shrinks" ask).
    orig.exposure   = tonumber(sky.exposure)
    orig.sunAngular = tonumber(sky.atmoSunAngularSize)
    -- atmoSunIrradiance = the SUN'S actual light into the atmosphere (drives shadows, scene brightness,
    -- and the blue-sky scatter). Dimming THIS is what turns a sunny-lit scene into real overcast.
    orig.sunIrr = nil
    local si = sky.getField and sky:getField('atmoSunIrradiance', 0)
    if type(si) == 'string' then
      local x, y, z = si:match("([%-%d%.eE]+)[%s,]+([%-%d%.eE]+)[%s,]+([%-%d%.eE]+)")
      if x then orig.sunIrr = { tonumber(x), tonumber(y), tonumber(z) } end
    end
    -- the sky's BLUE = atmoRayleighScattering (b >> r). Flattening it toward grey is what actually
    -- kills the "still blue in a storm" look without fighting the sun. Captured at real scale so we
    -- never break the sky with guessed numbers.
    orig.rayleigh = nil
    local ry = sky.getField and sky:getField('atmoRayleighScattering', 0)
    if type(ry) == 'string' then
      local x, y, z = ry:match("([%-%d%.eE]+)[%s,]+([%-%d%.eE]+)[%s,]+([%-%d%.eE]+)")
      if x then orig.rayleigh = { tonumber(x), tonumber(y), tonumber(z) } end
    end
    -- atmoMieScattering = the bright forward glow/haze AROUND the sun (the blown-out hotspot on the
    -- horizon in the sun's direction). Cut it for storms so that patch greys out like the rest.
    orig.mie = nil
    local me = sky.getField and sky:getField('atmoMieScattering', 0)
    if type(me) == 'string' then
      local x, y, z = me:match("([%-%d%.eE]+)[%s,]+([%-%d%.eE]+)[%s,]+([%-%d%.eE]+)")
      if x then orig.mie = { tonumber(x), tonumber(y), tonumber(z) } end
    end
  end
end

-- ===================== rain (editor-style 3D Precipitation) =====================
local function clearRain()
  local o = scenetree and scenetree.findObject and scenetree.findObject("jbWeatherRain")
  if o then o:delete() end
end
local function setRain(count)
  clearRain()
  rainShown = count or 0
  if rainShown <= 0 then return end
  if not (createObject and scenetree and scenetree.MissionGroup) then return end
  local obj = createObject('Precipitation')
  if not obj then return end
  obj:setField('dataBlock', 0, 'rain_drop')              -- spawn like the world editor (clean, no flash)
  obj:setField('numDrops', 0, tostring(math.floor(rainShown * RAIN_DENSITY)))  -- VISUAL count (logical amount * density) for a heavy, MK-like field
  obj:setField('doCollision', 0, 'false')                 -- per-drop world collision = big CPU cost at high counts; physics-only, no flash
  obj:setField('reflect', 0, 'false')                     -- drops needn't compute reflections
  obj:setField('useLighting', 0, 'true')                  -- ALWAYS light the drops so they stay dim -> high counts don't trip the HDR flash
  -- NOTE: the "realism" fields (rotateWithCamVel / useTurbulence / useWind / useTrueBillboards / min-maxMass /
  -- glowIntensity) were REMOVED -- rotateWithCamVel in particular stretches drops by camera velocity and blew them
  -- into full-screen BLACK SQUARES while driving/doing donuts. Density stays; the fancy motion fields do not.
  obj:registerObject('jbWeatherRain')
  scenetree.MissionGroup:addObject(obj)
end

-- ===================== wet road look (per-preset intensity, spread over frames) =====================
-- wetness = a roughnessFactor target on road materials (lower = shinier/wetter); each preset sets it,
-- so the track water effect scales with severity. Reloads are spread 1/frame so it never freezes.
-- genuine road-surface words only -- broad ones like "surface"/"floor"/"parking" matched dozens of
-- non-road materials, inflating the reload count (the lag) without adding the road you drive on.
local roadWords = {"road","asphalt","concrete","pavement","tarmac","sidewalk","curb","kerb",
  "track","blacktop","macadam","raceway","speedway","circuit","racetrack","cobble"}
-- NOTE: do NOT exclude "light" -- road materials are often named e.g. "road_asphalt_light" (light-
-- coloured asphalt), and excluding it skips the actual road. Lamp materials slipping in is harmless.
local excludeWords = {"sky","cloud","glass","window","water","tree","foliage","grass","plant",
  "leaf","wheel","tire","glow","decal","sign","smoke","fire","emissive",
  "grid","border","crack","crossing","skidmark","marking","arrow","paint"}
local savedRoughness, savedDiffuse, wetQueue = {}, {}, {}
local wetTargetRough, wetAppliedRough = 0, 0   -- target / committed road roughness (0 = dry)
local function matchesAny(low, list)
  for _, w in ipairs(list) do if string.find(low, w, 1, true) then return true end end
  return false
end
-- max road materials to wet. Each is a shader recompile, and a rainier preset re-wets them ALL at a
-- new roughness, so an uncapped set on a big map = sustained chop. Cap it, but paved-NAMED materials
-- FIRST -- those are the visible road surface (incl. terrain-baked roads); road objects are a backup.
local WET_CAP = 24   -- tunable live with tune('cap', N): higher = more coverage but more reload lag
local function collectRoadMats()
  local targets, n = {}, 0
  local function add(mn)
    if mn and mn ~= "" and not targets[mn] and not matchesAny(string.lower(mn), excludeWords) then
      targets[mn] = true; n = n + 1
    end
  end
  -- road-OBJECT materials first -- these are the actual drivable surfaces (markings now excluded)
  local function addFrom(cls, fld)
    for _, rn in ipairs(scenetree.findClassObjects(cls) or {}) do
      if n >= WET_CAP then return end
      local o = scenetree.findObject(rn)
      if o then
        local mn = o:getField(fld, 0)
        if (not mn) or mn == "" then mn = o:getField(fld, "") end
        add(mn)
      end
    end
  end
  addFrom('DecalRoad', 'Material'); addFrom('MeshRoad', 'topMaterial')
  -- then paved-named surface materials, up to the cap
  for _, m in ipairs(scenetree.findClassObjects('Material') or {}) do
    if n >= WET_CAP then break end
    if matchesAny(string.lower(m), roadWords) then add(m) end
  end
  return targets
end
-- scale the rgb of a torque "r g b a" color string (treat unset as white) -- used to darken albedo
local function darkenColorStr(s, mul)
  local r, g, b, a
  if s and s ~= "" then r, g, b, a = s:match("([%-%d%.eE]+)%s+([%-%d%.eE]+)%s+([%-%d%.eE]+)%s*([%-%d%.eE]*)") end
  r = tonumber(r) or 1; g = tonumber(g) or 1; b = tonumber(b) or 1; a = tonumber(a) or 1
  return string.format("%g %g %g %g", r * mul, g * mul, b * mul, a)
end
local function wetOne(matName, rough)
  local mat = scenetree.findObject(matName); if not mat then return end
  local layers = tonumber(mat.activeLayers) or 1
  savedRoughness[matName] = savedRoughness[matName] or {}
  savedDiffuse[matName] = savedDiffuse[matName] or {}
  -- wet asphalt is DARKER -- darken more the wetter it is (lower rough). 0.6 = soaked, 0.95 = just damp.
  local dark = 0.6 + 0.35 * math.max(0, math.min(1, (rough - 0.07) / 0.43))
  for layer = 0, layers - 1 do
    if savedRoughness[matName][layer] == nil then savedRoughness[matName][layer] = mat:getField('roughnessFactor', layer) end
    if savedDiffuse[matName][layer] == nil then savedDiffuse[matName][layer] = mat:getField('diffuseColor', layer) end
    pcall(function() mat:setField('roughnessFactor', layer, tostring(rough)) end)
    pcall(function() mat:setField('diffuseColor', layer, darkenColorStr(savedDiffuse[matName][layer], dark)) end)
  end
  pcall(function() mat:reload() end)
end
local function dryOne(matName)
  local mat = scenetree.findObject(matName); if not mat then return end
  local rl = savedRoughness[matName]
  if rl then for layer, ov in pairs(rl) do
    local val = (ov ~= nil and ov ~= "") and ov or "1"
    pcall(function() mat:setField('roughnessFactor', layer, tostring(val)) end)
  end end
  local dl = savedDiffuse[matName]
  if dl then for layer, ov in pairs(dl) do
    local val = (ov ~= nil and ov ~= "") and ov or "1 1 1 1"
    pcall(function() mat:setField('diffuseColor', layer, val) end)
  end end
  pcall(function() mat:reload() end)
end
-- set the target road wetness (roughness; 0 = dry). Rebuilds the spread queue only on a change, so
-- a rainier preset re-wets the roads at a lower (shinier) roughness.
local function setWetLook(rough)
  rough = rough or 0
  -- guard on the TARGET, not the applied value: while the queue is still draining toward `rough`,
  -- this is called every frame -- if we rebuilt whenever applied!=rough it would never finish (the
  -- old bug: queue stuck full, material reloaded every frame = no wet + permanent lag).
  if rough == wetTargetRough then return end
  wetTargetRough = rough
  wetQueue = {}
  local src = (rough > 0) and collectRoadMats() or savedRoughness
  for matName in pairs(src) do wetQueue[#wetQueue + 1] = matName end
end
local function tickWet()
  if #wetQueue == 0 then return end
  local matName = table.remove(wetQueue)   -- one shader recompile per frame -> no freeze
  if wetTargetRough > 0 then wetOne(matName, wetTargetRough) else dryOne(matName) end
  if #wetQueue == 0 then
    wetAppliedRough = wetTargetRough
    if wetTargetRough == 0 then savedRoughness = {}; savedDiffuse = {} end
  end
end

-- ===================== apply the live state to the world =====================
local function applyState()
  if not orig then captureBaseline() end
  if USE_NATIVE then     -- native engine owns the sky (driven from startTransition); we only gate our own rain here
    local wantRain = (cur.skyMul < RAIN_DARK) and lastRainCount or 0
    if wantRain ~= rainShown then setRain(wantRain) end
    return
  end
  local sky = scatterSky()
  -- FOG + CLOUDS = the 0.39 way: write the objects directly (see firstOfClass note above).
  -- CloudLayer coverage is what actually covers the sky now; we hold at least the map's own cover
  -- and add the preset's on top, so "clear" restores the map default and storms fully overcast.
  setFogDirect((orig.fog or 0) + (cur.fogAdd or 0) * lkFogMul())   -- fog amount scaled by the live Fog knob
  if orig.fogColor then      -- grey the haze as it darkens, so the DISTANCE reads stormy instead of sunny
    local dk = clamp01(1 - (cur.brightMul or 1))       -- 0 clear .. ~0.9 heavy storm
    local o, g = orig.fogColor, lkGrey()
    setFogColorDirect(o[1] + (g - o[1]) * dk, o[2] + (g + 0.02 - o[2]) * dk, o[3] + (g + 0.08 - o[3]) * dk)
  end
  setCloudDirect(cloudCoverageTarget())
  if sky then
    -- 0.39 PRIMARY levers: darken the whole scene via exposure, shrink the sun via its angular size.
    -- exposure curve is gentle (0.4x at the darkest storm) so it can't go pitch-black at any baseline scale.
    if orig.exposure   then local f = lkExpFloor(); pcall(function() sky.exposure = orig.exposure * (f + (1 - f) * (cur.brightMul or 1)) end) end
    if orig.sunAngular then pcall(function() sky.atmoSunAngularSize = orig.sunAngular * math.max(0.03, (cur.sunMul or 1) ^ 1.2) end) end
    -- THE big one: dim the sun's light into the sky so shadows soften, the scene darkens, and the blue
    -- sky greys out into overcast. Scale the whole irradiance vector by the preset brightness.
    if orig.sunIrr then
      local f = lkIrrFloor(); local k = f + (1 - f) * (cur.brightMul or 1)   -- sun light scaled by the live Darkness knob
      local s = orig.sunIrr
      pcall(function() sky:setField('atmoSunIrradiance', 0, string.format("%f %f %f", s[1]*k, s[2]*k, s[3]*k)) end)
    end
    pcall(function() sky.skyBrightness = orig.sky * cur.skyMul end)   -- deprecated in 0.39; kept as old-version fallback, guarded so it can't throw
    local function dimC(prop, base, mul)
      if base then pcall(function() sky[prop] = Point4F(base[1] * mul, base[2] * mul, base[3] * mul, base[4]) end) end
    end
    dimC('sunScale', orig.sunScale, cur.sunMul)
    dimC('ambientScale', orig.ambientScale, cur.ambMul)
    if orig.colorize then
      pcall(function() sky.colorize = Point4F(orig.colorize[1] * cur.tintR, orig.colorize[2] * cur.tintG, orig.colorize[3] * cur.tintB, orig.colorize[4]) end)
    end
    local function setNum(prop, base, mul) if base ~= nil then pcall(function() sky[prop] = base * mul end) end end
    setNum('sunSize', orig.sunSize, cur.sunMul)       -- shrink the visible sun disk with the sun light
    setNum('flareScale', orig.flareScale, cur.sunMul) -- and the lens flare
    setNum('brightness', orig.brightness, cur.brightMul)
    pcall(function() sky:postApply() end)
  end
  -- rain shows once the sky has darkened past RAIN_DARK (so it spawns into a dark scene, flash-free,
  -- and reads as "moving in"); it keeps the last rainy count through a clearing transition until the
  -- sky brightens again, so it fades out instead of cutting. (grip + wet-look are driven in onUpdate
  -- by the wetness accumulator, so they keep updating between transitions too.)
  local wantRain = (cur.skyMul < RAIN_DARK) and lastRainCount or 0
  if wantRain ~= rainShown then setRain(wantRain) end
end

-- effective grip = the active preset's base grip, dropped further by accumulated wetness
local function effGrip()
  return math.max(0.05, math.min(1, (cur.grip or 1) - WETNESS_PENALTY * wetness))
end
-- write grip to every ground model -- self-throttled so it only commits when grip actually moved
local function applyGrip()
  local g = effGrip()
  if math.abs(g - lastGripApplied) < 0.004 then return end
  lastGripApplied = g
  if core_environment and core_environment.groundModels and be then
    for name, gm in pairs(core_environment.groundModels) do
      if gm and gm.cdata then gm.cdata.strength = g; be:setGroundModel(name, gm.cdata) end
    end
  end
end

-- ===================== transitions / presets =====================
-- start an eased transition toward params table p (a preset OR a custom-built table)
local function startTransition(name, p, dur)
  if not orig then captureBaseline() end
  activeName = name
  for _, k in ipairs(EASED) do from[k] = cur[k]; tgt[k] = p[k] end
  tgt.rain = p.rain; tgt.rainLit = p.rainLit
  local fx = WEATHER_FX[name]
  cur.thunder = (fx and fx.thunder) or p.thunder or 0     -- audio cadence/level applies immediately (not eased)
  cur.wind    = (fx and fx.wind)    or p.wind    or 0
  if p.rain and p.rain > 0 then lastRainCount = p.rain; lastWetRough = p.wet or 0 end   -- keep for the fade-out on clearing
  transDur = tonumber(dur) or 15; if transDur <= 0 then transDur = 0.01 end
  transElapsed = 0; transActive = true; lastAppliedP = -1
  if USE_NATIVE and core_weather and core_weather.switchWeather then   -- hand the SKY to the native engine (smooth volumetric transition)
    pcall(function() core_weather.switchWeather(NATIVE_PRESET[name] or 'jb_overcast', transDur) end)
    -- grey the sky: flatten the blue Rayleigh scatter toward its own average (kills the blue) and dim it
    -- a touch, scaled by how stormy the target is. clear -> baseline restored; storm -> dim grey sky.
    if orig and orig.rayleigh then
      local sky = scatterSky()
      if sky then
        local r = orig.rayleigh
        local avg = (r[1] + r[2] + r[3]) / 3
        local d = clamp01(1 - (tgt.brightMul or 1))     -- 0 clear .. ~0.9 heavy storm
        local goal = avg * (1 - 0.35 * d)               -- storms dim the scatter a bit as well as greying it
        local function mix(c) return c + (goal - c) * d end
        pcall(function() sky:setField('atmoRayleighScattering', 0, string.format("%.8f %.8f %.8f", mix(r[1]), mix(r[2]), mix(r[3]))) end)
        -- shrink the sun disk + flare so it stops punching through the cloud gaps in a storm
        local sf = math.max(0.02, (1 - d) ^ 1.5)
        if orig.sunAngular then pcall(function() sky.atmoSunAngularSize = orig.sunAngular * sf end) end
        if orig.flareScale ~= nil then pcall(function() sky.flareScale = orig.flareScale * sf end) end
        -- cut the sun's actual LIGHT for storms so the harsh glare/specular on the car goes away
        -- (moderate: ~35% at heavy storm, keeps some contrast; full on clear). One-shot, holds like rayleigh.
        if orig.sunIrr then
          local k = 1 - 0.72 * d
          stormIrrK = k          -- remember the storm's sun-light level so the lightning flash can flood from/return to it
          local s = orig.sunIrr
          pcall(function() sky:setField('atmoSunIrradiance', 0, string.format("%f %f %f", s[1]*k, s[2]*k, s[3]*k)) end)
        end
        -- kill the sun-direction glow (the blown-out "nuclear" hotspot) for storms
        if orig.mie then
          local mk = 1 - 0.88 * d
          local m = orig.mie
          pcall(function() sky:setField('atmoMieScattering', 0, string.format("%.8f %.8f %.8f", m[1]*mk, m[2]*mk, m[3]*mk)) end)
        end
        -- pull the sky dome's own exposure down for storms (targets the bright top-of-sky directly)
        if orig.exposure then local e = orig.exposure * (1 - 0.5 * d); stormExp = e; pcall(function() sky.exposure = e end) end
      end
    end
  end
end

local function setPreset(name, dur)
  local p = PRESETS[name]; if not p then return end
  startTransition(name, p, dur)
end

-- CUSTOM weather: build a params table live from the panel's sliders (each 0..1) and transition to it.
-- rain implies cloud cover (so it darkens enough to actually show), and drives grip/wet directly.
local function setCustom(opts, dur)
  opts = opts or {}
  local rain    = clamp01(tonumber(opts.rain) or 0)
  local fog     = clamp01(tonumber(opts.fog) or 0)
  local cloud   = clamp01(tonumber(opts.cloud) or 0)
  local thunder = clamp01(tonumber(opts.thunder) or 0)
  local wind    = clamp01(tonumber(opts.wind) or 0)
  local dark = math.min(1, cloud * 0.95 + rain * 0.6)
  local p = {
    fogAdd    = fog * 0.012,
    cloud     = math.max(cloud, rain * 0.9),   -- rain implies real cloud cover (0.39 overcast lever)
    tempDrop  = dark * 9,                       -- stormier custom weather reads a few degrees cooler
    skyMul    = 1 - dark * 0.80,
    sunMul    = 1 - dark * 0.92,
    ambMul    = 1 - dark * 0.52,
    brightMul = 1 - dark * 0.80,
    grip      = 1 - rain * 0.12,
    rain      = math.floor(rain * 5000),
    rainLit   = (rain > 0.02) and 1 or 0,
    wet       = rain * 0.22,
    thunder   = thunder,
    wind      = wind,
    tintR = 1 - dark * 0.50, tintG = 1 - dark * 0.46, tintB = 1 - dark * 0.36,
  }
  if p.rain > 0 then p.skyMul = math.min(p.skyMul, RAIN_DARK - 0.05) end   -- guarantee rain is dark enough to render
  startTransition('custom', p, tonumber(dur) or 6)
  lastRainCount = p.rain                                   -- custom drives rain directly: 0 => it stops (no dark-sky hold)
  lastWetRough = (p.rain > 0) and p.wet or 0
  forecastOn = false                                       -- custom = manual control, like a preset pick
  -- (no pushState here: it's a local defined later in the file; the UI re-polls right after via applyCustom)
end

-- ===================== forecast =====================
local fcWeights = {}   -- per-type frequency multiplier (name -> 0..n, default 1); set from the panel's FORECAST MIX
-- pick the next condition as a small step from the current one along the calm->stormy scale, biased by the
-- user's per-type frequency weights (a type weighted to 0 never appears; 2x appears about twice as often).
local function nextWeather(curName)
  local s = FORECAST_SEV[curName] or 1
  local w = FORECAST_TRANS[s] or FORECAST_TRANS[1]   -- {-2,-1,0,+1,+2} step weights for this severity
  local ww, total = {}, 0
  for i = 1, 5 do
    local tgt = FORECAST_ORDER[math.max(1, math.min(#FORECAST_ORDER, s + (i - 3)))]
    local fw = fcWeights[tgt]; if fw == nil then fw = 1 end
    ww[i] = w[i] * fw; total = total + ww[i]
  end
  if total <= 0 then ww, total = w, 0; for _, x in ipairs(w) do total = total + x end end  -- fallback if all weighted out
  local r, pick = math.random() * total, 0
  for i = 1, 5 do
    r = r - ww[i]
    if r <= 0 then pick = i - 3; break end            -- i=1->-2 .. i=5->+2
  end
  return FORECAST_ORDER[math.max(1, math.min(#FORECAST_ORDER, s + pick))]
end
local function buildForecast(n)
  forecast = {}
  local prev = activeName or "clear"
  for i = 1, (n or 8) do prev = nextWeather(prev); forecast[i] = prev end   -- chain from the current weather
  fcIndex = 0
end
local function pushState()
  if guihooks and guihooks.trigger then guihooks.trigger('jbWeatherState', M.getForecast()) end
end
local function advanceForecast()
  fcIndex = fcIndex + 1
  -- keep ~6 queued ahead (so the strip never runs dry), each a coherent step from the previous one
  while #forecast < fcIndex + 6 do
    forecast[#forecast + 1] = nextWeather(forecast[#forecast] or activeName or "clear")
  end
  setPreset(forecast[fcIndex] or nextWeather(activeName or "clear"), math.min(tickInterval * 0.5, 45))
  pushState()
end
local function startForecast(interval)
  if interval then tickInterval = math.max(10, tonumber(interval) or tickInterval) end
  if #forecast == 0 then buildForecast(8) end
  forecastOn = true; tickElapsed = 0
  advanceForecast()
end
local function stopForecast() forecastOn = false; pushState() end
local function setTick(interval) tickInterval = math.max(10, tonumber(interval) or tickInterval); pushState() end

-- TIME OF DAY: t is 0..1 (0=noon, 0.25=sunset, 0.5=midnight, 0.75=sunrise). Freezes the day cycle so it holds.
-- IMPORTANT: read the CURRENT tod table and only override time+play -- setTimeOfDay writes dayScale/nightScale/
-- dayLength/azimuthOverride straight from the passed table, so a partial table would null them and break the sky.
local function setTimeOfDay(t)
  t = tonumber(t); if t == nil then return end
  if not (core_environment and core_environment.setTimeOfDay and core_environment.getTimeOfDay) then return end
  pcall(function()
    local tod = core_environment.getTimeOfDay() or {}
    tod.time = t % 1
    tod.play = false
    core_environment.setTimeOfDay(tod)
  end)
  if orig then applyState() end   -- re-assert the active weather's sky over the new time-of-day colors
end
-- FORECAST MIX: per-type frequency weights (name -> 0..n). Higher = that weather shows up more in the auto-forecast.
local function setForecastWeights(t)
  if type(t) ~= 'table' then return end
  for _, n in ipairs(FORECAST_ORDER) do
    local v = tonumber(t[n]); if v then fcWeights[n] = math.max(0, v) end
  end
end

local healClock = 0   -- self-heal throttle: re-assert the weather after a gravity / physics / world reset
-- lightning: brief sky+ambient flashes during storms. flashOn = seconds left in the current flash; flashCool =
-- seconds to the next. A pulse this short reads as lightning and is far too quick to trip the HDR auto-exposure.
local flashOn, flashCool = 0, 0   -- lightning flash timer (stormExp/stormIrrK are declared up top so startTransition can set them)
-- soundscape: rain/wind loops are re-triggered just before each instance ends (Engine.Audio.playOnce plays a
-- one-shot file). thunderT = countdown to a pending clap after a flash (<0 = none). sndBase resolved once.
local rainSndT, windSndT, thunderT, thunderVol = 0, 0, -1, 0.6
local sndBase = nil
local curCamName, curInterior = '', false   -- live camera readout (for the interior-rain switch + UI debug)
-- REAL WIND: cur.wind (0..1) -> an aero wind vector on the player vehicle, with a drifting direction + gusts.
local windDir, windSpeedNow, windGustTarget, windGustT, windApplied = 0, 0, 0, 0, -1
local function sndFile(name)
  if not sndBase then
    for _, b in ipairs({ '/sounds/', 'sounds/', '/mods/unpacked/jb_weather/sounds/' }) do
      if FS and FS.fileExists and FS:fileExists(b .. 'jbw_rain2.ogg') then sndBase = b; break end
    end
    sndBase = sndBase or '/sounds/'
  end
  return sndBase .. name
end

-- ===================== hooks / API =====================
local function onUpdate(dtReal, dtSim)
  local dt = dtSim or dtReal or 0
  -- SELF-HEAL (runs even when idle, once the mod has been used): three things can get left broken -- a gravity or
  -- world reset destroys our rain object + wipes grip, and the fog can end up stuck away from the active preset
  -- (the review's "clear won't reset the fog"). Re-assert all three a couple of times a second, each guarded so it
  -- only fires on a REAL drift (no per-frame flicker).
  if orig then
    healClock = healClock + dt
    if healClock >= 1.5 then
      healClock = 0
      if (not USE_NATIVE) and not transActive then
        local wantFog = (orig.fog or 0) + (cur.fogAdd or 0) * lkFogMul()   -- MUST match applyState
        local haveFog = getFogDirect() or wantFog
        if math.abs(haveFog - wantFog) > 0.0008 then setFogDirect(wantFog) end   -- fog drifted/stuck -> snap it back
        if orig.fogColor then                                        -- re-assert stormy fog tint after a reset
          local dk = clamp01(1 - (cur.brightMul or 1))
          local o, g = orig.fogColor, lkGrey()
          setFogColorDirect(o[1] + (g - o[1]) * dk, o[2] + (g + 0.02 - o[2]) * dk, o[3] + (g + 0.08 - o[3]) * dk)
        end
        local wantCloud = cloudCoverageTarget()
        local haveCloud = getCloudDirect() or wantCloud
        if math.abs(haveCloud - wantCloud) > 0.03 then setCloudDirect(wantCloud, true) end  -- clouds wiped by a reset -> re-cover (commit)
      end
      if rainShown > 0 and not (scenetree and scenetree.findObject and scenetree.findObject("jbWeatherRain")) then
        setRain(rainShown)                                            -- rain object destroyed by a reset -> recreate
      end
      if (wetness > 0) or ((cur.grip or 1) < 1) then lastGripApplied = -2; applyGrip() end  -- re-write wet grip a reset wiped
    end
  end
  if not (forecastOn or transActive or rainShown > 0 or wetness > 0 or (cur.thunder or 0) > 0.02 or (cur.wind or 0) > 0.05) and #wetQueue == 0 then return end
  -- HOLD the storm darkness EVERY frame. 0.39 re-lights the sun each frame from its position (celestial
  -- onPreRender), so a one-time dim gets brightened back up (worst when the sun moves). Re-assert live
  -- (no postApply -> cheap, no reshuffle). Exposure darkens the whole rendered image incl. the bright sun
  -- side; irradiance greys the sky/shadows.
  if (not USE_NATIVE) and orig and (cur.brightMul or 1) < 0.985 then
    local sky2 = scatterSky()
    if sky2 then
      if orig.exposure then local f = lkExpFloor(); pcall(function() sky2.exposure = orig.exposure * (f + (1 - f) * (cur.brightMul or 1)) end) end
      if orig.sunIrr then
        local f = lkIrrFloor(); local k = f + (1 - f) * (cur.brightMul or 1)
        local s = orig.sunIrr
        pcall(function() sky2:setField('atmoSunIrradiance', 0, string.format("%f %f %f", s[1]*k, s[2]*k, s[3]*k)) end)
      end
    end
  end
  if forecastOn then
    tickElapsed = tickElapsed + dt
    if tickElapsed >= tickInterval then tickElapsed = tickElapsed - tickInterval; advanceForecast() end
  end
  if transActive then
    transElapsed = transElapsed + dt
    local p = clamp01(transElapsed / transDur)
    if p >= 1 then transActive = false end
    for _, k in ipairs(EASED) do cur[k] = lerp(from[k], tgt[k], p) end
    -- ease the sky-dome brightness LIVE every frame: skyBrightness applies WITHOUT postApply, so this stays smooth
    -- and never flickers the glass on Vulkan.
    local sky = scatterSky()
    if sky then pcall(function() sky.skyBrightness = (orig.sky or 1) * (cur.skyMul or 1) end) end
    -- smooth commits (~40x) for the ambient/sun/colorize fields -- back to the smoother transition the user prefers
    -- (the flicker turned out to be driving-during-rain, not the commit count).
    if (not transActive) or math.abs(p - lastAppliedP) >= 0.025 then
      lastAppliedP = p
      applyState()
      if not transActive then setCloudDirect(cloudCoverageTarget(), true) end  -- transition done -> commit the cloud look ONCE (no mid-fade reshuffle)
    end
  end
  -- THUNDER cadence during a settled storm. The VISUAL lightning flash was SCRAPPED: BeamNG's sky can't be flashed
  -- cheaply -- changing skyBrightness at runtime either needs a full sky rebuild (postApply = per-strike lag) or,
  -- without it, sticks and breaks the lighting. So we keep only the AUDIO -- schedule a thunder clap on the same
  -- cadence, and NEVER touch the sky here (so the storm's lighting stays exactly as the preset set it).
  local thunderLvl = cur.thunder or 0                       -- driven by the preset (storms) OR the custom Thunder slider
  if thunderLvl > 0.02 and not transActive then
    flashCool = flashCool - dt
    if flashCool <= 0 then
      local iv = 3 + (1 - thunderLvl) * 20                   -- heavier thunder = shorter gaps (lvl 1 ~3s, low ~20s)
      flashCool = iv * (0.7 + math.random() * 0.7)
      thunderT = 0.3 + math.random() * (1.5 + (1 - thunderLvl) * 2.5)   -- nearer, quicker claps when heavier
      thunderVol = 0.7 + thunderLvl * 0.3
      -- LIGHTNING FLASH: in-world exposure/irradiance spikes got eaten by the engine's HDR auto-exposure
      -- (it re-balances brightness instantly). So we flash on the UI layer instead -- fire a screen-white
      -- overlay in the app, which auto-exposure can't touch. The clap follows after thunderT (light->sound).
      if guihooks then pcall(function() guihooks.trigger('jbWeatherFlash', thunderLvl) end) end
    end
  end
  -- REAL WIND: map cur.wind (0..1) to an aero wind speed, drift its direction, add gusts, and push it onto the
  -- player vehicle via obj:setWind (persistent m/s vector -> BeamNG's aero moves the car). Throttled: only re-sent
  -- when the speed moves enough, so it's a handful of calls a second at most.
  local windGoal = (cur.wind or 0) * WIND_MAX
  windGustT = windGustT - dt
  if windGustT <= 0 then
    windGustT = 1.5 + math.random() * 3
    windGustTarget = windGoal * (0.75 + math.random() * 0.5)     -- gusts run 75-125% of the base
    windDir = windDir + (math.random() - 0.5) * 0.7              -- slowly drift the direction
  end
  windSpeedNow = windSpeedNow + (windGustTarget - windSpeedNow) * math.min(1, dt * 1.2)
  if windGoal < 0.4 and windSpeedNow < 0.6 then windSpeedNow = 0 end   -- settle fully calm
  if math.abs(windSpeedNow - windApplied) > 0.25 then
    windApplied = windSpeedNow
    local wx, wy = windSpeedNow * math.cos(windDir), windSpeedNow * math.sin(windDir)
    if be and be.getPlayerVehicle then
      local veh = be:getPlayerVehicle(0)
      if veh then pcall(function() veh:queueLuaCommand(string.format('if obj and obj.setWind then obj:setWind(%.2f,%.2f,0) end', wx, wy)) end) end
    end
    -- NOTE: do NOT call core_environment.setWindSpeed here. It fires onEnvironmentChanged, which makes the game
    -- re-apply the map's DEFAULT sky/clouds and wipes our storm dimming (sun shines back through). Not worth it.
  end
  -- SOUNDSCAPE: rain + wind loops (re-triggered just before each instance ends), and a thunder clap after a flash
  -- read the camera EVERY frame (cheap) so the interior/exterior switch + UI readout stay live. Interior = the
  -- first-person cockpit cam (name contains driver/cockpit/interior); hood/chase/orbit etc. count as exterior.
  if core_camera and core_camera.getActiveCamName then
    local okc, cn = pcall(core_camera.getActiveCamName)
    curCamName = (okc and cn) and tostring(cn) or ''
  else curCamName = '' end
  local lc = curCamName:lower()
  curInterior = (lc:find('driver') or lc:find('cockpit') or lc:find('interior') or lc:find('dash')) ~= nil
  if Engine and Engine.Audio and Engine.Audio.playOnce then
    if rainShown > 0 then
      rainSndT = rainSndT - dt
      if rainSndT <= 0 then
        -- ONE rain loop only. The interior/exterior camera switch is parked: playOnce can't be stopped, so swapping
        -- files on a camera change layers the old sound under the new one (the "overlap"). Single source = no overlap.
        -- fade-out is baked into the file end, so this fadeIn crossfades the loop (no swell, no seam).
        local vol = 0.30 + 0.18 * math.min(1, rainShown / 4200)
        local ok, res = pcall(function() return Engine.Audio.playOnce('AudioGui', sndFile('jbw_rain2.ogg'), { volume = vol, fadeInTime = 0.35 }) end)
        rainSndT = (ok and res and res.len and res.len > 1) and (res.len - 0.35) or 7.5
      end
    else rainSndT = 0 end                                                        -- stop scheduling (short loop = short tail)
    local windLvl = cur.wind or 0                          -- driven by the preset (storms) OR the custom Wind slider
    if windLvl > 0.05 then
      windSndT = windSndT - dt
      if windSndT <= 0 then
        local vol = 0.12 + windLvl * 0.34                    -- subtle bed under the rain, scales with the wind level
        -- baked fade-out on the file end, so this fadeIn crossfades the loop (no swell, no seam) -- same as the rain
        local ok, res = pcall(function() return Engine.Audio.playOnce('AudioGui', sndFile('jbw_wind2.ogg'), { volume = vol, fadeInTime = 0.35 }) end)
        windSndT = (ok and res and res.len and res.len > 1) and (res.len - 0.35) or 9.5
      end
    else windSndT = 0 end
    if thunderT >= 0 then
      thunderT = thunderT - dt
      if thunderT < 0 then
        local claps = { 'jbw_thunder1.ogg', 'jbw_thunder2.ogg', 'jbw_thunder3.ogg' }
        pcall(function() Engine.Audio.playOnce('AudioGui', sndFile(claps[math.random(#claps)]), { volume = thunderVol }) end)
      end
    end
  end
  -- wetness accumulator: soak up while rain falls, dry out otherwise -- grip follows continuously
  if rainShown > 0 then wetness = math.min(1, wetness + ACCUM_RATE * dt)
  else wetness = math.max(0, wetness - DRY_RATE * dt) end
  applyGrip()
  -- visual wetness scales with the accumulator: just damp when rain starts -> glossiest when fully
  -- soaked -> fades back as it dries. Bucketed to 0.05 so we only re-wet on a real step (no churn).
  local wetVisual = 0
  if (rainShown > 0 or wetness > 0.02) and lastWetRough > 0 then
    local r = lerp(0.5, lastWetRough, math.min(1, wetness))   -- 0.5 = barely damp, lastWetRough = full soak
    wetVisual = math.floor(r / 0.05 + 0.5) * 0.05
  end
  setWetLook(wetVisual)
  tickWet()  -- one wet/dry reload per frame
end

local function reset(dur)
  forecastOn = false
  wetness = 0; lastGripApplied = -1   -- explicit clear dries the roads instantly
  setPreset("clear", dur or 8)
  pushState()
end

local function onClientStartMission()
  orig = nil
  initClear()
  savedRoughness = {}; savedDiffuse = {}; wetTargetRough = 0; wetAppliedRough = 0; wetQueue = {}
  rainShown = 0; lastRainCount = 0; lastWetRough = 0; wetness = 0; lastGripApplied = -1
  forecast = {}; fcIndex = 0; forecastOn = false; tickElapsed = 0
  pcall(function() clearRain() end)
end

-- snapshot for the UI
function M.getForecast()
  local upcoming = {}
  for i = fcIndex + 1, math.min(#forecast, fcIndex + 5) do
    if forecast[i] then upcoming[#upcoming + 1] = forecast[i] end
  end
  return {
    current = activeName,
    upcoming = upcoming,
    forecastOn = forecastOn,
    tickInterval = tickInterval,
    nextIn = forecastOn and math.max(0, math.floor(tickInterval - tickElapsed)) or 0,
    presets = PRESET_ORDER,
    grip = math.floor(effGrip() * 100 + 0.5),          -- live effective grip % (preset + wetness)
    wetness = math.floor(wetness * 100 + 0.5),         -- accumulated rain wetness %
    raining = rainShown > 0,
    transitioning = transActive,
    camName = curCamName,          -- live camera name (debug: confirms the interior/exterior switch)
    interior = curInterior,        -- true when the cockpit rain clip is selected
    windMph = math.floor(windSpeedNow * 2.237 + 0.5),   -- live wind speed for the readout
    tempC = (function()                                 -- ambient temp (real, from the game) adjusted by the weather
      local gk = core_environment and core_environment.getTemperatureK and core_environment.getTemperatureK()
      if not gk then return nil end
      return math.floor((gk - 273.15) - (cur.tempDrop or 0) + 0.5)
    end)(),
  }
end

-- dev: live-tune the ACTIVE preset's parameter, e.g. extensions.jbWeather.tune('skyMul', 0.3)
local function tune(k, v)
  v = tonumber(v); if v == nil then return end
  if k == 'cap' then WET_CAP = math.max(1, math.floor(v)); return end   -- road-material coverage cap
  if k == 'accum' then ACCUM_RATE = v; return end                        -- wetness gained per sec raining
  if k == 'dry' then DRY_RATE = v; return end                            -- wetness lost per sec when dry
  if k == 'wetpenalty' then WETNESS_PENALTY = v; lastGripApplied = -1; return end  -- max grip drop from wetness
  if k == 'wet' then  -- road shininess of the ACTIVE preset; lower = wetter but heavier SSR cost
    if PRESETS[activeName] then PRESETS[activeName].wet = v end
    if lastWetRough > 0 then lastWetRough = v end   -- re-wet live (next onUpdate picks it up)
    return
  end
  local p = PRESETS[activeName]
  if p and p[k] ~= nil then
    p[k] = v
    if cur[k] ~= nil then cur[k] = v end
    if tgt[k] ~= nil then tgt[k] = v end
    lastAppliedP = -1
    if not transActive then applyState() end
  end
end

-- live "storm look" from the UI sliders (each 0..100). Applies instantly so the player sees it while dragging.
local function setLookAll(d, c, f, g)
  d = tonumber(d); c = tonumber(c); f = tonumber(f); g = tonumber(g)
  if d then LOOK.dark  = math.max(0, math.min(100, d)) end
  if c then LOOK.cloud = math.max(0, math.min(100, c)) end
  if f then LOOK.fog   = math.max(0, math.min(100, f)) end
  if g then LOOK.grey  = math.max(0, math.min(100, g)) end
  if orig and not transActive then lastAppliedP = -1; applyState() end   -- reflect immediately
end
local function getLook() return LOOK end

local function diag()
  local sky = scatterSky()
  local str = table.concat({
    "active=" .. activeName,
    "sky=" .. tostring(sky ~= nil),
    "trans=" .. tostring(transActive),
    "skyMul=" .. string.format("%.2f", cur.skyMul or -1),
    "grip=" .. string.format("%.2f", cur.grip or -1),
    "rainShown=" .. rainShown,
    "wetness=" .. string.format("%.2f", wetness),
    "effGrip=" .. string.format("%.2f", effGrip()),
    "wetApplied=" .. string.format("%.2f", wetAppliedRough),
    "wetTarget=" .. string.format("%.2f", wetTargetRough),
    "wetQ=" .. #wetQueue,
    "lastWet=" .. string.format("%.2f", lastWetRough),
    "fcOn=" .. tostring(forecastOn),
    "nextIn=" .. (forecastOn and math.floor(tickInterval - tickElapsed) or -1),
  }, "  ")
  print(str); return str
end

-- diagnostic: dump candidate road materials so we can see what the road actually uses vs what we wet
local function roadScan()
  local function lim(t) local s = {}; for i = 1, math.min(#t, 30) do s[i] = t[i] end; return table.concat(s, ", ") end
  local broad = {"road","asphalt","concrete","pavement","tarmac","surface","floor","ground","track","drive","street","lane","highway","dirt","gravel","cobble","brick"}
  local named, seen = {}, {}
  for _, m in ipairs(scenetree.findClassObjects('Material') or {}) do
    local low = string.lower(m)
    for _, w in ipairs(broad) do
      if string.find(low, w, 1, true) then if not seen[m] then seen[m] = true; named[#named + 1] = m end break end
    end
  end
  local ro, seen2 = {}, {}
  local function addRO(cls, fld)
    for _, rn in ipairs(scenetree.findClassObjects(cls) or {}) do
      local o = scenetree.findObject(rn)
      if o then local mn = o:getField(fld, 0); if mn and mn ~= "" and not seen2[mn] then seen2[mn] = true; ro[#ro + 1] = mn end end
    end
  end
  addRO('DecalRoad', 'Material'); addRO('MeshRoad', 'topMaterial')
  local picked = {}; for k in pairs(collectRoadMats()) do picked[#picked + 1] = k end
  print("NAMED(" .. #named .. "): " .. lim(named))
  print("ROADOBJ(" .. #ro .. "): " .. lim(ro))
  print("PICKED(" .. #picked .. "): " .. lim(picked))
end

M.setPreset = setPreset
M.setCustom = setCustom
M.setTimeOfDay = setTimeOfDay
M.setForecastWeights = setForecastWeights
M.reset = reset
M.roadScan = roadScan
M.startForecast = startForecast
M.stopForecast = stopForecast
M.setTick = setTick
M.tune = tune
M.setLookAll = setLookAll
M.getLook = getLook
M.diag = diag
M.onUpdate = onUpdate
M.onClientStartMission = onClientStartMission

if math.randomseed and os and os.time then
  math.randomseed(os.time()); for _ = 1, 16 do math.random() end
end

return M
