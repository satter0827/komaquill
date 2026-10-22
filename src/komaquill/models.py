"""設定と保存データの型。"""

from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

JsonObject = dict[str, Any]


class ModelPreset(BaseModel):
    """取得するモデルを不変のリビジョンで指定する。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    repo_id: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license: str


class Session(BaseModel):
    """接続先と遠隔ランタイムの識別情報。秘密情報は含めない。"""

    endpoint: str
    runtime_id: UUID
    backend: Literal["colab", "external"] = "colab"
    source_revision: str | None = None
    comfy_revision: str | None = None
    frontend_version: str | None = None
    model_sha256: str | None = None
    username: str | None = None
    excluded_prompt_ids: set[str] = Field(default_factory=set)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str | None) -> str | None:
        """Basic認証の区切り文字と空の利用者名を拒否する。"""
        if value is not None and (not value.strip() or any(char in value for char in ":\r\n")):
            raise ValueError("Basic認証の利用者名を確認してください。")
        return value

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        """TLSまたはローカル接続だけを許可する。"""
        url = urlsplit(value)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
            or (url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"})
        ):
            raise ValueError("接続先には認証情報を含めず、HTTPSのルートURLを指定してください。")
        return value.rstrip("/")


class ImageRef(BaseModel):
    """ComfyUIが返す画像への参照。"""

    filename: str
    subfolder: str = ""
    type: Literal["output", "temp"] = "output"


class Run(BaseModel):
    """再接続後も回収を再開するための履歴。"""

    run_id: UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    runtime_id: UUID
    prompt_id: str | None = None
    state: Literal["prepared", "unknown", "queued", "saved", "failed"] = "prepared"
    app_version: str
    source_revision: str
    remote_source_revision: str | None
    comfy_revision: str | None
    frontend_version: str | None
    model_sha256: str | None
    workflow_sha256: str
    images: list[str] = Field(default_factory=list)
    error: str | None = None
