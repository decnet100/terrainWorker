// Dynamic Weather -- control panel for the jbWeather GE engine.
// Talks to the engine via bngApi.engineLua (setPreset / setCustom / startForecast / stopForecast / setTick /
// setTimeOfDay / setForecastWeights / reset / getForecast) and mirrors live state pushed back on the
// 'jbWeatherState' guihook. Styling is scoped under .jbw. Look tuned to BeamNG's own menus: frosted graphite,
// orange accent, bold-italic headers, collapsible sections.
angular.module('beamng.apps').directive('jbweather', [function () {
  var LABEL = { clear: 'Clear', overcast: 'Overcast', fog: 'Fog', drizzle: 'Drizzle', rain: 'Rain', thunderstorm: 'Thunderstorm', heavyThunderstorm: 'Heavy Storm', custom: 'Custom' };
  var SHORT = { clear: 'CLR', overcast: 'OVC', fog: 'FOG', drizzle: 'DRZ', rain: 'RAIN', thunderstorm: 'STORM', heavyThunderstorm: 'SEVERE', custom: 'CUSTOM' };
  // severity ramp: calm green -> cloud greys -> rain blues -> storm amber -> severe red; custom = orange
  var COLOR = { clear: '#6cc06c', overcast: '#9aa3ae', fog: '#bcc4cd', drizzle: '#5bbcd6', rain: '#4a8fd6', thunderstorm: '#e6923a', heavyThunderstorm: '#d94f43', custom: '#ff8a4c' };

  var tpl = ''
    + '<div class="jbw">'
    + '  <style>'
    + '  .jbw{font-family:"Cairo","Overpass",system-ui,sans-serif;color:#dfe2e6;background:rgba(18,20,24,0.9);'
    + '       border:1px solid rgba(255,255,255,0.07);border-top:2px solid #ff6600;border-radius:5px;'
    + '       height:auto;max-height:100%;box-sizing:border-box;display:flex;flex-direction:column;'
    + '       overflow-y:auto;overflow-x:hidden;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);}'
    + '  .jbw *{box-sizing:border-box;}'
    + '  .jbw-head{display:flex;align-items:center;justify-content:space-between;padding:10px 11px;'
    + '            border-bottom:1px solid rgba(255,255,255,0.06);}'
    + '  .jbw-brand{display:flex;align-items:center;min-width:0;}'
    + '  .jbw-slash{width:6px;height:16px;background:#ff6600;transform:skewX(-16deg);margin-right:9px;flex:none;}'
    + '  .jbw-title{font-family:"Cairo","Overpass",sans-serif;font-style:italic;font-weight:800;font-size:13px;letter-spacing:.3px;color:#f4f6f8;white-space:nowrap;}'
    + '  .jbw-headr{display:flex;align-items:center;gap:10px;flex:none;}'
    + '  .jbw-now{font-weight:700;font-size:13px;color:#eef0f2;white-space:nowrap;}'
    + '  .jbw-min{font-family:inherit;width:21px;height:21px;line-height:18px;text-align:center;border-radius:3px;'
    + '           border:1px solid rgba(255,255,255,0.14);background:transparent;color:#aeb4bb;cursor:pointer;font-size:15px;padding:0;}'
    + '  .jbw-min:hover{border-color:#ff6600;color:#ff6600;}'
    + '  .jbw-body{padding:8px 11px 11px;display:flex;flex-direction:column;}'
    + '  .jbw-sub{display:flex;flex-wrap:wrap;gap:6px 13px;font-size:11px;color:#8b9198;padding:2px 0 6px;}'
    + '  .jbw-sub b{color:#dfe2e6;font-weight:700;}'
    + '  .jbw-sec{border-top:1px solid rgba(255,255,255,0.05);}'
    + '  .jbw-sh{display:flex;align-items:center;justify-content:space-between;padding:8px 0 7px;cursor:pointer;}'
    + '  .jbw-st{font-style:italic;font-weight:700;font-size:13px;color:#c8ccd1;}'
    + '  .jbw-sh:hover .jbw-st{color:#fff;}'
    + '  .jbw-cv{color:#6d7278;font-size:16px;line-height:1;transition:transform .15s,color .15s;}'
    + '  .jbw-cv.o{transform:rotate(90deg);color:#ff6600;}'
    + '  .jbw-sb{display:flex;flex-direction:column;gap:7px;padding:1px 0 10px;}'
    + '  .jbw-hint{font-size:11px;color:#7a8088;margin:-2px 0 2px;}'
    + '  .jbw-next{color:#ff8c3a;font-size:11px;font-weight:600;}'
    + '  .jbw-fcrow{display:flex;gap:4px;}'
    + '  .jbw-chip{flex:1;text-align:center;font-size:10px;font-weight:700;padding:6px 0;border-radius:3px;'
    + '            background:rgba(255,255,255,0.06);color:#aeb4bb;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}'
    + '  .jbw-chip.first{background:#ff6600;color:#160d05;}'
    + '  .jbw-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;}'
    + '  .jbw-preset{font-family:inherit;text-align:center;font-size:12px;font-weight:600;color:#dfe2e6;'
    + '              background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.09);'
    + '              border-radius:3px;padding:8px 9px;cursor:pointer;transition:all .12s;}'
    + '  .jbw-preset:hover{background:rgba(255,255,255,0.11);border-color:rgba(255,255,255,0.18);}'
    + '  .jbw-preset.active{background:#ff6600;border-color:#ff6600;color:#160d05;font-weight:700;}'
    + '  .jbw-row{display:flex;align-items:center;gap:8px;margin-top:1px;}'
    + '  .jbw-btn{font-family:inherit;font-size:11px;font-weight:700;letter-spacing:.5px;border-radius:3px;padding:9px 0;'
    + '           cursor:pointer;border:1px solid rgba(255,255,255,0.11);background:rgba(255,255,255,0.06);color:#dfe2e6;flex:1;transition:all .12s;}'
    + '  .jbw-btn:hover{background:rgba(255,255,255,0.12);}'
    + '  .jbw-btn.on{background:#ff6600;border-color:#ff6600;color:#160d05;}'
    + '  .jbw-sl{display:flex;align-items:center;gap:8px;font-size:11px;color:#90969c;}'
    + '  .jbw-sl span.v{color:#dfe2e6;min-width:40px;text-align:right;font-variant-numeric:tabular-nums;}'
    + '  .jbw-sl span.cl{min-width:56px;color:#90969c;}'
    + '  .jbw-sl input[type=range]{-webkit-appearance:none;flex:1;height:4px;background:#3a3f45;border-radius:2px;outline:none;}'
    + '  .jbw-sl input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:8px;height:16px;border-radius:1px;background:#ff6600;transform:skewX(-18deg);cursor:pointer;box-shadow:0 0 3px rgba(0,0,0,0.45);}'
    + '  </style>'

    + '  <div class="jbw-head">'
    + '    <div class="jbw-brand"><span class="jbw-slash"></span><span class="jbw-title">DYNAMIC WEATHER</span></div>'
    + '    <div class="jbw-headr">'
    + '      <span class="jbw-now">{{condLabel(state.current)}}</span>'
    + '      <button class="jbw-min" ng-click="min=!min" title="collapse">{{min ? \'+\' : \'\\u2013\'}}</button>'
    + '    </div></div>'

    + '  <div class="jbw-body" ng-show="!min">'
    + '    <div class="jbw-sub"><span ng-if="state.tempC != null">Temp <b>{{state.tempC}}&deg;C</b></span>'
    + '         <span>Grip <b>{{state.grip}}%</b></span>'
    + '         <span ng-if="state.wetness > 0">Wet <b>{{state.wetness}}%</b></span>'
    + '         <span>Rain <b>{{state.raining ? \'yes\' : \'no\'}}</b></span>'
    + '         <span ng-if="state.windMph > 1">Wind <b>{{state.windMph}} mph</b></span>'
    + '         <span ng-if="state.transitioning" style="color:#ff8c3a">changing...</span></div>'

    // ---- PRESETS (open by default) ----
    + '    <div class="jbw-sec"><div class="jbw-sh" ng-click="sec.presets=!sec.presets">'
    + '         <span class="jbw-st">Presets</span><span class="jbw-cv" ng-class="{o:sec.presets}">&#8250;</span></div>'
    + '      <div class="jbw-sb" ng-show="sec.presets">'
    + '        <div class="jbw-grid"><button class="jbw-preset" ng-repeat="p in state.presets" ng-class="{active:state.current===p}"'
    + '             ng-click="pick(p)">{{condLabel(p)}}</button></div></div></div>'

    // ---- CUSTOM ----
    + '    <div class="jbw-sec"><div class="jbw-sh" ng-click="sec.custom=!sec.custom">'
    + '         <span class="jbw-st" ng-style="{color:state.current===\'custom\'?\'#ff8c3a\':\'\'}">Custom</span><span class="jbw-cv" ng-class="{o:sec.custom}">&#8250;</span></div>'
    + '      <div class="jbw-sb" ng-show="sec.custom">'
    + '        <div class="jbw-hint">Build your own conditions</div>'
    + '        <div class="jbw-sl"><span class="cl">Rain</span><input type="range" min="0" max="100" ng-model="cust.rain" ng-change="applyCustom()"><span class="v">{{cust.rain}}%</span></div>'
    + '        <div class="jbw-sl"><span class="cl">Fog</span><input type="range" min="0" max="100" ng-model="cust.fog" ng-change="applyCustom()"><span class="v">{{cust.fog}}%</span></div>'
    + '        <div class="jbw-sl"><span class="cl">Cloud</span><input type="range" min="0" max="100" ng-model="cust.cloud" ng-change="applyCustom()"><span class="v">{{cust.cloud}}%</span></div>'
    + '        <div class="jbw-sl"><span class="cl">Thunder</span><input type="range" min="0" max="100" ng-model="cust.thunder" ng-change="applyCustom()"><span class="v">{{cust.thunder}}%</span></div>'
    + '        <div class="jbw-sl"><span class="cl">Wind</span><input type="range" min="0" max="100" ng-model="cust.wind" ng-change="applyCustom()"><span class="v">{{cust.wind}}%</span></div>'
    + '        <div class="jbw-row"><button class="jbw-btn" ng-class="{on:state.current===\'custom\'}" ng-click="applyCustom()">'
    + '             {{state.current===\'custom\' ? \'CUSTOM ACTIVE\' : \'ENABLE CUSTOM\'}}</button></div></div></div>'

    // ---- FORECAST ----
    + '    <div class="jbw-sec"><div class="jbw-sh" ng-click="sec.forecast=!sec.forecast">'
    + '         <span class="jbw-st">Forecast</span><span class="jbw-cv" ng-class="{o:sec.forecast}">&#8250;</span></div>'
    + '      <div class="jbw-sb" ng-show="sec.forecast">'
    + '        <div class="jbw-fcrow">'
    + '          <div class="jbw-chip" ng-class="{first:$index===0}" ng-repeat="p in state.upcoming track by $index">{{condShort(p)}}</div>'
    + '          <div class="jbw-chip" ng-if="!state.upcoming.length">{{state.forecastOn ? \'next...\' : \'forecast off\'}}</div></div>'
    + '        <div class="jbw-hint" ng-if="state.forecastOn"><span class="jbw-next">next in {{fmt(state.nextIn)}}</span></div>'
    + '        <div class="jbw-row"><button class="jbw-btn" ng-class="{on:state.forecastOn}" ng-click="toggleForecast()">'
    + '             {{state.forecastOn ? \'FORECAST ON\' : \'START FORECAST\'}}</button></div>'
    + '        <div class="jbw-sl"><span class="cl">Tick</span><input type="range" min="20" max="600" step="10" ng-model="tick" ng-change="applyTick()"><span class="v">{{fmt(tick)}}</span></div></div></div>'

    // ---- FORECAST MIX ----
    + '    <div class="jbw-sec"><div class="jbw-sh" ng-click="sec.mix=!sec.mix">'
    + '         <span class="jbw-st">Forecast Mix</span><span class="jbw-cv" ng-class="{o:sec.mix}">&#8250;</span></div>'
    + '      <div class="jbw-sb" ng-show="sec.mix">'
    + '        <div class="jbw-hint">How often each appears in the forecast</div>'
    + '        <div class="jbw-sl"><span class="cl">Clear</span><input type="range" min="0" max="200" step="5" ng-model="mix.clear" ng-change="applyMix()"><span class="v">{{mix.clear}}%</span></div>'
    + '        <div class="jbw-sl"><span class="cl">Overcast</span><input type="range" min="0" max="200" step="5" ng-model="mix.overcast" ng-change="applyMix()"><span class="v">{{mix.overcast}}%</span></div>'
    + '        <div class="jbw-sl"><span class="cl">Fog</span><input type="range" min="0" max="200" step="5" ng-model="mix.fog" ng-change="applyMix()"><span class="v">{{mix.fog}}%</span></div>'
    + '        <div class="jbw-sl"><span class="cl">Drizzle</span><input type="range" min="0" max="200" step="5" ng-model="mix.drizzle" ng-change="applyMix()"><span class="v">{{mix.drizzle}}%</span></div>'
    + '        <div class="jbw-sl"><span class="cl">Rain</span><input type="range" min="0" max="200" step="5" ng-model="mix.rain" ng-change="applyMix()"><span class="v">{{mix.rain}}%</span></div>'
    + '        <div class="jbw-sl"><span class="cl">Storm</span><input type="range" min="0" max="200" step="5" ng-model="mix.thunderstorm" ng-change="applyMix()"><span class="v">{{mix.thunderstorm}}%</span></div>'
    + '        <div class="jbw-sl"><span class="cl">Severe</span><input type="range" min="0" max="200" step="5" ng-model="mix.heavyThunderstorm" ng-change="applyMix()"><span class="v">{{mix.heavyThunderstorm}}%</span></div></div></div>'

    // ---- TIME OF DAY ----
    + '    <div class="jbw-sec"><div class="jbw-sh" ng-click="sec.tod=!sec.tod">'
    + '         <span class="jbw-st">Time of Day</span><span class="jbw-cv" ng-class="{o:sec.tod}">&#8250;</span></div>'
    + '      <div class="jbw-sb" ng-show="sec.tod">'
    + '        <div class="jbw-sl"><span class="cl">Time</span><input type="range" min="0" max="24" step="0.25" ng-model="tod" ng-change="applyTod()"><span class="v">{{todFmt()}}</span></div></div></div>'

    // ---- footer: transition speed + clear ----
    + '    <div class="jbw-sec"><div class="jbw-sb" style="padding-top:9px;">'
    + '      <div class="jbw-sl"><span class="cl">Speed</span><input type="range" min="3" max="60" step="1" ng-model="trans"><span class="v">{{trans}}s</span></div>'
    + '      <div class="jbw-row"><button class="jbw-btn" ng-click="clearW()">CLEAR WEATHER</button></div></div></div>'

    + '  </div>'
    + '</div>';

  return {
    template: tpl,
    replace: true,
    restrict: 'EA',
    scope: true,
    link: function (scope) {
      scope.state = { current: 'clear', upcoming: [], forecastOn: false, tickInterval: 180, nextIn: 0, presets: [], grip: 100, raining: false, transitioning: false };
      scope.tick = 180;
      scope.trans = 20;
      scope.min = false;                                          // collapse the whole menu to just the title bar
      scope.tod = 12;
      scope.sec = { presets: true, custom: false, forecast: false, mix: false, tod: false };  // Presets open by default
      scope.cust = { rain: 0, fog: 0, cloud: 0, thunder: 0, wind: 0 };
      scope.mix = { clear: 100, overcast: 100, fog: 100, drizzle: 100, rain: 100, thunderstorm: 100, heavyThunderstorm: 100 };
      // PERSIST the UI state. Switching cameras / opening a menu re-links this directive with a FRESH
      // scope, which was snapping the minimize + slider VALUES back to defaults (the engine kept the real
      // values, so the forecast still ran -- only the display reset). Restore from localStorage on link,
      // and save on any change so the panel remembers what you set.
      try { var _s = JSON.parse(localStorage.getItem('jbw_ui'));
        if (_s) { if (_s.min != null) scope.min = _s.min; if (_s.tick != null) scope.tick = _s.tick;
          if (_s.trans != null) scope.trans = _s.trans; if (_s.tod != null) scope.tod = _s.tod;
          if (_s.mix) scope.mix = _s.mix; if (_s.cust) scope.cust = _s.cust; if (_s.sec) scope.sec = _s.sec; } } catch (e) {}
      scope.$watch(function () {
        return JSON.stringify({ min: scope.min, tick: scope.tick, trans: scope.trans, tod: scope.tod, mix: scope.mix, cust: scope.cust, sec: scope.sec });
      }, function (v) { try { localStorage.setItem('jbw_ui', v); } catch (e) {} });

      // custom builder: debounce so dragging a slider doesn't restart the transition every tick
      var custTimer = null;
      scope.applyCustom = function () {
        if (custTimer) { clearTimeout(custTimer); }
        custTimer = setTimeout(function () {
          var c = scope.cust;
          bngApi.engineLua('extensions.jbWeather.setCustom({rain=' + (c.rain / 100) + ',fog=' + (c.fog / 100)
            + ',cloud=' + (c.cloud / 100) + ',thunder=' + (c.thunder / 100) + ',wind=' + (c.wind / 100) + '}, 3)');
          scope.state.forecastOn = false;
          setTimeout(refresh, 120);
        }, 140);
      };

      // time of day: slider is in hours (12 = noon); convert to the engine's phase (0=noon,0.25=sunset,0.5=midnight)
      scope.applyTod = function () {
        var h = parseFloat(scope.tod) || 0;
        var tv = (((h - 12) / 24) % 1 + 1) % 1;
        bngApi.engineLua('extensions.jbWeather.setTimeOfDay(' + tv + ')');
      };
      scope.todFmt = function () {
        var h = parseFloat(scope.tod) || 0, hh = Math.floor(h), mm = Math.round((h - hh) * 60);
        if (mm === 60) { mm = 0; hh = (hh + 1) % 24; }
        return (hh < 10 ? '0' : '') + hh + ':' + (mm < 10 ? '0' : '') + mm;
      };

      // forecast mix: per-type frequency weights (100% = normal). Debounced.
      var mixTimer = null;
      scope.applyMix = function () {
        if (mixTimer) { clearTimeout(mixTimer); }
        mixTimer = setTimeout(function () {
          var m = scope.mix;
          bngApi.engineLua('extensions.jbWeather.setForecastWeights({clear=' + (m.clear / 100) + ',overcast=' + (m.overcast / 100)
            + ',fog=' + (m.fog / 100) + ',drizzle=' + (m.drizzle / 100) + ',rain=' + (m.rain / 100)
            + ',thunderstorm=' + (m.thunderstorm / 100) + ',heavyThunderstorm=' + (m.heavyThunderstorm / 100) + '})');
        }, 160);
      };

      scope.condLabel = function (p) { return LABEL[p] || p; };
      scope.condShort = function (p) { return SHORT[p] || (p || '').toUpperCase(); };
      scope.condColor = function (p) { return COLOR[p] || '#ff8a4c'; };
      scope.fmt = function (s) { s = Math.max(0, s | 0); var m = Math.floor(s / 60), ss = s % 60; return m + ':' + (ss < 10 ? '0' : '') + ss; };

      function apply(d) {
        if (!d) return;
        scope.$evalAsync(function () {
          scope.state = d;
          if (typeof d.tickInterval === 'number') scope.tick = d.tickInterval;
        });
      }
      function refresh() { bngApi.engineLua('extensions.jbWeather.getForecast()', apply); }

      scope.pick = function (p) {
        // a manual pick takes MANUAL control: stop the auto-forecast first, or it ticks on and overwrites your
        // choice on its next step (the "I select clear but it stays stuck in fog" bug).
        bngApi.engineLua('extensions.jbWeather.stopForecast(); extensions.jbWeather.setPreset(' + JSON.stringify(p) + ', ' + (scope.trans | 0) + ')');
        scope.state.current = p; scope.state.forecastOn = false;
        setTimeout(refresh, 120);
      };
      scope.toggleForecast = function () {
        if (scope.state.forecastOn) { bngApi.engineLua('extensions.jbWeather.stopForecast()'); }
        else { bngApi.engineLua('extensions.jbWeather.startForecast(' + (scope.tick | 0) + ')'); }
        setTimeout(refresh, 120);
      };
      scope.applyTick = function () { bngApi.engineLua('extensions.jbWeather.setTick(' + (scope.tick | 0) + ')'); };
      scope.clearW = function () { bngApi.engineLua('extensions.jbWeather.reset(8)'); setTimeout(refresh, 120); };

      scope.$on('jbWeatherState', function (e, d) { apply(d); });

      // LIGHTNING FLASH: a full-screen white overlay appended to <body> (NOT inside the panel -- the panel's
      // backdrop-filter would trap a fixed child. body has no such ancestor, so this covers the whole screen).
      // The engine fires 'jbWeatherFlash' on each strike; we pop it bright then fade. This lives on the UI
      // layer, so the game's HDR auto-exposure (which ate the in-world flash) can't touch it.
      var flashEl = document.createElement('div');
      flashEl.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:#eef3ff;opacity:0;pointer-events:none;z-index:2147483647;transition:opacity 0.32s ease-out;';
      document.body.appendChild(flashEl);
      var flashT1 = null, flashT2 = null;
      scope.$on('jbWeatherFlash', function (e, lvl) {
        var peak = 0.55 + 0.4 * (parseFloat(lvl) || 0.5);        // heavier storms flash brighter
        flashEl.style.transition = 'none'; flashEl.style.opacity = String(peak);   // instant pop
        if (flashT1) clearTimeout(flashT1); if (flashT2) clearTimeout(flashT2);
        flashT1 = setTimeout(function () { flashEl.style.opacity = String(peak * 0.5); }, 45);   // quick flicker
        flashT2 = setTimeout(function () { flashEl.style.transition = 'opacity 0.32s ease-out'; flashEl.style.opacity = '0'; }, 80);
      });

      bngApi.engineLua("extensions.load('jbWeather')");   // auto-load the engine -- no console needed
      var poll = setInterval(refresh, 1000);
      refresh();
      scope.$on('$destroy', function () { clearInterval(poll); if (flashT1) clearTimeout(flashT1); if (flashT2) clearTimeout(flashT2); if (flashEl.parentNode) flashEl.parentNode.removeChild(flashEl); });
    }
  };
}]);
