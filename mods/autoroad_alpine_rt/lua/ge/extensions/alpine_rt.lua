-- Alpine Roadtrip: dwell portals + session persist across core_levels.startLevel.
-- Load via scripts/modScript.lua (manual unload). Do not put this in mainLevel.lua.
-- Internal extension id is "alpine_rt".

local M = {}

local SESSION_PATH = "settings/alpine_rt/session.json"
local PORTALS_PATH = "/lua/ge/extensions/alpine_rt/portals.json"
-- Bump this when Lua changes; shown on load so a stale in-memory copy is obvious.
local LUA_REV = "2026-09-22a"
-- 24x: 1 real hour = 1 game day (radio hour = 150 s). Weather tick stays UDW's real-time clock.
local DAY_LENGTH_S = 3600

local portals = { grace_s = 8, gates = {} }
local dwellGate = nil
local dwellAcc = 0
local lastMsgS = -1
local graceUntil = 0
local switchQueued = nil
local worldReady = false
local restoreArmed = false
local restoreTries = 0
local udwResumeLeft = nil
-- UV Rotate Animation on the rotor material; Lua spin stays off.
local FAN_RPM = 0
local FAN_RAD_PER_S = FAN_RPM * math.pi / 30
local fanRotors = nil
local fanAngle = 0

local function now()
  return os.clock()
end

local function currentLevel()
  local id
  if getCurrentLevelIdentifier then
    id = getCurrentLevelIdentifier()
  end
  if type(id) ~= "string" or id == "" then
    return nil
  end
  id = id:gsub("\\", "/"):gsub("^/levels/", ""):gsub("/+$", "")
  local slash = id:find("/")
  if slash then
    id = id:sub(1, slash - 1)
  end
  return id
end

local function jsonClone(v)
  if v == nil then
    return nil
  end
  local ok, encoded = pcall(jsonEncode, v)
  if not ok or not encoded then
    return nil
  end
  local ok2, decoded = pcall(jsonDecode, encoded)
  if not ok2 then
    return nil
  end
  return decoded
end

local function uiMsg(text, ttl)
  ttl = ttl or 1
  if ui_message then
    ui_message(text, ttl, "info")
  elseif guihooks then
    guihooks.trigger("Message", { ttl = ttl, msg = text, category = "alpine_rt" })
  end
end

