"""Write optional composition guides for the native WAI workflow."""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from komaquill.adapters.atomic import atomic_bytes

GUIDE_PREFIX = "komaquill-guide-"
GUIDE_SIZE = 512
STANDING = (
    (0, 85),
    (0, 125),
    (-36, 132),
    (-57, 190),
    (-68, 245),
    (36, 132),
    (57, 190),
    (68, 245),
    (-24, 230),
    (-26, 315),
    (-34, 405),
    (24, 230),
    (26, 315),
    (34, 405),
)
POSE_SEGMENTS = (
    (1, 2, "#ff0000"),
    (1, 5, "#ff5500"),
    (2, 3, "#ffaa00"),
    (3, 4, "#ffff00"),
    (5, 6, "#aaff00"),
    (6, 7, "#55ff00"),
    (1, 8, "#00ff00"),
    (8, 9, "#00ff55"),
    (9, 10, "#00ffaa"),
    (1, 11, "#00ffff"),
    (11, 12, "#00aaff"),
    (12, 13, "#0055ff"),
    (1, 0, "#0000ff"),
)


def prepare_guides(input_directory: Path) -> None:
    """Place starter pose and depth images without replacing user inputs."""
    for name, centers in (
        ("standing-pose.png", (256,)),
        ("pair-pose.png", (144, 368)),
        ("center-depth.png", (256,)),
        ("pair-depth.png", (144, 368)),
    ):
        target = input_directory / f"{GUIDE_PREFIX}{name}"
        if target.exists():
            continue
        image = Image.new("RGB", (GUIDE_SIZE, GUIDE_SIZE), "black")
        canvas = ImageDraw.Draw(image)
        if name.endswith("-pose.png"):
            for center in centers:
                joints = [(center + x, y) for x, y in STANDING]
                for start, end, color in POSE_SEGMENTS:
                    canvas.line((joints[start], joints[end]), fill=color, width=8)
                    x, y = joints[end]
                    canvas.ellipse((x - 4, y - 4, x + 4, y + 4), fill=color)
        else:
            for y in range(GUIDE_SIZE):
                level = int(y * 112 / GUIDE_SIZE)
                canvas.line((0, y, GUIDE_SIZE - 1, y), fill=(level,) * 3)
            for center in centers:
                canvas.ellipse((center - 32, 55, center + 32, 119), fill=(204,) * 3)
                canvas.rounded_rectangle(
                    (center - 61, 124, center + 61, 423), radius=35, fill=(192,) * 3
                )
        with BytesIO() as encoded:
            image.save(encoded, format="PNG")
            atomic_bytes(target, encoded.getvalue())
