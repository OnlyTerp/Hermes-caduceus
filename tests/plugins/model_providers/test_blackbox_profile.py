"""Unit tests for the Blackbox AI provider profile.

Blackbox hosts reasoning models (e.g. NVIDIA Nemotron). Two failure modes the
profile guards against:

1. **Empty assistant messages.** With no output cap, heavy reasoning consumes
   the whole completion budget and the response returns ``finish_reason=length``
   with empty ``content``. ``default_max_tokens=16384`` keeps headroom for the
   visible answer.
2. **Dropped reasoning effort.** Blackbox's gateway accepts ``extra_body.reasoning``
   but rejects the ``xhigh`` tier — the profile forwards reasoning and clamps
   ``xhigh``/``max`` → ``high``.

These tests pin the profile's wire-shape contract without going live.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def blackbox_profile():
    """Resolve the registered Blackbox profile via the global registry.

    Going through ``providers.get_provider_profile`` keeps the test honest — if
    someone replaces the registered class with a plain ``ProviderProfile`` the
    reasoning assertions below collapse.
    """
    import model_tools  # noqa: F401  — import triggers plugin discovery
    import providers

    profile = providers.get_provider_profile("blackbox")
    assert profile is not None, "blackbox provider profile must be registered"
    return profile


class TestBlackboxIdentity:
    def test_base_url_and_max_tokens(self, blackbox_profile):
        # The 16384 cap is the actual empty-message fix — reasoning can't starve
        # the visible answer.
        assert blackbox_profile.base_url == "https://api.blackbox.ai/v1"
        assert blackbox_profile.default_max_tokens == 16384

    def test_env_var_and_aliases(self, blackbox_profile):
        assert "BLACKBOX_API_KEY" in blackbox_profile.env_vars
        assert "blackboxai" in blackbox_profile.aliases

    def test_resolves_by_alias(self):
        import model_tools  # noqa: F401
        import providers

        assert providers.get_provider_profile("blackboxai") is not None


class TestBlackboxReasoningWireShape:
    """``build_api_kwargs_extras`` forwards reasoning as extra_body.reasoning."""

    def test_xhigh_is_clamped_to_high(self, blackbox_profile):
        extra_body, top_level = blackbox_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            supports_reasoning=True,
            model="blackboxai/nvidia/nemotron-3-ultra",
        )
        assert extra_body == {"reasoning": {"enabled": True, "effort": "high"}}
        assert top_level == {}

    def test_max_is_clamped_to_high(self, blackbox_profile):
        extra_body, _ = blackbox_profile.build_api_kwargs_extras(
            reasoning_config={"effort": "max"}, supports_reasoning=True,
        )
        assert extra_body == {"reasoning": {"effort": "high"}}

    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_standard_efforts_pass_through(self, blackbox_profile, effort):
        extra_body, _ = blackbox_profile.build_api_kwargs_extras(
            reasoning_config={"effort": effort}, supports_reasoning=True,
        )
        assert extra_body == {"reasoning": {"effort": effort}}

    def test_no_config_defaults_to_medium(self, blackbox_profile):
        extra_body, _ = blackbox_profile.build_api_kwargs_extras(
            reasoning_config=None, supports_reasoning=True,
        )
        assert extra_body == {"reasoning": {"enabled": True, "effort": "medium"}}

    def test_disabled_reasoning_is_omitted(self, blackbox_profile):
        extra_body, top_level = blackbox_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, supports_reasoning=True,
        )
        assert extra_body == {}
        assert top_level == {}

    def test_unsupported_route_sends_no_reasoning(self, blackbox_profile):
        # When the transport says the route is not reasoning-capable, we must
        # not attach reasoning (avoids 400s on non-reasoning models).
        extra_body, _ = blackbox_profile.build_api_kwargs_extras(
            reasoning_config={"effort": "high"}, supports_reasoning=False,
        )
        assert extra_body == {}


class TestBlackboxResolution:
    """The 401-regression guard: a recognized provider resolves its key from
    BLACKBOX_API_KEY (the documented convention), with the profile's base_url."""

    def test_resolves_key_from_env(self, monkeypatch):
        import model_tools  # noqa: F401
        from hermes_cli import runtime_provider as rp

        monkeypatch.setenv("BLACKBOX_API_KEY", "sk-blackbox-test")
        resolved = rp.resolve_runtime_provider(requested="blackbox")
        assert resolved["provider"] == "blackbox"
        assert resolved["base_url"] == "https://api.blackbox.ai/v1"
        assert resolved["api_key"] == "sk-blackbox-test"


class TestBlackboxReasoningGate:
    """``api.blackbox.ai`` must be recognized as reasoning-capable, else the
    forwarded reasoning config is silently dropped."""

    def test_host_is_reasoning_capable(self):
        from agent.model_metadata import base_url_host_matches

        assert base_url_host_matches("https://api.blackbox.ai/v1", "api.blackbox.ai")
