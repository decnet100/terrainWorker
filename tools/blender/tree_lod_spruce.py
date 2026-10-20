"""Blender: Nah-LOD der Fichte aus Fichte2 (Bake-Quelle bleibt unangetastet).

Exportiert nur LOD0 als _a1000. install_spruce_far_lod haengt danach
vier Ansichtskarten (_a80) und den Kamera-Billboard (Groesse 25) an.

  blender C:\\temp\\assets\\trees\\fichte2.blend --background --python C:\\temp\\assets\\trees\\tree_lod_spruce.py
"""
from __future__ import annotations

import math
import os
import random
import shutil
import sys

import bmesh
import bpy
from mathutils import Vector

CFG = {
    "source": "tree",
    "work_collection": "LOD_work",
    "lod_collections": ["LOD0"],
    "bark_ratio": [0.20],
    "leaf_keep": [0.12],
    "bark_target_tris": [3000],
    "bark_min_radius_m": [0.03],
    "bark_weld_m": 0.002,
    "seed": 1,
    "export_dae": True,
    "pack_dir": r"C:\temp\beamng_autoroad\data\assets\trees\spruce",
    "dae_stem": "tree_spruce_large",
    "lod_pixels": [1000],  # Nahmesh bis 1000 px; danach vier Karten, Billboard ab 25
    "autoroad_tools": r"C:\temp\beamng_autoroad\tools",
    "species": "spruce",
    "tex_bark": r"C:\temp\assets\trees\spruce\SpruceBark.color.png",
    "tex_twig_color": r"C:\temp\assets\trees\spruce\SpruceTwig.color.png",
    "tex_twig_alpha": r"C:\temp\assets\trees\spruce\SpruceTwig.alpha.png",
    # SpruceTwig1 Hue/Saturation/Value in fichte2.blend (hue 0.5 = unveraendert)
    "twig_hsv": [0.5, 0.9, 0.5],
}

_lines = []


def log(msg):
    line = f"[lod_spruce] {msg}"
    print(line)
    _lines.append(line)
    txt = bpy.data.texts.get("tree_lod_spruce_log") or bpy.data.texts.new("tree_lod_spruce_log")
    txt.clear()
    txt.write("\n".join(_lines))


def get_coll(name):
    c = bpy.data.collections.get(name)
    if c is None:
        c = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(c)
    return c


def clear_coll(coll):
    for o in list(coll.objects):
        me = o.data
        bpy.data.objects.remove(o, do_unlink=True)
        if me and me.users == 0:
            bpy.data.meshes.remove(me)


def tris(obj):
    return sum(len(p.vertices) - 2 for p in obj.data.polygons)


def _mat_name(m):
    return (m.name if m else "").lower()


def classify_materials(me):
    bark, leaf, other = [], [], []
    hints_b = ("bark", "rinde", "stamm", "trunk")
    hints_l = ("twig", "leaf", "nadel", "foliage", "needle", "blatt")
    for i, m in enumerate(me.materials):
        n = _mat_name(m)
        if any(h in n for h in hints_b):
            bark.append(i)
        elif any(h in n for h in hints_l):
            leaf.append(i)
        else:
            other.append(i)
    if not bark and not leaf:
        # fallback: densest material = foliage if more faces, else first
        counts = [0] * max(len(me.materials), 1)
        for p in me.polygons:
            if p.material_index < len(counts):
                counts[p.material_index] += 1
        if counts:
            leaf.append(int(max(range(len(counts)), key=lambda i: counts[i])))
            bark = [i for i in range(len(counts)) if i not in leaf]
    log(f"Material-Index Rinde={bark} Nadeln={leaf} sonst={other}")
    return set(bark), set(leaf) | set(other)


def split_by_sets(me_src, keep_idx):
    me = me_src.copy()
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.delete(bm, geom=[f for f in bm.faces if f.material_index not in keep_idx], context="FACES")
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
    bm.to_mesh(me)
    bm.free()
    return me


def find_source():
    name = (CFG.get("source") or "").strip()
    if name:
        return bpy.data.objects.get(name)
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH" and not o.hide_render]
    meshes.sort(key=lambda o: len(o.data.polygons) if o.data else 0, reverse=True)
    return meshes[0] if meshes else None


