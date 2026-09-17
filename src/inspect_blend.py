"""
Runs INSIDE Blender, with no window open.

    blender --background --factory-startup --python src/inspect_blend.py -- <model> <out.json>

It opens one model file and writes down what is in it, as JSON.

It measures. It does not judge. Whether 180,000 triangles is "too many" depends
on whether you are aiming at Godot or Unreal, and on whether this is a hero prop
or a background crate — that decision is M2's job, and it lives somewhere else on
purpose. Keeping measuring and judging apart means the rules can be argued with
and changed without ever re-opening a single file in Blender.
"""

import json
import math
import os
import sys
import traceback

import bpy
import bmesh
from mathutils import Vector


# ---------------------------------------------------------------- importing

# Blender renamed several importers over the years. Each entry is the list of
# operators to try for that extension, newest name first.
IMPORTERS = {
    ".glb":  [("import_scene", "gltf")],
    ".gltf": [("import_scene", "gltf")],
    ".fbx":  [("import_scene", "fbx")],
    ".obj":  [("wm", "obj_import"), ("import_scene", "obj")],
    ".stl":  [("wm", "stl_import"), ("import_mesh", "stl")],
    ".ply":  [("wm", "ply_import"), ("import_mesh", "ply")],
    ".dae":  [("wm", "collada_import")],
    ".usd":  [("wm", "usd_import")],
    ".usdz": [("wm", "usd_import")],
    ".usda": [("wm", "usd_import")],
    ".usdc": [("wm", "usd_import")],
    ".abc":  [("wm", "alembic_import")],
}


def load(path):
    """Get the file open in Blender. Returns the name of how we opened it."""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
        return "open_mainfile"

    # Start from an empty scene so the default cube never lands in the numbers.
    bpy.ops.wm.read_factory_settings(use_empty=True)

    if ext not in IMPORTERS:
        raise ValueError(f"no importer for {ext}")

    last = None
    for module, op in IMPORTERS[ext]:
        fn = getattr(getattr(bpy.ops, module, None), op, None)
        if fn is None:
            continue
        try:
            # collada wants a different keyword than everything else
            if op == "collada_import":
                fn(filepath=path)
            else:
                fn(filepath=path)
            return f"{module}.{op}"
        except Exception as exc:  # try the next name on the list
            last = exc
    raise RuntimeError(f"every importer failed for {ext}: {last}")


# ---------------------------------------------------------------- measuring

def mesh_objects():
    return [o for o in bpy.data.objects if o.type == "MESH"]


def evaluated_mesh(obj, depsgraph):
    """The mesh as it really is — after modifiers, the way an exporter sees it."""
    eval_obj = obj.evaluated_get(depsgraph)
    return eval_obj.to_mesh()


