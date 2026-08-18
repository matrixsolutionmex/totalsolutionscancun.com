import os
import unittest
from unittest.mock import patch

import httpx

from app.services.pablo_ai_providers import ProviderConfig, generate_from_provider, provider_config
from app.services.pablo_ai_service import generate_pablo_reply, pablo_ai_enabled


class PabloAiProvidersTest(unittest.TestCase):
    def test_no_api_key_leaves_ai_disabled_for_default_openai(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(pablo_ai_enabled())

    def test_primary_failure_uses_secondary_provider(self):
        primary = httpx.Response(503, request=httpx.Request("POST", "https://primary/v1/responses"))
        secondary = httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Resposta secundária"}}]},
            request=httpx.Request("POST", "https://secondary/v1/chat/completions"),
        )
        with patch.dict(os.environ, {
            "PABLO_AI_PROVIDERS": "openai,selfhosted",
            "PABLO_OPENAI_API_KEY": "test-primary",
            "PABLO_SELFHOSTED_BASE_URL": "https://secondary/v1",
            "PABLO_SELFHOSTED_MODEL": "local-model",
        }, clear=True), patch("httpx.Client.post", side_effect=[primary, secondary]):
            self.assertEqual(
                generate_pablo_reply(message="oi", actor={}, context={}),
                "Resposta secundária",
            )

    def test_all_providers_failed_returns_none(self):
        failure = httpx.Response(500, request=httpx.Request("POST", "https://provider"))
        with patch.dict(os.environ, {
            "PABLO_AI_PROVIDERS": "openai,selfhosted",
            "PABLO_OPENAI_API_KEY": "test-primary",
            "PABLO_SELFHOSTED_BASE_URL": "https://secondary/v1",
        }, clear=True), patch("httpx.Client.post", return_value=failure):
            self.assertIsNone(generate_pablo_reply(message="oi", actor={}, context={}))

    def test_openai_legacy_key_enables_provider(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "legacy-key"}, clear=True):
            self.assertTrue(pablo_ai_enabled())

    def test_openai_key_precedence_is_provider_then_generic_then_legacy(self):
        with patch.dict(os.environ, {
            "PABLO_OPENAI_API_KEY": "provider-key",
            "PABLO_AI_API_KEY": "generic-key",
            "OPENAI_API_KEY": "legacy-key",
        }, clear=True):
            self.assertEqual(provider_config("openai").api_key, "provider-key")

        with patch.dict(os.environ, {
            "PABLO_AI_API_KEY": "generic-key",
            "OPENAI_API_KEY": "legacy-key",
        }, clear=True):
            self.assertEqual(provider_config("openai").api_key, "generic-key")

        with patch.dict(os.environ, {"OPENAI_API_KEY": "legacy-key"}, clear=True):
            self.assertEqual(provider_config("openai").api_key, "legacy-key")

    def test_openai_default_model_and_base_url_are_resolved(self):
        with patch.dict(os.environ, {"PABLO_AI_API_KEY": "generic-key"}, clear=True):
            config = provider_config("openai")
            self.assertEqual(config.model, "gpt-5.6-luna")
            self.assertEqual(config.base_url, "https://api.openai.com/v1")

    def test_new_generic_configuration_is_supported_without_exposing_key(self):
        response = httpx.Response(
            200,
            json={"output_text": "Resposta OpenAI"},
            request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
        )
        with patch.dict(os.environ, {
            "PABLO_AI_PROVIDER": "openai",
            "PABLO_AI_API_KEY": "generic-key",
            "PABLO_AI_MODEL": "gpt-5.6-luna",
        }, clear=True), patch("httpx.Client.post", return_value=response) as post:
            self.assertEqual(generate_pablo_reply(message="oi", actor={}, context={}), "Resposta OpenAI")
            self.assertEqual(post.call_args.args[0], "https://api.openai.com/v1/responses")

    def test_openai_compatible_response_is_parsed(self):
        config = ProviderConfig("openai_compatible", "local-model", "https://local/v1", "")
        response = httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Local reply"}}]},
            request=httpx.Request("POST", "https://local/v1/chat/completions"),
        )
        with patch("httpx.Client.post", return_value=response):
            self.assertEqual(
                generate_from_provider(config, message="oi", instructions="context", timeout_seconds=1),
                "Local reply",
            )


if __name__ == "__main__":
    unittest.main()