def step1_split():
    src = find_source()
    if src is None:
        log("FEHLER: kein Quell-Mesh")
        return
    dg = bpy.context.evaluated_depsgraph_get()
    baked = bpy.data.meshes.new_from_object(src.evaluated_get(dg), depsgraph=dg)
    bark_i, leaf_i = classify_materials(baked)
    work = get_coll(CFG["work_collection"])
    clear_coll(work)
    for kind, idx in (("bark", bark_i), ("leaf", leaf_i)):
        if not idx:
            log(f"Hinweis: keine Faces fuer {kind}")
            continue
        me = split_by_sets(baked, idx)
        o = bpy.data.objects.new(f"{kind}_base", me)
        o.matrix_world = src.matrix_world.copy()
        work.objects.link(o)
        log(f"{o.name}: {len(me.polygons)} Faces, {tris(o)} Tris")
    bpy.data.meshes.remove(baked)


def _ensure_in_view(obj):
    sc = bpy.context.scene
    if obj.name not in sc.collection.objects:
        sc.collection.objects.link(obj)
    obj.hide_set(False)
    obj.hide_viewport = False
    obj.hide_render = False
    bpy.context.view_layer.objects.active = obj
    for o in bpy.context.view_layer.objects:
        o.select_set(o == obj)
    bpy.context.view_layer.update()


def _prune_thin_stems(obj, min_radius):
    """Drop bark faces whose mean vertex radius is below min_radius (metres)."""
    me = obj.data
    attr = me.attributes.get("radius")
    if attr is None or attr.domain != "POINT" or min_radius <= 0:
        log(f"  radius-Attribut fehlt, keine Stamm-Pruning ({obj.name})")
        return
    rs = [d.value for d in attr.data]
    if len(rs) != len(me.vertices):
        log(f"  radius-Laenge {len(rs)} != verts {len(me.vertices)}")
        return
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.verts.ensure_lookup_table()
    kill = [
        f
        for f in bm.faces
        if sum(rs[v.index] for v in f.verts) / max(len(f.verts), 1) < float(min_radius)
    ]
    before_f = len(bm.faces)
    if kill:
        bmesh.ops.delete(bm, geom=kill, context="FACES")
        bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
    weld = float(CFG.get("bark_weld_m") or 0.0)
    if weld > 0 and bm.verts:
        bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=weld)
    bm.to_mesh(me)
    bm.free()
    me.update()
    log(f"  prune r>={min_radius:.3f} m  {before_f} -> {len(me.polygons)} Faces, {tris(obj)} Tris")


