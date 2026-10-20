"""Blender: bake BeamNG-style tree imposters from the high-poly source.

Renders unlit albedo+alpha and world-space normals for the alpine polar orbit
(Fichte follows stock douglasfir: 16 equator, no polar rings, no poles). Does not instance the 500k
mesh in-game — that mesh is the bake source only.

Run inside the spruce blend (text editor Alt+P, or CLI):

  blender C:\\temp\\assets\\trees\\fichte2.blend --background --python C:\\temp\\assets\\trees\\tree_imposter_capture.py

Blender 5.2 (the .blend files are newer than 4.2).

Then pack DDS + cheap DAE:

  cd C:\\temp\\beamng_autoroad; python tools\\pack_tree_imposters.py --species spruce
"""
from __future__ import annotations

import math
import os
import sys

import bpy
from mathutils import Vector

CFG = {
    "species": "spruce",
    "object": "tree",  # empty = largest mesh; spruce source is 'tree'
    "samples_albedo": 128,
    "samples_normal": 32,
    "samples_ao": 32,
    "ao_distance_m": 0.45,
    "ao_samples": 16,
    "transparent_max_bounces": 64,
    "max_bounces": 12,
    "render_px": 512,  # 2x dimension, then the packer boxes to 256
    "out_dir": "//out/spruce/cells/",
    "autoroad_tools": r"C:\temp\beamng_autoroad\tools",
    "hide_helpers": True,
}

_lines = []


def log(msg):
    line = f"[imposter] {msg}"
    print(line)
    _lines.append(line)
    txt = bpy.data.texts.get("tree_imposter_log") or bpy.data.texts.new("tree_imposter_log")
    txt.clear()
    txt.write("\n".join(_lines))


def _add_tools():
    p = CFG["autoroad_tools"]
    if p and p not in sys.path and os.path.isdir(p):
        sys.path.insert(0, p)


def out_dir():
    p = bpy.path.abspath(CFG["out_dir"])
    os.makedirs(p, exist_ok=True)
    return p


def find_source():
    name = (CFG.get("object") or "").strip()
    if name:
        o = bpy.data.objects.get(name)
        if o is None:
            log(f"FEHLER: Objekt '{name}' fehlt")
        return o
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH" and not o.hide_render]
    if not meshes:
        log("FEHLER: kein Mesh in der Szene")
        return None
    meshes.sort(key=lambda o: len(o.data.polygons) if o.data else 0, reverse=True)
    log(f"Quelle: {meshes[0].name} ({len(meshes[0].data.polygons)} Faces)")
    return meshes[0]


def evaluated_copy(src):
    """Bake Geometry Nodes into a standalone mesh so helper Planes/Cubes stay out."""
    dg = bpy.context.evaluated_depsgraph_get()
    ev = src.evaluated_get(dg)
    me = bpy.data.meshes.new_from_object(ev, depsgraph=dg)
    ob = bpy.data.objects.new(f"{src.name}_imposter_eval", me)
    ob.matrix_world = src.matrix_world.copy()
    log(f"Evaluated {ob.name}: {len(me.polygons)} Faces, {len(me.materials)} Mats")
    return ob


def bounds_world(obj):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    corners = [ev.matrix_world @ Vector(c) for c in ev.bound_box]
    xs = [c.x for c in corners]
    ys = [c.y for c in corners]
    zs = [c.z for c in corners]
    mn = Vector((min(xs), min(ys), min(zs)))
    mx = Vector((max(xs), max(ys), max(zs)))
    center = (mn + mx) * 0.5
    # radius of AABB (Torque uses shape radius)
    half = (mx - mn) * 0.5
    radius = max(half.length, 0.5)
    log(
        f"AABB center=({center.x:.2f},{center.y:.2f},{center.z:.2f}) "
        f"radius={radius:.2f} m  size=({mx.x-mn.x:.2f},{mx.y-mn.y:.2f},{mx.z-mn.z:.2f})"
    )
    return center, radius


def look_at(cam, loc, target, up=Vector((0.0, 0.0, 1.0))):
    cam.location = loc
    direction = target - loc
    if direction.length < 1e-8:
        return
    direction.normalize()
    if abs(direction.dot(up)) > 0.98:
        up = Vector((0.0, 1.0, 0.0))
    quat = direction.to_track_quat("-Z", "Y")
    cam.rotation_euler = quat.to_euler()


def camera_for_view(center, radius, view, dist_mul=8.0):
    dist = radius * dist_mul
    if view.kind == "pole_top":
        return center + Vector((0.0, 0.0, dist)), Vector((0.0, 1.0, 0.0))
    if view.kind == "pole_bottom":
        return center + Vector((0.0, 0.0, -dist)), Vector((0.0, 1.0, 0.0))
    # Inverse of Torque Rx(rot_x)*Rz(rot_z) applied to a camera on -X (first equator).
    cz, sz = math.cos(view.rot_z), math.sin(view.rot_z)
    cx, sx = math.cos(view.rot_x), math.sin(view.rot_x)
    # rot_z=-pi/2, rot_x=0 -> (-1, 0, 0)
    direction = Vector((sz * cx, -cz * cx, sx))
    if direction.length < 1e-8:
        direction = Vector((0.0, -1.0, 0.0))
    direction.normalize()
    return center + direction * dist, Vector((0.0, 0.0, 1.0))


