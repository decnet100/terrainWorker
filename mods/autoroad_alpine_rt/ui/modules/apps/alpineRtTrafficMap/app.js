// Alpine Roadtrip Map (Traffic Overlay)
// UI App: renders a simple map-like polyline view with low/medium/high traffic colouring.
angular.module('beamng.apps').directive('alpinerttrafficmap', [function () {
  var tpl = ''
    + '<div class="artm">'
    + '  <style>'
    + '  .artm{font-family:"Cairo","Overpass",system-ui,sans-serif;color:#dfe2e6;background:rgba(18,20,24,0.90);'
    + '       border:1px solid rgba(255,255,255,0.07);border-top:2px solid #ff6600;border-radius:5px;'
    + '       height:100%;width:100%;box-sizing:border-box;display:flex;flex-direction:column;'
    + '       overflow:hidden;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);}'
    + '  .artm *{box-sizing:border-box;}'
    + '  .artm-head{display:flex;align-items:center;justify-content:space-between;padding:10px 11px;'
    + '            border-bottom:1px solid rgba(255,255,255,0.06);}'
    + '  .artm-brand{display:flex;align-items:center;min-width:0;}'
    + '  .artm-slash{width:6px;height:16px;background:#ff6600;transform:skewX(-16deg);margin-right:9px;flex:none;}'
    + '  .artm-title{font-style:italic;font-weight:800;font-size:13px;letter-spacing:.3px;color:#f4f6f8;white-space:nowrap;}'
    + '  .artm-sub{padding:7px 11px 0;color:#8b9198;font-size:11px;display:flex;flex-wrap:wrap;gap:10px;}'
    + '  .artm-sub b{color:#dfe2e6;font-weight:700;}'
    + '  .artm-body{padding:10px 11px 11px;display:flex;flex-direction:column;gap:8px;flex:1;min-height:0;}'
    + '  .artm-can{width:100%;flex:1;border:1px solid rgba(255,255,255,0.08);border-radius:4px;background:rgba(0,0,0,0.18);}'
    + '  .artm-leg{display:flex;align-items:center;gap:10px;font-size:11px;color:#9aa3ae;}'
    + '  .artm-dot{width:9px;height:9px;border-radius:2px;display:inline-block;margin-right:6px;transform:skewX(-12deg);}'
    + '  .artm-row{display:flex;align-items:center;justify-content:space-between;gap:10px;}'
    + '  .artm-btn{font-family:inherit;font-size:11px;font-weight:700;letter-spacing:.5px;border-radius:3px;padding:7px 10px;'
    + '           cursor:pointer;border:1px solid rgba(255,255,255,0.11);background:rgba(255,255,255,0.06);color:#dfe2e6;transition:all .12s;}'
    + '  .artm-btn:hover{background:rgba(255,255,255,0.12);}'
    + '  </style>'
    + '  <div class="artm-head">'
    + '    <div class="artm-brand"><span class="artm-slash"></span><span class="artm-title">ALPINE ROADTRIP MAP</span></div>'
    + '    <button class="artm-btn" ng-click="refresh()">Refresh</button>'
    + '  </div>'
    + '  <div class="artm-sub">'
    + '    <span>Level <b>{{state.level || "-"}}</b></span>'
    + '    <span ng-if="state.lua_rev">Lua <b>{{state.lua_rev}}</b></span>'
    + '    <span ng-if="state.segments">Segments <b>{{state.segments.length}}</b></span>'
    + '  </div>'
    + '  <div class="artm-body">'
    + '    <canvas class="artm-can"></canvas>'
    + '    <div class="artm-row">'
    + '      <div class="artm-leg">'
    + '        <span><i class="artm-dot" style="background:#3ddc84"></i>low</span>'
    + '        <span><i class="artm-dot" style="background:#ffd166"></i>medium</span>'
    + '        <span><i class="artm-dot" style="background:#ff4d4f"></i>high</span>'
    + '      </div>'
    + '      <div style="font-size:11px;color:#7a8088" ng-if="state.now_s">t={{state.t_s}}s</div>'
    + '    </div>'
    + '  </div>'
    + '</div>';

  function colFor(d) {
    if (d == null) return '#9aa3ae';
    if (d < 0.40) return '#3ddc84';
    if (d < 0.70) return '#ffd166';
    return '#ff4d4f';
  }

  function draw(canvas, state) {
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    if (!ctx) return;

    var w = canvas.clientWidth || 10;
    var h = canvas.clientHeight || 10;
    if (canvas.width !== w) canvas.width = w;
    if (canvas.height !== h) canvas.height = h;

    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = 'rgba(0,0,0,0.08)';
    ctx.fillRect(0, 0, w, h);

    var segs = (state && state.segments) ? state.segments : [];
    if (!segs.length) {
      ctx.fillStyle = 'rgba(255,255,255,0.55)';
      ctx.font = '12px system-ui, sans-serif';
      ctx.fillText('No map segments yet (build_portals arc points).', 12, 22);
      return;
    }

    // Compute bounds over all points (world x/y), then fit into canvas with padding.
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (var i = 0; i < segs.length; i++) {
      var pts = segs[i].points || [];
      for (var j = 0; j < pts.length; j++) {
        var p = pts[j];
        if (!p || p.length < 2) continue;
        var x = p[0], y = p[1];
        if (x < minX) minX = x; if (x > maxX) maxX = x;
        if (y < minY) minY = y; if (y > maxY) maxY = y;
      }
    }
    if (!isFinite(minX) || !isFinite(minY) || (maxX - minX) < 1e-6 || (maxY - minY) < 1e-6) {
      ctx.fillStyle = 'rgba(255,255,255,0.55)';
      ctx.font = '12px system-ui, sans-serif';
      ctx.fillText('Map bounds invalid.', 12, 22);
      return;
    }
    var pad = 18;
    var sx = (w - pad * 2) / (maxX - minX);
    var sy = (h - pad * 2) / (maxY - minY);
    var s = Math.min(sx, sy);
    var offX = pad + (w - pad * 2 - (maxX - minX) * s) * 0.5;
    var offY = pad + (h - pad * 2 - (maxY - minY) * s) * 0.5;

    function tx(x) { return offX + (x - minX) * s; }
    function ty(y) { return offY + (maxY - y) * s; } // invert so north-ish is up

    // Draw a faint frame/grid.
    ctx.strokeStyle = 'rgba(255,255,255,0.06)';
    ctx.lineWidth = 1;
    ctx.strokeRect(0.5, 0.5, w - 1, h - 1);

    // Draw segments.
    for (var k = 0; k < segs.length; k++) {
      var seg = segs[k];
      var pts2 = seg.points || [];
      if (pts2.length < 2) continue;
      ctx.beginPath();
      for (var m = 0; m < pts2.length; m++) {
        var pp = pts2[m];
        if (!pp || pp.length < 2) continue;
        var px = tx(pp[0]), py = ty(pp[1]);
        if (m === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      }
      ctx.strokeStyle = colFor(seg.density);
      ctx.lineWidth = 4.0;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.globalAlpha = 0.90;
      ctx.stroke();

      // Outline for readability.
      ctx.strokeStyle = 'rgba(0,0,0,0.45)';
      ctx.lineWidth = 6.0;
      ctx.globalAlpha = 0.35;
      ctx.stroke();
      ctx.globalAlpha = 1.0;
    }

    // Labels (first point of each segment).
    ctx.font = '11px system-ui, sans-serif';
    ctx.fillStyle = 'rgba(255,255,255,0.70)';
    for (var z = 0; z < segs.length; z++) {
      var s0 = segs[z];
      var p0 = (s0.points && s0.points[0]) ? s0.points[0] : null;
      if (!p0) continue;
      ctx.fillText(String(s0.label || s0.id || ''), tx(p0[0]) + 6, ty(p0[1]) - 6);
    }
  }

  return {
    template: tpl,
    replace: true,
    restrict: 'EA',
    scope: true,
    link: function (scope, element) {
      scope.state = { level: null, segments: [] };

      function canvasEl() {
        return element[0] && element[0].querySelector ? element[0].querySelector('canvas') : null;
      }

      function apply(d) {
        if (!d) return;
        scope.$evalAsync(function () {
          scope.state = d;
          draw(canvasEl(), scope.state);
        });
      }

      function poll() {
        if (!window.bngApi || !bngApi.engineLua) return;
        bngApi.engineLua('extensions.alpine_rt.getTrafficMap()', apply);
      }

      scope.refresh = function () {
        poll();
      };

      try {
        if (window.bngApi && bngApi.engineLua) {
          bngApi.engineLua("extensions.load('alpine_rt')");
        }
      } catch (e) {}

      var timer = setInterval(poll, 1000);
      poll();

      // Redraw on size changes (simple periodic check).
      var lastW = 0, lastH = 0;
      var sizeTimer = setInterval(function () {
        var c = canvasEl(); if (!c) return;
        var w = c.clientWidth || 0, h = c.clientHeight || 0;
        if (w !== lastW || h !== lastH) {
          lastW = w; lastH = h;
          draw(c, scope.state);
        }
      }, 700);

      scope.$on('$destroy', function () {
        clearInterval(timer);
        clearInterval(sizeTimer);
      });
    }
  };
}]);

