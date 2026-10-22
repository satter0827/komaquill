"""プロジェクト設定とパス。インポート時にはファイルを作らない。"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from komaquill.errors import KomaquillError


class Settings(BaseSettings):
    """通信と待機の設定。"""

    model_config = SettingsConfigDict(env_prefix="KOMAQUILL_", extra="forbid")
    poll_interval: float = Field(default=2.0, gt=0)
    request_timeout: float = Field(default=30.0, gt=0)
    generation_timeout: float = Field(default=600.0, gt=0)
    preparation_timeout: float = Field(default=900.0, gt=0)
    shutdown_timeout: float = Field(default=600.0, gt=0)
    execution_timeout: float = Field(default=86400.0, gt=0)
    control_timeout: float = Field(default=90.0, gt=0)
    local_port: int = Field(default=18188, ge=1024, le=65535)
    remote_port: int = Field(default=8188, ge=1024, le=65535)
    gpu: str = Field(default="T4", pattern=r"^(T4|L4|G4|H100|A100)$")


@dataclass(frozen=True)
class Project:
    """実行場所によらず一箇所に保存する。"""

    root: Path

    @property
    def artifacts(self) -> Path:
        return self.root / ".artifacts"

    @property
    def runtime(self) -> Path:
        return self.artifacts / "runtime"

    @property
    def session_file(self) -> Path:
        return self.artifacts / "runtime" / "session.json"

    @property
    def runs(self) -> Path:
        return self.artifacts / "runs"

    def settings(self) -> Settings:
        """TOMLの初期値と環境変数を読み込む。"""
        with (self.root / "komaquill.toml").open("rb") as handle:
            defaults = tomllib.load(handle)
        # 環境変数をTOMLより優先する。秘密情報は設定に含めない。
        for name in list(defaults):
            if f"KOMAQUILL_{name.upper()}" in os.environ:
                del defaults[name]
        return Settings(**defaults)


def find_project(explicit: Path | None = None) -> Project:
    """設定ファイルのあるプロジェクトを探索する。"""
    start = explicit or Path(os.environ.get("KOMAQUILL_PROJECT_DIR", Path.cwd()))
    start = start.resolve()
    candidates = [start] if explicit else [start, *start.parents]
    for candidate in candidates:
        if (candidate / "komaquill.toml").is_file():
            return Project(candidate)
    raise KomaquillError("作業フォルダーが見つかりません。--project で指定してください。")