def _principled(nt):
    for n in nt.nodes:
        if n.type == "BSDF_PRINCIPLED":
            return n
    return None


def _ensure_output(nt):
    for n in nt.nodes:
        if n.type == "OUTPUT_MATERIAL":
            return n
    return nt.nodes.new("ShaderNodeOutputMaterial")


def install_capture_shaders(mode: str):
    """mode: albedo | normal. Stores originals on the material as '_imp_backup'."""
    for mat in bpy.data.materials:
        if not mat.use_nodes or mat.node_tree is None:
            continue
        nt = mat.node_tree
        if mat.get("_imp_mode") == mode:
            continue
        restore_capture_shaders_one(mat)
        out = _ensure_output(nt)
        princ = _principled(nt)
        em = nt.nodes.new("ShaderNodeEmission")
        em.name = "_imp_emission"
        em.location = (out.location.x - 280, out.location.y)
        if mode == "albedo":
            if princ and "Base Color" in princ.inputs and princ.inputs["Base Color"].links:
                nt.links.new(princ.inputs["Base Color"].links[0].from_socket, em.inputs["Color"])
            elif princ:
                em.inputs["Color"].default_value = princ.inputs["Base Color"].default_value
            em.inputs["Strength"].default_value = 1.0
        elif mode == "ao":
            ao = nt.nodes.new("ShaderNodeAmbientOcclusion")
            ao.name = "_imp_ao"
            ao.inputs["Distance"].default_value = float(CFG["ao_distance_m"])
            if hasattr(ao, "only_local"):
                ao.only_local = True
            if hasattr(ao, "samples"):
                ao.samples = int(CFG["ao_samples"])
            nt.links.new(ao.outputs["AO"], em.inputs["Color"])
            em.inputs["Strength"].default_value = 1.0
        else:
            geom = nt.nodes.new("ShaderNodeNewGeometry")
            geom.name = "_imp_geom"
            add = nt.nodes.new("ShaderNodeVectorMath")
            add.name = "_imp_add"
            add.operation = "ADD"
            mul = nt.nodes.new("ShaderNodeVectorMath")
            mul.name = "_imp_mul"
            mul.operation = "MULTIPLY"
            mul.inputs[1].default_value = (0.5, 0.5, 0.5)
            add.inputs[1].default_value = (0.5, 0.5, 0.5)
            nt.links.new(geom.outputs["True Normal"], mul.inputs[0])
            nt.links.new(mul.outputs[0], add.inputs[0])
            nt.links.new(add.outputs[0], em.inputs["Color"])
            em.inputs["Strength"].default_value = 1.0
        # Keep alpha: mix with transparent via original principled alpha.
        trans = nt.nodes.new("ShaderNodeBsdfTransparent")
        trans.name = "_imp_trans"
        mix = nt.nodes.new("ShaderNodeMixShader")
        mix.name = "_imp_mix"
        fac = mix.inputs[0]
        if princ and "Alpha" in princ.inputs and princ.inputs["Alpha"].links:
            nt.links.new(princ.inputs["Alpha"].links[0].from_socket, fac)
        elif princ:
            fac.default_value = princ.inputs["Alpha"].default_value
        else:
            fac.default_value = 1.0
        nt.links.new(trans.outputs[0], mix.inputs[1])
        nt.links.new(em.outputs[0], mix.inputs[2])
        # Disconnect previous surface, connect mix
        for link in list(nt.links):
            if link.to_node == out and link.to_socket == out.inputs["Surface"]:
                nt.links.remove(link)
        nt.links.new(mix.outputs[0], out.inputs["Surface"])
        mat["_imp_mode"] = mode
        mat.blend_method = "CLIP"
        if hasattr(mat, "shadow_method"):
            mat.shadow_method = "CLIP"


def restore_capture_shaders_one(mat):
    if not mat.use_nodes or mat.node_tree is None:
        return
    nt = mat.node_tree
    for name in ("_imp_emission", "_imp_geom", "_imp_add", "_imp_mul", "_imp_ao", "_imp_trans", "_imp_mix"):
        n = nt.nodes.get(name)
        if n:
            nt.nodes.remove(n)
    princ = _principled(nt)
    out = _ensure_output(nt)
    if princ:
        linked = any(l.to_node == out and l.to_socket == out.inputs["Surface"] for l in nt.links)
        if not linked:
            nt.links.new(princ.outputs[0], out.inputs["Surface"])
    if "_imp_mode" in mat:
        del mat["_imp_mode"]


def restore_capture_shaders():
    for mat in bpy.data.materials:
        restore_capture_shaders_one(mat)