def _decimate_to(obj, ratio, target):
    """Collapse via DECIMATE + object.convert (apply is a no-op in 5.2 background)."""
    before = tris(obj)
    if before <= 0:
        return
    if target and before <= float(target) * 1.05:
        log(f"  decimate skip {before} <= Ziel {target}")
        return
    use = float(ratio)
    if target and before * use > float(target):
        use = max(0.02, float(target) / float(before))
    if use >= 0.999:
        return
    _ensure_in_view(obj)
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    mod = obj.modifiers.new("dec", "DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = use
    bpy.context.view_layer.update()
    eval_faces = int(mod.face_count) if mod.face_count else 0
    bpy.ops.object.convert(target="MESH")
    after = tris(obj)
    log(f"  decimate factor={use:.4f} eval_faces={eval_faces}  {before} -> {after} Tris")
    if target and after > float(target) * 1.8:
        use2 = max(0.05, float(target) / float(after))
        _ensure_in_view(obj)
        mod2 = obj.modifiers.new("dec2", "DECIMATE")
        mod2.decimate_type = "COLLAPSE"
        mod2.ratio = use2
        bpy.ops.object.convert(target="MESH")
        log(f"  decimate 2nd factor={use2:.4f}  {after} -> {tris(obj)} Tris")


def _thin_leaves(me, keep, rnd):
    if keep >= 0.999:
        return me
    bm = bmesh.new()
    bm.from_mesh(me)
    faces = list(bm.faces)
    rnd.shuffle(faces)
    n_keep = max(8, int(len(faces) * keep)) if keep > 0 else 0
    if n_keep <= 0:
        bmesh.ops.delete(bm, geom=faces, context="FACES")
    else:
        bmesh.ops.delete(bm, geom=faces[n_keep:], context="FACES")
        bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
        scale = 1.0 / math.sqrt(max(keep, 0.02))
        done = set()
        for f in bm.faces:
            c = f.calc_center_median()
            for v in f.verts:
                if v.index in done:
                    continue
                done.add(v.index)
                v.co = c + (v.co - c) * scale
    bm.to_mesh(me)
    bm.free()
    return me


def _cross_planes(leaf_obj, name):
    """Two vertical cards covering the foliage AABB. UVs 0..1 for the silhouette PNG."""
    mw = leaf_obj.matrix_world
    pts = [mw @ Vector(v.co) for v in leaf_obj.data.vertices]
    if not pts:
        return None
    xs, ys, zs = [p.x for p in pts], [p.y for p in pts], [p.z for p in pts]
    cx, cy = (min(xs) + max(xs)) * 0.5, (min(ys) + max(ys)) * 0.5
    z0, z1 = min(zs), max(zs)
    rx = max(max(xs) - min(xs), max(ys) - min(ys)) * 0.5
    h = z1 - z0
    me = bpy.data.meshes.new(name)
    verts = []
    faces = []
    for yaw in (0.0, math.pi / 2.0):
        c, s = math.cos(yaw), math.sin(yaw)
        hx, hy = c * rx, s * rx
        base = len(verts)
        verts.extend(
            [
                (cx - hx, cy - hy, z0),
                (cx + hx, cy + hy, z0),
                (cx + hx, cy + hy, z1),
                (cx - hx, cy - hy, z1),
            ]
        )
        faces.append((base, base + 1, base + 2))
        faces.append((base, base + 2, base + 3))
    me.from_pydata(verts, [], faces)
    uv = me.uv_layers.new(name="UVMap")
    # two tris per card: (0,1,2) and (0,2,3) with v0=(0,0) v1=(1,0) v2=(1,1) v3=(0,1)
    corners = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    loop_uvs = []
    for _yaw in (0, 1):
        loop_uvs.extend([corners[0], corners[1], corners[2], corners[0], corners[2], corners[3]])
    for i, (u, v) in enumerate(loop_uvs):
        uv.data[i].uv = (u, v)
    me.update()
    mat = bpy.data.materials.get("SpruceCard")
    if mat is None:
        mat = bpy.data.materials.new("SpruceCard")
    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    log(f"{name}: 2 Kreuzkarten, Spannweite {rx*2:.2f} m, Hoehe {h:.2f} m")
    return ob


def step2_make_lods():
    bark = bpy.data.objects.get("bark_base")
    leaf = bpy.data.objects.get("leaf_base")
    if bark is None:
        log("FEHLER: erst step1_split()")
        return
    rnd = random.Random(CFG["seed"])
    for i, cname in enumerate(CFG["lod_collections"]):
        coll = get_coll(cname)
        clear_coll(coll)
        me = bark.data.copy()
        ob = bpy.data.objects.new(f"bark_lod{i}", me)
        ob.matrix_world = bark.matrix_world.copy()
        coll.objects.link(ob)
        radii = CFG.get("bark_min_radius_m") or [0.0, 0.0]
        _prune_thin_stems(ob, radii[i] if i < len(radii) else radii[-1])
        _decimate_to(ob, CFG["bark_ratio"][i], CFG["bark_target_tris"][i])
        log(f"{cname} Rinde: {tris(bark)} -> {tris(ob)}")
        keep = CFG["leaf_keep"][i]
        if leaf is None:
            continue
        if keep <= 0.001:
            card = _cross_planes(leaf, f"cards_lod{i}")
            if card:
                coll.objects.link(card)
        else:
            me = leaf.data.copy()
            _thin_leaves(me, keep, rnd)
            ol = bpy.data.objects.new(f"leaf_lod{i}", me)
            ol.matrix_world = leaf.matrix_world.copy()
            coll.objects.link(ol)
            log(f"{cname} Nadeln: {tris(leaf)} -> {tris(ol)}")


def step3_report():
    for cname in CFG["lod_collections"]:
        coll = bpy.data.collections.get(cname)
        if coll is None:
            continue
        parts = {o.name: tris(o) for o in coll.objects}
        log(f"{cname}: {parts} Summe={sum(parts.values())}")


def _sid(name: str) -> str:
    out = []
    for ch in name:
        out.append(ch if ch.isalnum() or ch in "-_" else "_")
    s = "".join(out).strip("_") or "Mat"
    if s[0].isdigit():
        s = "M_" + s
    return s


def _xml_esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _mesh_world_tris(obj):
    """Exploded corners (unique UV). Returns verts, uvs, faces_by_matname."""
    mw = obj.matrix_world
    me = obj.data
    me.calc_loop_triangles()
    uv_layer = me.uv_layers.active
    verts = []
    uvs = []
    by_mat = {}
    for tri in me.loop_triangles:
        mat = None
        if tri.material_index < len(me.materials):
            mat = me.materials[tri.material_index]
        mname = mat.name if mat else "Default"
        idxs = []
        for li in tri.loops:
            loop = me.loops[li]
            co = mw @ me.vertices[loop.vertex_index].co
            verts.append((float(co.x), float(co.y), float(co.z)))
            if uv_layer:
                u, v = uv_layer.data[li].uv
                uvs.append((float(u), float(v)))
            else:
                uvs.append((0.0, 0.0))
            idxs.append(len(verts) - 1)
        by_mat.setdefault(mname, []).append((idxs[0], idxs[1], idxs[2]))
    return verts, uvs, by_mat


def _composite_twig_png(color_path, alpha_path, dest):
    """RGBA PNG: color RGB + alpha map as A (BeamNG alphaTest)."""
    if not color_path or not os.path.isfile(color_path):
        return False
    col = bpy.data.images.load(color_path, check_existing=False)
    w, h = int(col.size[0]), int(col.size[1])
    if w <= 0 or h <= 0:
        shutil.copy2(color_path, dest)
        return True
    cp = list(col.pixels)
    if alpha_path and os.path.isfile(alpha_path):
        alp = bpy.data.images.load(alpha_path, check_existing=False)
        if int(alp.size[0]) == w and int(alp.size[1]) == h:
            ap = alp.pixels[:]
            for i in range(w * h):
                cp[i * 4 + 3] = ap[i * 4]
    hsv = CFG.get("twig_hsv") or (0.5, 1.0, 1.0)
    sat, val = float(hsv[1]), float(hsv[2])
    if sat < 0.999 or val < 0.999:
        n = w * h
        luma_w = (0.2126, 0.7152, 0.0722)
        for i in range(n):
            r, g, b = cp[i * 4], cp[i * 4 + 1], cp[i * 4 + 2]
            y = r * luma_w[0] + g * luma_w[1] + b * luma_w[2]
            cp[i * 4] = (y + (r - y) * sat) * val
            cp[i * 4 + 1] = (y + (g - y) * sat) * val
            cp[i * 4 + 2] = (y + (b - y) * sat) * val
    img = bpy.data.images.new("_twig_rgba", w, h, alpha=True)
    img.pixels = cp
    img.filepath_raw = dest
    img.file_format = "PNG"
    img.save()
    bpy.data.images.remove(img)
    return True


def _install_pack_textures(pack):
    """Copy Fichte2 albedos into the pack. Returns {matname: {base: fname}}."""
    os.makedirs(pack, exist_ok=True)
    out = {}
    bark_src = CFG.get("tex_bark")
    bark_name = "bark_spruce.color.png"
    if bark_src and os.path.isfile(bark_src):
        shutil.copy2(bark_src, os.path.join(pack, bark_name))
        log(f"  Rinde {bark_src} -> {bark_name}")
        out["SpruceBark"] = {"base": bark_name}
    twig_name = "twig_spruce.color.png"
    op_name = "twig_spruce_o.data.png"
    if _composite_twig_png(CFG.get("tex_twig_color"), CFG.get("tex_twig_alpha"), os.path.join(pack, twig_name)):
        log(f"  Nadeln -> {twig_name}")
        twig = {"base": twig_name}
        alp = CFG.get("tex_twig_alpha")
        if alp and os.path.isfile(alp):
            shutil.copy2(alp, os.path.join(pack, op_name))
            twig["opacity"] = op_name
            log(f"  Nadel-Opacity -> {op_name}")
        out["SpruceTwig1"] = twig
        out["SpruceTwig.001"] = dict(twig)
    card_name = f"{CFG['dae_stem']}_lod1_side.png"
    if os.path.isfile(os.path.join(pack, card_name)):
        out["SpruceCard"] = {"base": card_name}
        log(f"  LOD1-Karte {card_name}")
    elif twig_name in (out.get("SpruceTwig1") or {}).values():
        out["SpruceCard"] = {"base": twig_name}
    return out


def _save_mat_images(mat, dest_dir):
    """Prefer files already on disk (Fichte2 textures are not packed)."""
    out = {}
    if mat is None:
        return out
    name = mat.name
    low = name.lower()
    if "card" in low:
        card = f"{CFG['dae_stem']}_lod1_side.png"
        if os.path.isfile(os.path.join(dest_dir, card)):
            return {"base": card}
    if "bark" in low and os.path.isfile(os.path.join(dest_dir, "bark_spruce.color.png")):
        return {"base": "bark_spruce.color.png"}
    if any(h in low for h in ("twig", "leaf", "nadel")) and os.path.isfile(
        os.path.join(dest_dir, "twig_spruce.color.png")
    ):
        return {"base": "twig_spruce.color.png"}
    if not mat.use_nodes or mat.node_tree is None:
        return out
    os.makedirs(dest_dir, exist_ok=True)
    sid = _sid(mat.name)
    n_img = 0
    for node in mat.node_tree.nodes:
        if node.type != "TEX_IMAGE" or node.image is None:
            continue
        img = node.image
        src = bpy.path.abspath(img.filepath) if img.filepath else ""
        fname = f"{sid}_{n_img}.png"
        n_img += 1
        path = os.path.join(dest_dir, fname)
        copied = False
        if src and os.path.isfile(src):
            shutil.copy2(src, path)
            copied = True
        elif img.has_data and img.size[0] > 0:
            try:
                img.filepath_raw = path
                img.file_format = "PNG"
                img.save()
                copied = True
            except Exception as exc:  # noqa: BLE001
                log(f"Hinweis: Textur {img.name} nicht gespeichert ({exc})")
        if not copied:
            continue
        to_alpha = False
        for link in mat.node_tree.links:
            if link.from_node == node and link.from_socket.name.lower() == "alpha":
                to_alpha = True
        if to_alpha:
            out.setdefault("alpha", fname)
        else:
            out.setdefault("base", fname)
        log(f"  Textur {mat.name} -> {fname}")
    return out


def _write_materials_json(path, mat_files, vfs_rel):
    import json

    data = {}
    for name, files in mat_files.items():
        base = files.get("base")
        low = name.lower()
        is_twig = any(h in low for h in ("twig", "leaf", "nadel")) and "card" not in low
        is_bark = "bark" in low or "rinde" in low
        stage = {"metallicFactor": 0.0}
        if not is_twig and not is_bark:
            stage["roughnessFactor"] = 0.85
        if base:
            stage["baseColorMap"] = f"{vfs_rel}/{base}"
        if files.get("opacity"):
            stage["opacityMap"] = f"{vfs_rel}/{files['opacity']}"
        if is_twig:
            stage["roughnessFactor"] = 0.9
        elif is_bark:
            stage["roughnessFactor"] = 0.85
        leaf = any(h in low for h in ("twig", "leaf", "nadel", "card"))
        block = {
            "name": name,
            "mapTo": name,
            "class": "Material",
            "Stages": [stage, {}, {}, {}],
            "castShadows": False,
            "annotation": "TREE",
            "version": 1.5,
        }
        if leaf:
            # Like stock fir_leaves: dedicated opacityMap + alphaTest.
            # baseColorMap-Alpha ignoriert die PBR-Pipeline; translucentZWrite
            # stapelt die Karten und macht sie undurchsichtig.
            block["alphaTest"] = True
            block["alphaRef"] = 75
            block["doubleSided"] = True
            block["invertBackFaceNormals"] = True
        data[name] = block
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _write_multi_lod_dae(path, lods, stem):
    """lods: list of (pixel_size, verts, uvs, faces_by_mat)."""
    images = []
    effects = []
    materials = []
    geoms = []
    nodes = []
    seen_mat = set()
    mat_png = {}
    pack = os.path.dirname(path)
    for _, _, _, by_mat in lods:
        for mname in by_mat:
            if mname in seen_mat:
                continue
            seen_mat.add(mname)
            sid = _sid(mname)
            mat = bpy.data.materials.get(mname)
            files = _save_mat_images(mat, pack)
            mat_png[mname] = files
            png = files.get("base")
            if png:
                images.append(
                    f'    <image id="{sid}-image" name="{sid}-image">'
                    f"<init_from>{_xml_esc(png)}</init_from></image>"
                )
                diff = f'<texture texture="{sid}-sampler" texcoord="UVSET0"/>'
                params = (
                    f'        <newparam sid="{sid}-surface"><surface type="2D">'
                    f"<init_from>{sid}-image</init_from></surface></newparam>\n"
                    f'        <newparam sid="{sid}-sampler"><sampler2D>'
                    f"<source>{sid}-surface</source></sampler2D></newparam>"
                )
            else:
                diff = "<color>0.2 0.14 0.08 1</color>"
                params = ""
            extra = ""
            if any(h in mname.lower() for h in ("twig", "leaf", "nadel", "card")):
                extra = (
                    "        <extra><technique profile=\"GOOGLEEARTH\">"
                    "<double_sided>1</double_sided></technique></extra>\n"
                )
            effects.append(
                f'    <effect id="{sid}-effect">\n'
                f"      <profile_COMMON>\n{params}\n"
                f'        <technique sid="common"><lambert>'
                f"<diffuse>{diff}</diffuse></lambert></technique>\n{extra}"
                f"      </profile_COMMON>\n    </effect>"
            )
            materials.append(
                f'    <material id="{sid}-material" name="{_xml_esc(mname)}">'
                f'<instance_effect url="#{sid}-effect"/></material>'
            )

    for pix, verts, uvs, by_mat in lods:
        lod = f"{stem}_a{int(pix)}"
        n_vert = len(verts)
        pos_vals = " ".join(f"{x:.4f} {y:.4f} {z:.4f}" for x, y, z in verts)
        uv_vals = " ".join(f"{u:.5f} {v:.5f}" for u, v in uvs)
        tri_xml = []
        binds = []
        for mname, faces in by_mat.items():
            sid = _sid(mname)
            p = " ".join(f"{a} {a} {b} {b} {c} {c}" for a, b, c in faces)
            sym = _xml_esc(mname)
            tri_xml.append(
                f'        <triangles material="{sym}" count="{len(faces)}">\n'
                f'          <input semantic="VERTEX" source="#{lod}-mesh-vertices" offset="0"/>\n'
                f'          <input semantic="TEXCOORD" source="#{lod}-mesh-map-0" offset="1" set="0"/>\n'
                f"          <p>{p}</p>\n"
                f"        </triangles>"
            )
            binds.append(
                f'                  <instance_material symbol="{sym}" target="#{sid}-material">\n'
                f'                    <bind_vertex_input semantic="UVSET0" '
                f'input_semantic="TEXCOORD" input_set="0"/>\n'
                f"                  </instance_material>"
            )
        geoms.append(
            f'    <geometry id="{lod}-mesh" name="{lod}-mesh">\n'
            f"      <mesh>\n"
            f'        <source id="{lod}-mesh-positions">\n'
            f'          <float_array id="{lod}-mesh-positions-array" count="{n_vert * 3}">{pos_vals}</float_array>\n'
            f"          <technique_common><accessor source=\"#{lod}-mesh-positions-array\" "
            f'count="{n_vert}" stride="3">\n'
            f'            <param name="X" type="float"/><param name="Y" type="float"/>'
            f'<param name="Z" type="float"/>\n'
            f"          </accessor></technique_common>\n        </source>\n"
            f'        <source id="{lod}-mesh-map-0">\n'
            f'          <float_array id="{lod}-mesh-map-0-array" count="{n_vert * 2}">{uv_vals}</float_array>\n'
            f"          <technique_common><accessor source=\"#{lod}-mesh-map-0-array\" "
            f'count="{n_vert}" stride="2">\n'
            f'            <param name="S" type="float"/><param name="T" type="float"/>\n'
            f"          </accessor></technique_common>\n        </source>\n"
            f'        <vertices id="{lod}-mesh-vertices">'
            f'<input semantic="POSITION" source="#{lod}-mesh-positions"/></vertices>\n'
            + "\n".join(tri_xml)
            + "\n      </mesh>\n    </geometry>"
        )
        nodes.append(
            f'          <node id="{lod}" name="{lod}" type="NODE">\n'
            f'            <instance_geometry url="#{lod}-mesh">\n'
            f"              <bind_material><technique_common>\n"
            + "\n".join(binds)
            + "\n              </technique_common></bind_material>\n"
            f"            </instance_geometry>\n          </node>"
        )

    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">\n'
        "  <asset>\n"
        "    <contributor><authoring_tool>beamng_autoroad tree_lod_spruce</authoring_tool></contributor>\n"
        '    <unit name="meter" meter="1"/>\n'
        "    <up_axis>Z_UP</up_axis>\n"
        "  </asset>\n"
        "  <library_images>\n"
        + "\n".join(images)
        + "\n  </library_images>\n"
        "  <library_effects>\n"
        + "\n".join(effects)
        + "\n  </library_effects>\n"
        "  <library_materials>\n"
        + "\n".join(materials)
        + "\n  </library_materials>\n"
        "  <library_geometries>\n"
        + "\n".join(geoms)
        + "\n  </library_geometries>\n"
        "  <library_visual_scenes>\n"
        '    <visual_scene id="Scene" name="Scene">\n'
        '      <node id="base00" name="base00" type="NODE">\n'
        '        <node id="start01" name="start01" type="NODE">\n'
        + "\n".join(nodes)
        + "\n        </node>\n      </node>\n    </visual_scene>\n"
        "  </library_visual_scenes>\n"
        '  <scene><instance_visual_scene url="#Scene"/></scene>\n'
        "</COLLADA>\n"
    )
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(xml)
    return mat_png


