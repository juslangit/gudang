#!/usr/bin/env python3
"""
gudang inspector — the outside half.

    python3 src/inspect.py <file-or-folder> [...] [--out out/] [--timeout 120]

Blender is a big program and some files will crash it or hang it. That is not a
bug to be surprised by later; it is the normal weather for a tool that accepts
whatever people drop on it. So every file gets its own Blender process with a
time limit, and a file that kills Blender produces a report saying so rather
than taking the whole run down with it.

Output is one JSON file per model, plus a summary.json for the batch.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BLENDER_SIDE = HERE / "inspect_blend.py"

MODEL_EXTENSIONS = {
    ".blend", ".glb", ".gltf", ".fbx", ".obj", ".stl",
    ".ply", ".dae", ".usd", ".usdz", ".usda", ".usdc", ".abc",
}

# Where Blender tends to be, in the order we try.
BLENDER_CANDIDATES = [
    "blender",
    "/opt/homebrew/bin/blender",
    "/usr/local/bin/blender",
    "/Applications/Blender.app/Contents/MacOS/Blender",
    r"C:\Program Files\Blender Foundation\Blender\blender.exe",
]


def find_blender():
    env = os.environ.get("BLENDER")
    if env and Path(env).exists():
        return env
    for candidate in BLENDER_CANDIDATES:
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if found:
            return found
    sys.exit(
        "Blender not found. Install it, or point at it:  export BLENDER=/path/to/blender"
    )


def collect(targets):
    """Every model file under the given files and folders, sorted."""
    files = []
    for target in targets:
        p = Path(target).expanduser()
        if p.is_dir():
            files += [
                f for f in p.rglob("*")
                if f.is_file() and f.suffix.lower() in MODEL_EXTENSIONS
            ]
        elif p.is_file():
            files.append(p)
    return sorted(set(files))


def inspect_one(blender, model, out_dir, timeout):
    """One file, one Blender, one report. Never raises."""
    stem = model.stem.replace(" ", "_")
    # keep a bit of the parent folder so player_red/scene.gltf and
    # player_blue/scene.gltf do not overwrite each other
    out_file = out_dir / f"{model.parent.name}__{stem}{model.suffix.replace('.', '_')}.json"

    started = time.monotonic()
    try:
        proc = subprocess.run(
            [
                blender, "--background", "--factory-startup",
                "--python", str(BLENDER_SIDE),
                "--", str(model), str(out_file),
            ],
            capture_output=True, text=True, timeout=timeout,
        )
        elapsed = time.monotonic() - started

        if out_file.exists():
            report = json.loads(out_file.read_text())
        else:
            report = {
                "ok": False,
                "error": "blender wrote no report",
                "returncode": proc.returncode,
                "stderr_tail": proc.stderr[-800:],
                "source": {"path": str(model), "name": model.name},
            }
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        report = {
            "ok": False,
            "error": f"timed out after {timeout}s",
            "source": {"path": str(model), "name": model.name},
        }
    except Exception as exc:
        elapsed = time.monotonic() - started
        report = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "source": {"path": str(model), "name": model.name},
        }

    report["seconds"] = round(elapsed, 2)
    out_file.write_text(json.dumps(report, indent=2))
    return report


def one_line(report):
    """What a person wants to see scroll past while it works."""
    src = report.get("source", {})
    name = src.get("name", "?")
    secs = report.get("seconds", 0)

    if not report.get("ok"):
        return f"  FAIL  {name:<42} {secs:>6.1f}s  {report.get('error', '')[:60]}"

    t = report["totals"]
    scale = report.get("scale") or {}
    longest = scale.get("longest_edge_m")
    size = f"{longest:.2f}m" if longest is not None else "—"
    flags = []
    if t.get("non_manifold_edges"):
        flags.append(f"{t['non_manifold_edges']} non-manifold")
    if t.get("inconsistent_winding_edges"):
        flags.append(f"{t['inconsistent_winding_edges']} inconsistent")
    if t.get("flipped_faces"):
        flags.append(f"{t['flipped_faces']} flipped")
    if t.get("ngons"):
        flags.append(f"{t['ngons']} n-gons")
    if report.get("watertight") is False:
        flags.append("open")
    return (
        f"  ok    {name:<42} {secs:>6.1f}s  "
        f"{t['triangles']:>8,} tris  {size:>8}  {', '.join(flags)}"
    )


def main():
    ap = argparse.ArgumentParser(description="Measure what is inside model files.")
    ap.add_argument("targets", nargs="+", help="files or folders to inspect")
    ap.add_argument("--out", default="out", help="where reports go (default: out/)")
    ap.add_argument("--timeout", type=int, default=120, help="seconds per file")
    args = ap.parse_args()

    blender = find_blender()
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    files = collect(args.targets)
    if not files:
        sys.exit("no model files found")

    print(f"blender: {blender}")
    print(f"files:   {len(files)}")
    print()

    reports = []
    batch_started = time.monotonic()
    for i, model in enumerate(files, 1):
        print(f"[{i}/{len(files)}]", end=" ", flush=True)
        report = inspect_one(blender, model, out_dir, args.timeout)
        reports.append(report)
        print(one_line(report).strip())

    elapsed = time.monotonic() - batch_started
    ok = [r for r in reports if r.get("ok")]
    failed = [r for r in reports if not r.get("ok")]

    summary = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "blender": (ok[0]["blender"] if ok else None),
        "files": len(files),
        "ok": len(ok),
        "failed": len(failed),
        "seconds_total": round(elapsed, 1),
        "seconds_per_file": round(elapsed / len(files), 2),
        "failures": [
            {"name": r["source"]["name"], "error": r.get("error")} for r in failed
        ],
        "totals": {
            key: sum(r["totals"].get(key, 0) for r in ok)
            for key in (
                "triangles", "ngons", "non_manifold_edges", "boundary_edges",
                "inconsistent_winding_edges", "flipped_faces", "loose_vertices",
            )
        },
        "watertight_models": sum(1 for r in ok if r.get("watertight")),
        "models_without_uvs": sum(
            1 for r in ok
            if r["objects"] and not any(o["uv"]["has_uvs"] for o in r["objects"])
        ),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    print()
    print(f"read {len(ok)}/{len(files)} files in {elapsed:.0f}s "
          f"({summary['seconds_per_file']}s each)")
    if failed:
        print(f"could not read {len(failed)}:")
        for f in failed:
            print(f"  {f['source']['name']}: {f.get('error', '')[:70]}")


if __name__ == "__main__":
    main()
