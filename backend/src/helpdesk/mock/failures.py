"""Provide finite, operator-controlled faults in the running mock API."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FailureConfig(BaseModel):
    """Bound synthetic faults so the demo can recover without a service restart."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["off", "rate_limit", "server_error", "slow", "committed_timeout"] = "off"
    target: Literal["any", "read", "write"] = "any"
    remaining: int = Field(default=2, ge=0, le=100)
    delay_seconds: float = Field(default=4, ge=0, le=30)
    retry_after_seconds: float = Field(default=0.2, ge=0, le=60)


class FailureSwitch:
    """Claim each finite fault before awaiting, within one mock worker."""

    def __init__(self, config=None):
        self.config = config or FailureConfig()

    def take(self, method, payload):
        """Consume one matching fault without affecting auth or operator routes."""
        config = self.config
        writing = method == "POST"
        if (config.mode == "off" or config.remaining == 0
                or config.target == "read" and writing or config.target == "write" and not writing
                or config.mode == "committed_timeout" and not (writing and payload.get("approval_id"))):
            return None
        selected = config.model_copy()
        config.remaining -= 1
        return selected