def measure_geometry(obj, depsgraph):
    """Counts of the things a game engine cares about, for one object."""
    me = evaluated_mesh(obj, depsgraph)
    try:
        me.calc_loop_triangles()

        tris = quads = ngons = 0
        for poly in me.polygons:
            n = poly.loop_total
            if n == 3:
                tris += 1
            elif n == 4:
                quads += 1
            else:
                ngons += 1

        # bmesh is how you ask the topology questions: is it watertight, are any
        # faces inside out, are there stray vertices joined to nothing.
        bm = bmesh.new()
        bm.from_mesh(me)

        # Blender calls an open border "non-manifold" too, but a single-sided
        # game asset is *meant* to be open — a curtain, a signboard, a chair
        # shell. Counting those as defects made a perfectly good chair look
        # broken in 812 places, so the two are kept apart:
        #   boundary    — an edge with one face. An open border. Usually fine.
        #   non-manifold — an edge with three or more faces, or a wire edge
        #                  with none. Geometry an engine cannot resolve.
        boundary_edges = sum(1 for e in bm.edges if len(e.link_faces) == 1)
        non_manifold_edges = sum(
            1 for e in bm.edges if len(e.link_faces) > 2 or len(e.link_faces) == 0
        )
        loose_verts = sum(1 for v in bm.verts if not v.link_edges)
        loose_edges = sum(1 for e in bm.edges if not e.link_faces)
        watertight = boundary_edges == 0 and non_manifold_edges == 0

        # Inside-out faces, done two ways, because the easy way only works on a
        # closed mesh.
        #
        # On any mesh, neighbouring faces should walk their shared edge in
        # opposite directions — that is what makes a surface consistently wound.
        # Where two faces walk it the same way, one of them is inside out
        # relative to the other. This is well defined even on an open shell.
        inconsistent = 0
        for e in bm.edges:
            if len(e.link_loops) == 2:
                l1, l2 = e.link_loops
                if l1.vert is l2.vert:
                    inconsistent += 1

        # On a closed mesh we can go further and say which way is *out*, by
        # asking Blender to recalculate and counting the faces that moved.
        # On an open mesh "outside" has no meaning, so we do not guess.
        if watertight:
            before = [f.normal.copy() for f in bm.faces]
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
            flipped = sum(
                1 for f, was in zip(bm.faces, before)
                if was.length > 0 and f.normal.dot(was) < 0
            )
        else:
            flipped = None

        bm.free()

        return {
            "vertices": len(me.vertices),
            "edges": len(me.edges),
            "faces": len(me.polygons),
            "triangles": len(me.loop_triangles),
            "tris": tris,
            "quads": quads,
            "ngons": ngons,
            "non_manifold_edges": non_manifold_edges,
            "boundary_edges": boundary_edges,
            "watertight": watertight,
            "loose_vertices": loose_verts,
            "loose_edges": loose_edges,
            "inconsistent_winding_edges": inconsistent,
            # None means "not knowable" — the mesh is open, so there is no
            # outside to be flipped away from. Not the same as zero.
            "flipped_faces": flipped,
        }
    finally:
        obj.evaluated_get(depsgraph).to_mesh_clear()


def measure_uvs(obj, depsgraph):
    """How the flat texture map is laid out, if there is one at all."""
    me = evaluated_mesh(obj, depsgraph)
    try:
        if not me.uv_layers:
            return {"maps": 0, "has_uvs": False}

        layer = me.uv_layers[0].data
        me.calc_loop_triangles()

        area = 0.0
        outside = 0
        for tri in me.loop_triangles:
            a, b, c = (Vector(layer[i].uv) for i in tri.loops)
            # area of a triangle in UV space
            area += abs((b - a).cross(c - a)) / 2.0
        for loop in layer:
            u, v = loop.uv
            if u < -1e-6 or u > 1 + 1e-6 or v < -1e-6 or v > 1 + 1e-6:
                outside += 1

        return {
            "maps": len(me.uv_layers),
            "has_uvs": True,
            "names": [l.name for l in me.uv_layers],
            # 1.0 means the UVs exactly fill the square; above 1.0 means pieces
            # are stacked on top of each other or spill outside it.
            "uv_area": round(area, 5),
            "loops_outside_0_1": outside,
            "loops_total": len(layer),
        }
    finally:
        obj.evaluated_get(depsgraph).to_mesh_clear()


def measure_scale():
    """How big the whole thing is, in metres, as the engine will see it."""
    objs = mesh_objects()
    if not objs:
        return None

    lo = Vector((math.inf,) * 3)
    hi = Vector((-math.inf,) * 3)
    for o in objs:
        for corner in o.bound_box:
            world = o.matrix_world @ Vector(corner)
            lo = Vector((min(lo[i], world[i]) for i in range(3)))
            hi = Vector((max(hi[i], world[i]) for i in range(3)))

    size = hi - lo
    unit = bpy.context.scene.unit_settings
    return {
        "dimensions_m": [round(v, 4) for v in size],
        "longest_edge_m": round(max(size), 4),
        "unit_scale_length": round(unit.scale_length, 6),
        "unit_system": unit.system,
        # A model whose origin is far from its own body is a pivot problem.
        "centre_offset_m": [round(v, 4) for v in (lo + size / 2)],
    }


