from pydantic import Field
from pydantic_settings import BaseSettings


class WorkbenchConfig(BaseSettings):
    WORKBENCH_ENABLED: bool = False
    # Missing tenant/empty list means all its members; staging can open named accounts only.
    WORKBENCH_ALLOWED_ACCOUNTS: dict[str, list[str]] = Field(default_factory=dict)
    # JSON object mapping tenant IDs to the one administrator-published template Agent.
    WORKBENCH_AGENT_TEMPLATES: dict[str, str] = Field(default_factory=dict)
    # tenant -> public tool id -> parameter -> JSON schema. Never infer publicity from names.
    WORKBENCH_TOOL_PARAMETERS: dict[str, dict[str, dict[str, dict]]] = Field(default_factory=dict)
    WORKBENCH_PER_USER_RUNS: int = Field(default=2, ge=1, le=10)
    WORKBENCH_GLOBAL_RUNS: int = Field(default=20, ge=1, le=100)
    WORKBENCH_MAX_ACTIVE_USERS: int = Field(default=10, ge=1, le=100)
    WORKBENCH_SPEECH_PROVIDER: str = ""
    WORKBENCH_SPEECH_MODEL: str = ""
    WORKBENCH_SANDBOX_MANAGER_URL: str = "http://workbench-sandbox-manager:5010"
    WORKBENCH_SANDBOX_MANAGER_TOKEN: str = ""
