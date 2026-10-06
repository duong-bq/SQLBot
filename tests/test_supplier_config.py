"""
Unit tests that the backend model factory can serve the MiniMax provider via the OpenAI protocol.
"""

import os
import unittest

# Project root relative to this test file
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestModelFactoryConfig(unittest.TestCase):
    """Test that backend model_factory.py can support MiniMax via OpenAI protocol."""

    def setUp(self):
        factory_path = os.path.join(
            PROJECT_ROOT, "apps", "ai_model", "model_factory.py"
        )
        with open(factory_path, "r", encoding="utf-8") as f:
            self.factory_content = f.read()

    def test_openai_type_in_factory(self):
        """OpenAI type should be registered in LLMFactory._llm_types."""
        self.assertIn('"openai"', self.factory_content)

    def test_factory_supports_register(self):
        """LLMFactory should have register_llm method for extensibility."""
        self.assertIn("register_llm", self.factory_content)

    def test_factory_creates_openai_llm_for_openai_type(self):
        """The 'openai' type should map to OpenAILLM class."""
        self.assertIn('"openai": OpenAILLM', self.factory_content)

    def test_llm_config_has_api_base_url(self):
        """LLMConfig should support api_base_url for custom endpoints."""
        self.assertIn("api_base_url", self.factory_content)

    def test_openai_llm_passes_base_url(self):
        """OpenAILLM should pass base_url to BaseChatOpenAI."""
        self.assertIn("base_url=self.config.api_base_url", self.factory_content)


if __name__ == "__main__":
    unittest.main()
