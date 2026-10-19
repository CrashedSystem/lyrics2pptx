"""Build the install-free Windows package (ADR-033).

Run it from the repository root with the development Python::

    python -X utf8 packaging\\build.py

It produces::

    dist\\lyrics2pptx\\                      <- the folder the user unzips
        lyrics2pptx.exe                     python + lxml + Pillow + web/ inside
        template.pptx                       slide source the tool clones (37 MB)
        사용법.txt                           one page: run it, use it, report a problem
    dist\\lyrics2pptx-<version>-win64.zip    <- the thing to hand over

Why a frozen exe: the beta promise is "unzip, double-click, done" — no Python
install, no pip, no shortcut to create. ``--onefile`` keeps that promise down to
one visible program file, ``--noconsole`` stops a black window from flashing, and
``config.py`` understands the frozen layout (``sys.frozen``) so ``template.pptx``
and ``output/`` sit next to the exe while ``web/`` travels inside it.

This is a developer tool that lives outside ``src/`` (like ``scripts/``), so it may
import anything and is not covered by the module-boundary tests (P1–P9).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
DIST = ROOT / "dist"
BUILD = ROOT / "build"

APP_NAME = "lyrics2pptx"


def _version() -> str:
    """The version from ``pyproject.toml`` — one source of truth (build layer 08)."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _make_icon() -> Path | None:
    """Draw ``packaging/icon.ico`` (accent square + 가). Best effort, may skip.

    A distinct icon is how a non-technical user tells the program apart from the
    data files in the unzipped folder; if Pillow or the font is missing we build
    without one rather than failing the release.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:  # pragma: no cover - Pillow ships with python-pptx
        print("[icon] Pillow 이 없어 아이콘 없이 빌드합니다")
        return None
    font = None
    for candidate in ("malgunbd.ttf", "malgun.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(candidate, 150)
            break
        except OSError:
            continue
    if font is None:  # pragma: no cover - every Windows has one of the three
        print("[icon] 쓸 수 있는 글꼴이 없어 아이콘 없이 빌드합니다")
        return None

    size = 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # Same accent as the web UI (web/style.css --accent).
    draw.rounded_rectangle((10, 10, size - 10, size - 10), radius=52, fill=(79, 70, 229, 255))
    box = draw.textbbox((0, 0), "가", font=font)
    draw.text(
        ((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]),
        "가",
        font=font,
        fill=(255, 255, 255, 255),
    )
    target = PACKAGING / "icon.ico"
    image.save(target, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"[icon] {target.relative_to(ROOT)}")
    return target


def _run_pyinstaller(icon: Path | None) -> Path:
    """Freeze ``packaging/entry.py`` into ``dist/lyrics2pptx.exe``."""
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--noconsole",
        "--name",
        APP_NAME,
        "--paths",
        str(ROOT / "src"),
        # Absolute source: PyInstaller resolves relative *data* paths against
        # ``--specpath`` (build/), not the working directory. ``web`` inside the
        # bundle keeps the name ``web``, which is where config.py looks (ADR-033).
        "--add-data",
        f"{ROOT / 'web'}{os.pathsep}web",
        "--distpath",
        "dist",
        "--workpath",
        "build",
        "--specpath",
        "build",
    ]
    if icon is not None:
        command += ["--icon", str(icon)]
    command.append(str(PACKAGING / "entry.py"))
    print("[1/3] 얼리기:", " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)
    exe = DIST / f"{APP_NAME}.exe"
    if not exe.is_file():
        raise SystemExit(f"PyInstaller 가 실행 파일을 만들지 않았습니다: {exe}")
    return exe


def _assemble(exe: Path, version: str) -> Path:
    """Lay out the folder the user unzips: exe + template + 사용법.txt."""
    release = DIST / APP_NAME
    if release.exists():
        shutil.rmtree(release)
    release.mkdir(parents=True)
    shutil.copy2(exe, release / exe.name)
    template = ROOT / "template.pptx"
    if not template.is_file():
        raise SystemExit(f"동봉할 파일이 없습니다: {template}")
    shutil.copy2(template, release / template.name)
    # 사용법.txt carries the build version, so a user pasting the file into a bug
    # report tells us exactly which build they ran. CRLF + UTF-8 for Notepad.
    usage = PACKAGING / "사용법.txt"
    if not usage.is_file():
        raise SystemExit(f"동봉할 파일이 없습니다: {usage}")
    (release / usage.name).write_text(
        usage.read_text(encoding="utf-8").replace("__VERSION__", version),
        encoding="utf-8",
        newline="\r\n",
    )
    if (PACKAGING / "icon.ico").is_file():
        shutil.copy2(PACKAGING / "icon.ico", release / f"{APP_NAME}.ico")
    print(f"[2/3] 동봉 폴더: {release.relative_to(ROOT)}\\  (버전 {version})")
    for path in sorted(release.iterdir()):
        print(f"        {path.name:24s} {path.stat().st_size / 1_048_576:7.1f} MB")
    return release


def _zip_folder(release: Path, version: str) -> Path:
    """Zip it with the folder at the top level, so unzipping makes one folder."""
    archive = DIST / f"{APP_NAME}-{version}-win64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(release.rglob("*")):
            if path.is_file():
                zf.write(path, Path(release.name) / path.relative_to(release))
    print(f"[3/3] 배포 파일: dist\\{archive.name}  ({archive.stat().st_size / 1_048_576:.1f} MB)")
    return archive


def main() -> int:
    version = _version()
    print(f"{APP_NAME} {version} 패키지 빌드 (ADR-033)")
    icon = _make_icon()
    exe = _run_pyinstaller(icon)
    release = _assemble(exe, version)
    archive = _zip_folder(release, version)
    print(
        "\n완료. 사용자에게 줄 파일은 하나입니다:\n"
        f"  {archive}\n"
        "압축을 풀면 폴더가 하나 나오고, 그 안의 lyrics2pptx.exe 를 더블클릭하면 끝입니다."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
