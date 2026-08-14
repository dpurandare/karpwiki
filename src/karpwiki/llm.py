"""Per-agent-role model resolution (09 §16).

The effective model for an agent is the workspace's `SCHEMA.md` override if it sets one,
otherwise the platform default. The value is a single Pydantic AI `provider:model` string,
so switching provider and switching model are the same operation and no caller branches on
provider. Credentials are not here — they resolve from the secrets manager (09 §13).
"""

from typing import Literal

from . import config

AgentRole = Literal["classifier", "curator"]

_PLATFORM_DEFAULTS: dict[str, str] = {
    "classifier": config.LLM_CLASSIFIER_MODEL,
    "curator": config.LLM_CURATOR_MODEL,
}


class ModelNotConfiguredError(RuntimeError):
    """Neither the workspace nor the platform defines a model for this role."""


def resolve_model(role: AgentRole, schema: dict | None = None) -> str:
    """Return the `provider:model` string for `role` in a workspace with this SCHEMA.md."""
    override = ((schema or {}).get("llm") or {}).get(role, {}).get("model")
    model = override or _PLATFORM_DEFAULTS.get(role) or ""
    if not model:
        raise ModelNotConfiguredError(
            f"no model configured for the {role} agent: set llm.{role}.model in the "
            f"workspace's SCHEMA.md, or KARPWIKI_LLM_{role.upper()}_MODEL"
        )
    return model