def measure_textures():
    out = []
    for img in bpy.data.images:
        if img.name == "Render Result" or img.size[0] == 0:
            continue
        out.append({
            "name": img.name,
            "width": img.size[0],
            "height": img.size[1],
            "channels": img.channels,
            "file_format": img.file_format,
            "packed": bool(img.packed_file),
            # a texture that is not a power of two upsets some engines
            "power_of_two": all(
                v > 0 and (v & (v - 1)) == 0 for v in (img.size[0], img.size[1])
            ),
        })
    return out


def measure_rig():
    armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    shape_keys = sum(
        1 for o in mesh_objects()
        if o.data.shape_keys and len(o.data.shape_keys.key_blocks) > 1
    )
    return {
        "armatures": len(armatures),
        "bones": sum(len(a.data.bones) for a in armatures),
        "actions": len(bpy.data.actions),
        "action_names": [a.name for a in bpy.data.actions][:20],
        "meshes_with_shape_keys": shape_keys,
    }


# ---------------------------------------------------------------- the report

def inspect(path):
    opened_with = load(path)
    depsgraph = bpy.context.evaluated_depsgraph_get()

    objects = []
    totals = {
        "vertices": 0, "triangles": 0, "faces": 0, "ngons": 0,
        "non_manifold_edges": 0, "boundary_edges": 0, "loose_vertices": 0,
        "inconsistent_winding_edges": 0, "flipped_faces": 0,
    }

    for obj in mesh_objects():
        geo = measure_geometry(obj, depsgraph)
        uv = measure_uvs(obj, depsgraph)
        objects.append({
            "name": obj.name,
            "geometry": geo,
            "uv": uv,
            "material_slots": len(obj.material_slots),
            "empty_material_slots": sum(
                1 for s in obj.material_slots if s.material is None
            ),
            "modifiers": [m.type for m in obj.modifiers],
            "scale": [round(v, 5) for v in obj.scale],
            # a scale that is not 1,1,1 on export bites people later
            "scale_applied": all(abs(v - 1.0) < 1e-4 for v in obj.scale),
        })
        for k in totals:
            # flipped_faces is None on an open mesh; unknown is not zero, so it
            # simply does not contribute to the total.
            totals[k] += geo.get(k) or 0

    return {
        "ok": True,
        "opened_with": opened_with,
        "blender": bpy.app.version_string,
        "scene": {
            "objects_total": len(bpy.data.objects),
            "mesh_objects": len(mesh_objects()),
            "empties": sum(1 for o in bpy.data.objects if o.type == "EMPTY"),
            "cameras": sum(1 for o in bpy.data.objects if o.type == "CAMERA"),
            "lights": sum(1 for o in bpy.data.objects if o.type == "LIGHT"),
            "collections": len(bpy.data.collections),
        },
        "totals": totals,
        "watertight": all(o["geometry"]["watertight"] for o in objects) if objects else None,
        "scale": measure_scale(),
        "materials": {
            "count": len(bpy.data.materials),
            "names": [m.name for m in bpy.data.materials][:30],
        },
        "textures": measure_textures(),
        "rig": measure_rig(),
        "objects": objects,
    }


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    model_path, out_path = argv[0], argv[1]

    try:
        report = inspect(model_path)
    except Exception as exc:
        report = {
            "ok": False,
            "blender": bpy.app.version_string,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=6),
        }

    report["source"] = {
        "path": model_path,
        "name": os.path.basename(model_path),
        "extension": os.path.splitext(model_path)[1].lower(),
        "bytes": os.path.getsize(model_path) if os.path.exists(model_path) else None,
    }

    with open(out_path, "w") as fh:
        json.dump(report, fh, indent=2)


main()