def setup_scene(sc, samples: int, px: int):
    sc.render.engine = "CYCLES"
    sc.cycles.samples = samples
    sc.cycles.use_denoising = False
    sc.cycles.transparent_max_bounces = CFG["transparent_max_bounces"]
    sc.cycles.max_bounces = CFG["max_bounces"]
    sc.cycles.diffuse_bounces = 4
    sc.render.film_transparent = True
    sc.render.resolution_x = sc.render.resolution_y = px
    sc.render.resolution_percentage = 100
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    sc.render.image_settings.color_depth = "8"
    sc.view_settings.view_transform = "Standard"
    sc.view_settings.look = "None"
    sc.view_settings.exposure = 0.0
    sc.view_settings.gamma = 1.0
    # No scene lights: emission shaders only
    for o in list(sc.objects):
        if o.type == "LIGHT":
            o.hide_render = True
    world = sc.world or bpy.data.worlds.new("imposter_world")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0, 0, 0, 1)
        bg.inputs[1].default_value = 0.0
    sc.world = world
    return sc


def ensure_camera(sc, center, radius):
    cam = sc.camera
    if cam is None or cam.type != "CAMERA":
        camd = bpy.data.cameras.new("imposter_cam")
        cam = bpy.data.objects.new("imposter_cam", camd)
        sc.collection.objects.link(cam)
        sc.camera = cam
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = radius * 2.05
    cam.data.clip_start = 0.01
    cam.data.clip_end = radius * 40.0
    return cam


def render_view(sc, cam, center, radius, view, path):
    loc, up = camera_for_view(center, radius, view)
    look_at(cam, loc, center, up)
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True, scene=sc.name)


def _cli_args():
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1 :]


def step_capture():
    _add_tools()
    try:
        from tree_imposter_lib import iter_views, species_cfg
    except ImportError:
        log(f"FEHLER: tree_imposter_lib nicht in {CFG['autoroad_tools']}")
        return
    spec = species_cfg(CFG["species"])
    src = find_source()
    if src is None:
        return
    bake = evaluated_copy(src)
    work = bpy.data.scenes.new("imposter_capture")
    work.collection.objects.link(bake)
    center, radius = bounds_world(bake)
    views = iter_views(
        spec["equator_steps"],
        spec["polar_steps"],
        spec["include_poles"],
        spec["polar_angle"],
    )
    d = out_dir()
    setup_scene(work, CFG["samples_albedo"], CFG["render_px"])
    cam = ensure_camera(work, center, radius)
    cli = _cli_args()
    ao_only = "--ao" in cli
    if "--ao-distance" in cli:
        CFG["ao_distance_m"] = float(cli[cli.index("--ao-distance") + 1])
    wanted = None
    if "--views" in cli:
        wanted = {int(x) for x in cli[cli.index("--views") + 1].split(",") if x}
    log(f"{len(views)} Ansichten, Zellenordner {d}")

    try:
        if ao_only:
            log(f"=== occlusion ({CFG['ao_distance_m']} m, nur das Objekt) ===")
            install_capture_shaders("ao")
            work.cycles.samples = CFG["samples_ao"]
            for i, view in enumerate(views):
                if wanted is not None and i not in wanted:
                    continue
                path = os.path.join(d, f"ao_{i:02d}.png")
                render_view(work, cam, center, radius, view, path)
                log(f"  {i:02d} ao -> {os.path.basename(path)}")
        else:
            log("=== albedo (unbeleuchtet) ===")
            install_capture_shaders("albedo")
            work.cycles.samples = CFG["samples_albedo"]
            for i, view in enumerate(views):
                path = os.path.join(d, f"albedo_{i:02d}.png")
                render_view(work, cam, center, radius, view, path)
                log(f"  {i:02d} {view.kind} polar={view.polar_index} eq={view.equator_index} -> {os.path.basename(path)}")

            log("=== world-normal ===")
            install_capture_shaders("normal")
            work.cycles.samples = CFG["samples_normal"]
            for i, view in enumerate(views):
                path = os.path.join(d, f"normal_{i:02d}.png")
                render_view(work, cam, center, radius, view, path)
                log(f"  {i:02d} normal -> {os.path.basename(path)}")
    finally:
        restore_capture_shaders()
        me = bake.data
        bpy.data.objects.remove(bake, do_unlink=True)
        if me and me.users == 0:
            bpy.data.meshes.remove(me)
        bpy.data.scenes.remove(work)
    meta = os.path.join(d, "views.json")
    import json

    with open(meta, "w", encoding="utf-8") as f:
        json.dump(
            {
                "species": CFG["species"],
                "object": src.name,
                "center": [center.x, center.y, center.z],
                "radius": radius,
                "views": [
                    {
                        "i": i,
                        "kind": v.kind,
                        "polar_index": v.polar_index,
                        "equator_index": v.equator_index,
                        "rot_x": v.rot_x,
                        "rot_z": v.rot_z,
                    }
                    for i, v in enumerate(views)
                ],
            },
            f,
            indent=2,
        )
    log(f"views.json geschrieben. Packer: python tools\\pack_tree_imposters.py --species {CFG['species']}")
    log("Zellen vor dem Packen ansehen: Krone frei, Alpha sauber, Normalen bunt (nicht beleuchtet).")


RUN = ["step_capture"]

for _name in RUN:
    log(f"=== {_name} ===")
    globals()[_name]()
