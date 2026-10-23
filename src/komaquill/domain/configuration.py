"""Define immutable runtime settings without reading the environment."""

from pydantic import BaseModel, ConfigDict, Field


class RuntimeSettings(BaseModel):
    """Keep the effective settings shared by one connection."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    poll_interval: float = Field(gt=0)
    request_timeout: float = Field(gt=0)
    generation_timeout: float = Field(gt=0)
    preparation_timeout: float = Field(gt=0)
    shutdown_timeout: float = Field(gt=0)
    execution_timeout: float = Field(gt=0)
    control_timeout: float = Field(gt=0)
    local_port: int = Field(ge=1024, le=65535)
    remote_port: int = Field(ge=1024, le=65535)
    gpu: str = Field(pattern=r"^(T4|L4|G4|H100|A100)$")
