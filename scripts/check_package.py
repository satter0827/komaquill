"""配布物へ生成物が入らず、バージョンが一致することを確認する。"""

import os
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from importlib.metadata import version
from pathlib import Path


def main() -> None:
    """wheel、sdist、宣言、タグのバージョンを照合する。"""
    root = Path(__file__).resolve().parents[1]
    expected = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    if expected != version("komaquill"):
        raise SystemExit("環境を同期し、宣言とインストール済みバージョンを一致させてください。")
    if (
        os.environ.get("GITHUB_REF_TYPE") == "tag"
        and os.environ.get("GITHUB_REF_NAME") != f"v{expected}"
    ):
        raise SystemExit("タグとバージョンが一致しません。")
    wheel_path = root / f".artifacts/dist/komaquill-{expected}-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(wheel.read(metadata_name).decode())
        if metadata["Version"] != expected:
            raise SystemExit("wheelのバージョンが一致しません。")
        required = {"workflow.json", "workflow-api.json", "model.toml", "runtime.toml"}
        if not all(f"komaquill/resources/{name}" in names for name in required):
            raise SystemExit("配布するワークフローまたは設定が不足しています。")
    with tarfile.open(root / f".artifacts/dist/komaquill-{expected}.tar.gz") as archive:
        names += archive.getnames()
        member = archive.extractfile(f"komaquill-{expected}/PKG-INFO")
        if member is None or Parser().parsestr(member.read().decode())["Version"] != expected:
            raise SystemExit("sdistのバージョンが一致しません。")
    if any({".artifacts", "artifacts", ".env"}.intersection(Path(name).parts) for name in names):
        raise SystemExit("配布物に生成物または資格情報が含まれています。")
    print("バージョンと配布対象を確認しました。")


if __name__ == "__main__":
    main()
