"""Blackbox AI provider profile.

Blackbox is an OpenAI-compatible aggregator (https://api.blackbox.ai/v1) that
hosts reasoning models such as NVIDIA Nemotron. Reasoning models emit their
chain-of-thought in a separate ``reasoning_content`` field and the answer in
``content``. With no output cap, heavy reasoning can consume the whole
completion budget and the response comes back with ``finish_reason="length"``
and an EMPTY ``content`` — which Hermes renders as a blank / ``(empty)``
assistant message.

This profile fixes that two ways:

* ``default_max_tokens=16384`` — gives the visible answer headroom so reasoning
  can't starve it. (This is the actual empty-message fix.)
* :meth:`build_api_kwargs_extras` forwards the reasoning config as
  ``extra_body.reasoning`` (OpenRouter-style), clamping ``xhigh`` → ``high``
  since Blackbox's gateway accepts ``high`` but not ``xhigh``.

Reasoning is only forwarded when the transport reports the route as
reasoning-capable (``supports_reasoning``); see
``AIAgent._supports_reasoning_extra_body`` which recognises ``api.blackbox.ai``.
"""

from __future__ import annotations

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class BlackboxProfile(ProviderProfile):
    """Blackbox AI — reasoning forwarded as extra_body.reasoning (xhigh→high)."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        supports_reasoning: bool = False,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        if not supports_reasoning:
            return extra_body, {}

        if reasoning_config is not None:
            rc = dict(reasoning_config)
            if rc.get("enabled") is False:
                # Disabled reasoning: omit the field entirely.
                return extra_body, {}
            # Blackbox's gateway rejects the xhigh tier — clamp to high.
            effort = (rc.get("effort") or "").strip().lower()
            if effort in {"xhigh", "max"}:
                rc["effort"] = "high"
            extra_body["reasoning"] = rc
        else:
            extra_body["reasoning"] = {"enabled": True, "effort": "medium"}
        return extra_body, {}


blackbox = BlackboxProfile(
    name="blackbox",
    aliases=("blackboxai", "blackbox-ai"),
    # BLACKBOX_BASE_URL lets users point at a relay; the auth registry routes
    # the *_BASE_URL entry to base_url and the rest to the API key.
    env_vars=("BLACKBOX_API_KEY", "BLACKBOX_BASE_URL"),
    display_name="Blackbox AI",
    description="Blackbox AI — OpenAI-compatible aggregator (hosts Nemotron reasoning)",
    signup_url="https://www.blackbox.ai/",
    fallback_models=(
        "blackboxai/nvidia/nemotron-3-ultra",
        "blackboxai/nvidia/nemotron-3-super-120b-a12b",
    ),
    base_url="https://api.blackbox.ai/v1",
    default_max_tokens=16384,
)

register_provider(blackbox)