local function loadPortals()
  local data = jsonReadFile(PORTALS_PATH)
  if type(data) == "table" and type(data.gates) == "table" then
    portals = data
    log("I", "alpine_rt", "Loaded " .. tostring(#portals.gates) .. " gates from " .. PORTALS_PATH)
  else
    log("W", "alpine_rt", "No portals.json at " .. PORTALS_PATH)
    portals = { grace_s = 8, gates = {} }
  end
end

local function readSession()
  local data = jsonReadFile(SESSION_PATH)
  if type(data) == "table" then
    return data
  end
  return nil
end

local function writeSession(data)
  jsonWriteFile(SESSION_PATH, data, true)
end

local function isAlpineRoadtripLevel(level)
  level = level or currentLevel()
  if not level then
    return false
  end
  for _, g in ipairs(portals.gates or {}) do
    if g.from_level == level or g.to_level == level then
      return true
    end
  end
  return false
end

local function envTime(env)
  if type(env) ~= "table" then
    return nil
  end
  if type(env.time) == "number" then
    return env.time
  end
  if type(env.timeOfDay) == "table" and type(env.timeOfDay.time) == "number" then
    return env.timeOfDay.time
  end
  return nil
end

-- Sun clock only. Do not push fog/clouds via setState: jbWeather's onClientStartMission
-- resets to clear, and core_environment.setState retriggers the map default sky.
local function applyClock(env)
  if not (core_environment and core_environment.getTimeOfDay and core_environment.setTimeOfDay) then
    return
  end
  local tod = core_environment.getTimeOfDay() or {}
  local t = envTime(env)
  if t == nil and core_environment.getState then
    t = envTime(core_environment.getState())
  end
  if t ~= nil then
    tod.time = t
  end
  tod.play = true
  tod.dayLength = DAY_LENGTH_S
  tod.dayScale = 1
  tod.nightScale = 1
  pcall(function()
    core_environment.setTimeOfDay(tod)
  end)
end

local function ensureJbWeather()
  if not (extensions and extensions.load) then
    return nil
  end
  if not (extensions.jbWeather and extensions.jbWeather.getForecast) then
    pcall(extensions.load, "jbWeather")
  end
  if extensions.jbWeather and extensions.jbWeather.getForecast then
    return extensions.jbWeather
  end
  return nil
end

local function snapshotUdw()
  local jbw = ensureJbWeather()
  if not jbw then
    return nil
  end
  local fc
  local ok, data = pcall(jbw.getForecast)
  if ok and type(data) == "table" and data.current then
    fc = jsonClone(data) or data
  end
  if type(fc) ~= "table" then
    return nil
  end
  if jbw.getLook then
    local okL, look = pcall(jbw.getLook)
    if okL and type(look) == "table" then
      fc.look = jsonClone(look) or look
    end
  end
  return fc
end

-- jbWeather has no setState. onClientStartMission wipes to clear; we re-apply the last
-- preset and delay startForecast until nextIn so the portal does not skip a weather step.
local function applyUdw(st)
  udwResumeLeft = nil
  if type(st) ~= "table" or not st.current then
    return
  end
  local jbw = ensureJbWeather()
  if not jbw then
    log("W", "alpine_rt", "jbWeather not loaded, skip UDW restore")
    return
  end
  if st.look and jbw.setLookAll then
    pcall(jbw.setLookAll, st.look.dark, st.look.cloud, st.look.fog, st.look.grey)
  end
  if st.current == "custom" then
    log("I", "alpine_rt", "UDW was custom; sliders are not in getForecast, skip")
  elseif jbw.setPreset then
    pcall(jbw.setPreset, st.current, 1)
  end
  if st.tickInterval and jbw.setTick then
    pcall(jbw.setTick, st.tickInterval)
  end
  if st.forecastOn then
    local wait = tonumber(st.nextIn)
    if wait == nil then
      wait = tonumber(st.tickInterval) or 180
    end
    udwResumeLeft = math.max(0.05, wait)
  end
  log("I", "alpine_rt", "UDW restore " .. tostring(st.current)
    .. (st.forecastOn and (" forecast in " .. tostring(udwResumeLeft) .. "s") or ""))
end

local function tickUdwResume(dtReal)
  if not udwResumeLeft then
    return
  end
  udwResumeLeft = udwResumeLeft - (dtReal or 0)
  if udwResumeLeft > 0 then
    return
  end
  udwResumeLeft = nil
  local jbw = ensureJbWeather()
  if not (jbw and jbw.startForecast) then
    return
  end
  local tick = 180
  local session = readSession()
  if session and session.udw and session.udw.tickInterval then
    tick = session.udw.tickInterval
  end
  pcall(jbw.startForecast, tick)
  log("I", "alpine_rt", "UDW forecast resumed")
end

local function snapshotEnvironment()
  local env
  if core_environment and core_environment.getState then
    env = jsonClone(core_environment.getState())
  elseif core_environment and core_environment.getTimeOfDay then
    env = jsonClone(core_environment.getTimeOfDay())
  end
  return { environment = env }
end

local function playerVehicle()
  if not be then
    return nil
  end
  local ok, veh = pcall(function()
    return be:getPlayerVehicle(0)
  end)
  if ok then
    return veh
  end
  return nil
end

local function snapshotVehicle()
  local veh = playerVehicle()
  if not veh then
    return nil
  end
  local model = veh.jbeam or veh.JBeam
  local config
  if core_vehicle_manager and core_vehicle_manager.getPlayerVehicleData then
    local vd = core_vehicle_manager.getPlayerVehicleData()
    if vd then
      config = jsonClone(vd.config)
      if not model and vd.config and vd.config.model then
        model = vd.config.model
      end
    end
  end
  if not config then
    local pc = veh.partConfig
    if type(pc) == "string" or type(pc) == "table" then
      config = jsonClone(pc) or pc
    end
  end
  if not model then
    return nil
  end
  return { model = model, config = config }
end

local function yawToQuat(tx, ty)
  local dx, dy = tonumber(tx) or 1, tonumber(ty) or 0
  local n = math.sqrt(dx * dx + dy * dy)
  if n < 1e-6 then
    dx, dy = 1, 0
  else
    dx, dy = dx / n, dy / n
  end
  if quatFromDir then
    return quatFromDir(vec3(dx, dy, 0), vec3(0, 0, 1))
  end
  local yaw = math.atan2(dx, dy)
  local half = yaw * 0.5
  return quat(0, 0, math.sin(half), math.cos(half))
end

local function teleportToArrive(arrive)
  local veh = playerVehicle()
  if not veh or not arrive or not arrive.pos then
    return
  end
  local p = arrive.pos
  local pos = vec3(p[1], p[2], p[3])
  local t = arrive.tangent or { 1, 0 }
  local rot = yawToQuat(t[1], t[2])
  if spawn and spawn.safeTeleport then
    spawn.safeTeleport(veh, pos, rot)
  else
    veh:setPositionRotation(pos.x, pos.y, pos.z, rot.x, rot.y, rot.z, rot.w)
  end
end

local function gatesForLevel(level)
  local out = {}
  if not level then
    return out
  end
  for _, g in ipairs(portals.gates or {}) do
    if g.from_level == level then
      table.insert(out, g)
    end
  end
  return out
end

local function pointInBox(px, py, pz, box)
  if not box or not box.pos or not box.size then
    return false
  end
  local c = box.pos
  local s = box.size
  local t = box.tangent or { 1, 0 }
  local fx, fy = t[1], t[2]
  local n = math.sqrt(fx * fx + fy * fy)
  if n < 1e-6 then
    fx, fy = 1, 0
  else
    fx, fy = fx / n, fy / n
  end
  local rx, ry = -fy, fx
  local dx, dy, dz = px - c[1], py - c[2], pz - c[3]
  local along = dx * fx + dy * fy
  local across = dx * rx + dy * ry
  return math.abs(along) <= s[1] * 0.5
    and math.abs(across) <= s[2] * 0.5
    and math.abs(dz) <= (s[3] * 0.5 + 4)
end

local function playerInGate(gate)
  local veh = playerVehicle()
  if not veh then
    return false
  end
  local p = veh:getPosition()
  return pointInBox(p.x, p.y, p.z, gate.box)
end

local function drawGateArc(gate)
  local arc = gate and gate.arc
  if not arc or type(arc.points) ~= "table" or #arc.points < 2 then
    return
  end
  if not debugDrawer then
    return
  end
  local col = ColorF(1.0, 0.35, 0.22, 1.0)
  local colDim = ColorF(1.0, 0.35, 0.22, 0.35)
  local w = tonumber(arc.width_m) or 20
  local hw = w * 0.5
  for i = 1, #arc.points - 1 do
    local a = arc.points[i]
    local b = arc.points[i + 1]
    if a and b then
      local ax, ay, az = a[1], a[2], a[3]
      local bx, by, bz = b[1], b[2], b[3]
      pcall(function()
        debugDrawer:drawLine(vec3(ax, ay, az), vec3(bx, by, bz), col)
      end)
      -- 20 m wide: two edge lines
      local tx, ty = bx - ax, by - ay
      local len = math.sqrt(tx * tx + ty * ty)
      if len > 1e-3 then
        local rx, ry = -ty / len * hw, tx / len * hw
        pcall(function()
          debugDrawer:drawLine(vec3(ax - rx, ay - ry, az), vec3(bx - rx, by - ry, bz), colDim)
          debugDrawer:drawLine(vec3(ax + rx, ay + ry, az), vec3(bx + rx, by + ry, bz), colDim)
        end)
      end
    end
  end
end

local liveArcId = nil
local liveArcGateId = nil

local function hideAllArcs()
  if liveArcId then
    local obj = scenetree.findObjectById and scenetree.findObjectById(liveArcId)
    if obj then
      pcall(function()
        obj:delete()
      end)
    end
  end
  local leftover = scenetree.findObject and scenetree.findObject("alpine_rt_arc_live")
  if leftover then
    pcall(function()
      leftover:delete()
    end)
  end
  liveArcId = nil
  liveArcGateId = nil
end

local function ensureLiveArc(gate)
  if not gate or not createObject then
    return false
  end
  if liveArcGateId == gate.id and liveArcId then
    local existing = scenetree.findObjectById and scenetree.findObjectById(liveArcId)
    if existing then
      existing.hidden = false
      existing.isRenderEnabled = true
      return true
    end
  end
  hideAllArcs()
  local pts = gate.arc and gate.arc.points
  if not pts or not pts[1] then
    return false
  end
  local level = currentLevel()
  if not level then
    return false
  end
  local shape = string.format(
    "/levels/%s/art/shapes/portals/portal_arc_%s.dae",
    level,
    tostring(gate.id)
  )
  local obj = createObject("TSStatic")
  if not obj then
    log("E", "alpine_rt", "createObject TSStatic failed")
    return false
  end
  obj.canSave = false
  obj:setField("shapeName", 0, shape)
  obj.shapeName = shape
  obj:setField("collisionType", 0, "None")
  obj:setField("decalType", 0, "None")
  obj:setField("castShadows", 0, "0")
  obj:setPosition(vec3(pts[1][1], pts[1][2], pts[1][3]))
  obj.scale = vec3(1, 1, 1)
  obj.hidden = false
  obj.isRenderEnabled = true
  obj:registerObject("alpine_rt_arc_live")
  local grp = scenetree.findObject and (
    scenetree.findObject("alpine_rt_portals") or scenetree.findObject("MissionGroup")
  )
  if grp and grp.addObject then
    pcall(function()
      grp:addObject(obj)
    end)
  end
  liveArcId = obj:getId()
  liveArcGateId = gate.id
  log("I", "alpine_rt", "Spawned arc " .. shape)
  return true
end

local function ensureSession(session)
  session = session or readSession() or {}
  if not session.session_id then
    session.session_id = tostring(os.time()) .. "-" .. tostring(math.random(10000, 99999))
    session.started_at = os.date("!%Y-%m-%dT%H:%M:%SZ")
    session.segments = session.segments or {}
    session.traffic = session.traffic or { epoch = os.time(), seed = math.random(100000, 999999) }
  end
  return session
end

local function ensureTraffic()
  local session = ensureSession()
  if type(session.traffic) ~= "table" then
    session.traffic = { epoch = os.time(), seed = math.random(100000, 999999) }
    writeSession(session)
    return session.traffic.epoch, session.traffic.seed, session
  end
  local epoch = tonumber(session.traffic.epoch)
  local seed = tonumber(session.traffic.seed)
  local changed = false
  if not epoch then
    epoch = os.time()
    session.traffic.epoch = epoch
    changed = true
  end
  if not seed then
    seed = math.random(100000, 999999)
    session.traffic.seed = seed
    changed = true
  end
  if changed then
    writeSession(session)
  end
  return epoch, seed, session
end

local function segDensity(segId, tSec, seed)
  local h = 0
  local text = tostring(segId or "")
  for i = 1, #text do
    h = (h * 131 + string.byte(text, i)) % 2147483647
  end
  local x = (h + (seed or 0) * 1013) % 2147483647
  local a = (x % 10000) / 10000.0
  local b = ((math.floor(x / 10000) % 10000)) / 10000.0
  local p1 = math.sin((tSec or 0) * (0.0009 + 0.0022 * a) + 6.28318 * b)
  local p2 = math.sin((tSec or 0) * (0.0041 + 0.0011 * b) + 6.28318 * a)
  local v = 0.52 + 0.30 * p1 + 0.18 * p2
  if v < 0 then
    v = 0
  elseif v > 1 then
    v = 1
  end
  return v
end

local function getTrafficMap()
  local epoch, seed = ensureTraffic()
  local t = os.time() - (epoch or os.time())
  local level = currentLevel()
  local segs = {}
  for _, g in ipairs(portals.gates or {}) do
    local arc = g.arc
    if arc and type(arc.points) == "table" and #arc.points >= 2 then
      local id = tostring(g.id or "")
      segs[#segs + 1] = {
        id = id,
        label = g.label or id,
        from_level = g.from_level,
        to_level = g.to_level,
        points = arc.points,
        density = segDensity(id, t, seed),
      }
    end
  end
  return {
    product = "Alpine Roadtrip",
    short = "alpine-rt",
    lua_rev = LUA_REV,
    level = level,
    now_s = os.time(),
    t_s = t,
    segments = segs,
  }
end

local function queueSwitch(gate)
  local settings = snapshotEnvironment()
  local vehicle = snapshotVehicle()
  local session = ensureSession()
  session.settings = settings
  session.udw = snapshotUdw()
  session.world = { clock_rate = 24, day_length_s = DAY_LENGTH_S }
  session.vehicle = vehicle
  session.pending_restore = {
    to_level = gate.to_level,
    from_gate = gate.id,
    arrive = gate.arrive,
  }
  writeSession(session)
  log("I", "alpine_rt", "Queued switch " .. tostring(gate.from_level) .. " -> " .. tostring(gate.to_level))
  switchQueued = {
    to_level = gate.to_level,
    vehicle = vehicle,
  }
end

local function doQueuedSwitch()
  local q = switchQueued
  switchQueued = nil
  if not q then
    return
  end
  local path = "/levels/" .. q.to_level .. "/main/"
  local spawnArg
  if q.vehicle and q.vehicle.model then
    spawnArg = { q.vehicle.model, { config = q.vehicle.config } }
  end
  log("I", "alpine_rt", "startLevel " .. path)
  -- next frame / after JSON flush; not from a trigger callback
  if core_levels and core_levels.startLevel then
    core_levels.startLevel(path, false, nil, spawnArg)
  else
    log("E", "alpine_rt", "core_levels.startLevel missing")
  end
end

local function applyPendingRestore()
  local session = readSession()
  if not session or not session.pending_restore then
    if isAlpineRoadtripLevel() then
      applyClock(session and session.settings and session.settings.environment)
      applyUdw(session and session.udw)
    end
    restoreArmed = false
    restoreTries = 0
    return true
  end
  if not playerVehicle() then
    restoreTries = restoreTries + 1
    return false
  end
  local pending = session.pending_restore
  local level = currentLevel()
  if pending.to_level and level and pending.to_level ~= level then
    log("W", "alpine_rt", "pending_restore for " .. tostring(pending.to_level) .. " but on " .. tostring(level))
    restoreArmed = false
    return true
  end
  applyClock(session.settings and session.settings.environment)
  if session.vehicle and session.vehicle.model then
    local veh = playerVehicle()
    local needReplace = true
    if veh and veh.jbeam == session.vehicle.model then
      needReplace = false
    end
    if needReplace and core_vehicles and core_vehicles.replaceVehicle then
      core_vehicles.replaceVehicle(session.vehicle.model, { config = session.vehicle.config })
    end
  end
  teleportToArrive(pending.arrive)
  applyUdw(session.udw)
  session.pending_restore = nil
  writeSession(session)
  local grace = tonumber(portals.grace_s) or 8
  graceUntil = now() + grace
  dwellGate = nil
  dwellAcc = 0
  restoreArmed = false
  restoreTries = 0
  uiMsg("Alpine Roadtrip: map loaded", 3)
  log("I", "alpine_rt", "Restore done, grace " .. tostring(grace) .. "s")
  return true
end

local function col3(mat, i)
  local c = mat:getColumn(i)
  return vec3(c.x, c.y, c.z)
end

local function collectFanRotors()
  fanRotors = {}
  fanAngle = 0
  if not scenetree or not scenetree.findClassObjects then
    return
  end
  local names = scenetree.findClassObjects("TSStatic") or {}
  for _, name in ipairs(names) do
    if type(name) == "string" and name:find("__fanrotor_", 1, true) then
      local obj = scenetree.findObject(name)
      if obj and obj.getTransform then
        local t = obj:getTransform()
        local shape = tostring(obj.shapeName or obj:getField("shapeName", 0) or "")
        local dir = 1
        if shape:find("ccw", 1, true) then
          dir = -1
        end
        fanRotors[#fanRotors + 1] = {
          id = obj:getId(),
          pos = obj:getPosition(),
          x = col3(t, 0),
          y = col3(t, 1),
          z = col3(t, 2),
          dir = dir,
        }
      end
    end
  end
  log("I", "alpine_rt", "fan rotors " .. tostring(#fanRotors) .. " @ " .. tostring(FAN_RPM) .. " rpm")
end

local function tickFanRotors(dt)
  if FAN_RPM == 0 then
    return
  end
  if fanRotors == nil then
    collectFanRotors()
  end
  if not fanRotors or #fanRotors == 0 then
    return
  end
  fanAngle = fanAngle + (dt or 0) * FAN_RAD_PER_S
  local c = math.cos(fanAngle)
  local s = math.sin(fanAngle)
  for i = 1, #fanRotors do
    local f = fanRotors[i]
    local obj = scenetree.findObjectById and scenetree.findObjectById(f.id)
    if obj and obj.setTransform then
      local sd = s * f.dir
      local y2 = f.y * c + f.z * sd
      local z2 = f.z * c - f.y * sd
      local mat = MatrixF(true)
      mat:setColumn(0, f.x)
      mat:setColumn(1, y2)
      mat:setColumn(2, z2)
      mat:setColumn(3, f.pos)
      obj:setTransform(mat)
    end
  end
end

local function onExtensionLoaded()
  loadPortals()
  math.randomseed(os.time())
  hideAllArcs()
  ensureJbWeather()
  log("I", "alpine_rt", "extension loaded " .. LUA_REV)
  uiMsg("Alpine Roadtrip Lua " .. LUA_REV, 4)
end

local function onWorldReadyState(state)
  worldReady = (state == 2)
  if state == 2 then
    loadPortals()
    hideAllArcs()
    fanRotors = nil
    restoreArmed = true
    restoreTries = 0
  end
end

local function onClientEndMission()
  local session = readSession()
  if session then
    session.settings = snapshotEnvironment()
    session.udw = snapshotUdw()
    session.world = { clock_rate = 24, day_length_s = DAY_LENGTH_S }
    writeSession(session)
  end
  worldReady = false
  dwellGate = nil
  dwellAcc = 0
  udwResumeLeft = nil
  fanRotors = nil
  hideAllArcs()
end

local function onUpdate(dtReal)
  if switchQueued then
    doQueuedSwitch()
    return
  end
  if restoreArmed then
    hideAllArcs()
    if applyPendingRestore() or restoreTries > 180 then
      if restoreTries > 180 then
        log("W", "alpine_rt", "Restore timed out waiting for player vehicle")
      end
      restoreArmed = false
    end
    return
  end
  if not worldReady then
    return
  end
  tickFanRotors(dtReal)
  tickUdwResume(dtReal)
  local level = currentLevel()
  if not level then
    return
  end
  if graceUntil > now() then
    hideAllArcs()
    return
  end
  local veh = playerVehicle()
  if not veh then
    dwellGate = nil
    dwellAcc = 0
    hideAllArcs()
    return
  end

  local inside = nil
  for _, g in ipairs(gatesForLevel(level)) do
    if playerInGate(g) then
      inside = g
      break
    end
  end

  if not inside then
    if dwellGate then
      uiMsg("Left portal", 1)
    end
    hideAllArcs()
    dwellGate = nil
    dwellAcc = 0
    lastMsgS = -1
    return
  end

  if not dwellGate or dwellGate.id ~= inside.id then
    dwellGate = inside
    dwellAcc = 0
    lastMsgS = -1
    local km0 = 0
    if inside.arc then
      km0 = tonumber(inside.arc.geo_distance_m) or tonumber(inside.arc.length_m) or 0
    end
    local need0 = km0 > 0 and (km0 / 1000) or (tonumber(inside.dwell_s) or 5)
    log("I", "alpine_rt", string.format(
      "enter %s need=%.1fs geo=%.0f dwell_s=%s rev=%s",
      tostring(inside.id),
      need0,
      km0,
      tostring(inside.dwell_s),
      LUA_REV
    ))
  end
  ensureLiveArc(inside)
  drawGateArc(inside)

  dwellAcc = dwellAcc + (dtReal or 0)
  local km = 0
  if inside.arc then
    km = tonumber(inside.arc.geo_distance_m) or tonumber(inside.arc.length_m) or 0
  end
  local need = km > 0 and (km / 1000) or (tonumber(inside.dwell_s) or 5)
  local remain = math.max(0, need - dwellAcc)
  local sec = math.ceil(remain)
  if sec ~= lastMsgS then
    lastMsgS = sec
    local label = inside.label or inside.id
    if remain > 0.05 then
      uiMsg(string.format("%s — switching in %ds", label, sec), 1.05)
    end
  end

  if dwellAcc >= need then
    dwellGate = nil
    dwellAcc = 0
    hideAllArcs()
    uiMsg((inside.label or "Portal") .. " — loading map", 2)
    queueSwitch(inside)
  end
end

local function onSerialize()
  return {
    dwellAcc = dwellAcc,
    dwellId = dwellGate and dwellGate.id or nil,
    graceUntil = graceUntil,
  }
end

local function onDeserialized(data)
  loadPortals()
  hideAllArcs()
  if type(data) == "table" then
    dwellAcc = data.dwellAcc or 0
    graceUntil = data.graceUntil or 0
  end
end

M.onExtensionLoaded = onExtensionLoaded
M.onWorldReadyState = onWorldReadyState
M.onClientEndMission = onClientEndMission
M.onUpdate = onUpdate
M.onSerialize = onSerialize
M.onDeserialized = onDeserialized

M.getTrafficMap = getTrafficMap

return M