def step4_export_dae():
    if not CFG.get("export_dae"):
        return
    pack = CFG["pack_dir"]
    stem = CFG["dae_stem"]
    pixels = CFG["lod_pixels"]
    lods = []
    for i, cname in enumerate(CFG["lod_collections"]):
        coll = bpy.data.collections.get(cname)
        if coll is None or not coll.objects:
            log(f"FEHLER: {cname} leer")
            return
        verts, uvs, by_mat = [], [], {}
        for o in coll.objects:
            v, u, bm = _mesh_world_tris(o)
            off = len(verts)
            verts.extend(v)
            uvs.extend(u)
            for mname, faces in bm.items():
                by_mat.setdefault(mname, []).extend(
                    (a + off, b + off, c + off) for a, b, c in faces
                )
        pix = pixels[i] if i < len(pixels) else max(4, 80 // (i + 1))
        n_tri = sum(len(f) for f in by_mat.values())
        log(f"{cname} -> {stem}_a{pix}: {len(verts)} verts, {n_tri} tris, mats={list(by_mat)}")
        lods.append((pix, verts, uvs, by_mat))
    dae_path = os.path.join(pack, f"{stem}.dae")
    known = _install_pack_textures(pack)
    mat_png = _write_multi_lod_dae(dae_path, lods, stem)
    for mname, files in known.items():
        cur = mat_png.setdefault(mname, {})
        if files.get("base") and not cur.get("base"):
            cur["base"] = files["base"]
    _write_materials_json(
        os.path.join(pack, "main.materials.json"),
        mat_png,
        "art/shapes/trees/trees_spruce",
    )
    import json

    asset_path = os.path.join(pack, f"{stem}.dae.asset.json")
    with open(asset_path, "w", encoding="utf-8") as f:
        json.dump({"imposters": []}, f, indent=2)
        f.write("\n")
    tools = CFG["autoroad_tools"]
    if tools and tools not in sys.path:
        sys.path.insert(0, tools)
    from tree_imposter_lib import install_spruce_far_lod

    install_spruce_far_lod(dae_path)
    names = " + ".join(f"a{p}" for p in pixels)
    log(f"DAE {dae_path} ({names}, vier Karten a80, Billboard 25).")
    log("Pack ins Level kopiert build_forest.py. BeamNG ganz beenden.")


RUN = ["step1_split", "step2_make_lods", "step3_report", "step4_export_dae"]

for _name in RUN:
    log(f"=== {_name} ===")
    globals()[_name]()
