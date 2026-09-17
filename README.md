# gudang

The receiving bay every 3D asset passes through on its way into a game.

Drop in a Sketchfab download, a Meshy generation or a `.blend`. Gudang opens it in
Blender with no window, measures what is actually in it, judges it against the engine
you are aiming at, keeps the attempts apart, and hands back something ready to use.

**Status:** M1 — the inspector works. Nothing else is built yet.

## The inspector

    python3 src/inspect.py <file-or-folder> [...] [--out out/] [--timeout 120]

It measures; it does not judge. Whether 180,000 triangles is "too many" depends on
your engine and on whether this is a hero prop or a background crate — that is M2's
job, kept deliberately separate so the rules can be argued with and changed without
re-opening a single file in Blender.

Per model it reports: triangle, face and vertex counts; n-gons, quads and tris; real
size in metres and how far the origin sits from the body; watertightness, open borders
and true non-manifold edges; inconsistent winding and — only on closed meshes —
inside-out faces; UV maps, coverage and pieces outside the 0–1 square; textures with
resolution and whether they are power-of-two; armatures, bones, animation clips and
shape keys; materials and empty material slots; and whether object scale has been
applied.

Every file gets its own Blender process with a time limit, so a file that crashes or
hangs Blender produces a report saying so instead of taking the run down with it.

Output is one JSON file per model plus a `summary.json` for the batch.

## What it found on 75 real files

Read 75/75 in 73 seconds, about a second each, on Blender 5.2.1 LTS.

- **16 of 32 Sketchfab downloads are at the wrong scale. 0 of 33 Meshy files are.**
  A WW2 fighter plane 1,757 m long, a submarine at 1,476 m, a plastic chair 76 m tall
  sitting 250 m from its own origin.
- **The same model is stored many times over.** Six files share `player_blue`'s exact
  triangle count, six more share `player_red`'s, and five share `official`'s.
- Five files — `athlete_average`, `athlete_stocky`, `athlete_tall`, `athlete_wiry`
  and `spectator` — have an identical 2,816 triangles, so the four "body types" are
  very probably one mesh at different scales.

## Requirements

Blender 4.x or newer on the PATH, or `export BLENDER=/path/to/blender`. Python 3.
No packages.
