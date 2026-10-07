"""Video post-processing shared by local and container execution."""
from pathlib import Path
import re

from .agent import CommandRunner
from .store import artifact


def extract_frames(video: Path, folder: Path, root: Path, stop=lambda: None, *, ffmpeg="ffmpeg"):
    """Post-process actual recording at 10 s intervals. Gameplay never waits here."""
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "extract.log"
    CommandRunner().run([ffmpeg, "-y", "-loglevel", "info", "-i", str(video),
        "-vf", "select='isnan(prev_selected_t)+gte(t-prev_selected_t,10)',showinfo",
        "-fps_mode", "vfr", str(folder / "frame-%06d.png")], folder, log, tick=stop)
    times = re.findall(r"\bn:\s*\d+\s+pts:\s*\S+\s+pts_time:([0-9.eE+-]+)", log.read_text())
    images = sorted(folder.glob("frame-*.png"))
    if not images or len(images) != len(times):
        raise ValueError("Cannot associate recorded keyframes with video timestamps")
    return [{"clock": "video", "time_seconds": float(t), "image": artifact(p, root)}
            for p, t in zip(images, times)]
