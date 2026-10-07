"""Fixed asset fixture; no model calls or external assets."""
import argparse
from pathlib import Path
import sys
import bpy

parser = argparse.ArgumentParser()
parser.add_argument('--output', required=True)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_cube_add(size=1)
bpy.context.object.name = 'FixtureCube'
# Relative scratch writes must affect only the per-job copy.
Path('scratch.txt').write_text('Blender scratch fixture')
bpy.ops.export_scene.gltf(filepath=args.output, export_format='GLB')
