import json
import unittest

import _bootstrap  # noqa: F401
from mockingbird.api import responses
from mockingbird.api.providers import bedrock, google
from mockingbird.core import agent


class UsageTests(unittest.TestCase):
    def test_claude_usage_and_provider_responses(self):
        event = {"type": "result", "result": '{"text":"hello"}',
                 "usage": {"input_tokens": 10, "cache_read_input_tokens": 20,
                           "cache_creation_input_tokens": 5, "output_tokens": 3},
                 "total_cost_usd": 0.01}
        text, usage = agent.claude_output(json.dumps(event))
        ans = {**agent.parse_answer(text), "usage": usage}
        req = {"model": "test", "messages": []}
        ant = responses.anthropic_body(ans, req, "1")
        self.assertEqual(ant["usage"]["input_tokens"], 10)
        self.assertEqual(ant["usage"]["cache_read_input_tokens"], 20)
        self.assertEqual(ant["usage"]["output_tokens"], 3)
        self.assertIn('"output_tokens": 3', responses.anthropic_sse(ant))
        oai = responses.openai_body(ans, req, "1")
        self.assertEqual(oai["usage"]["prompt_tokens"], 35)
        self.assertEqual(oai["usage"]["total_tokens"], 38)
        stream = responses.openai_sse(oai, {"stream_options": {"include_usage": True}})
        self.assertIn('"total_tokens": 38', stream)
        self.assertEqual(bedrock.response(ans, req, "1")["usage"]["totalTokens"], 38)
        self.assertEqual(google.response(ans, req, "1")["usageMetadata"]["totalTokenCount"], 38)

    def test_custom_and_error_outputs(self):
        with self.assertRaises(agent.CliError):
            agent.claude_output(json.dumps({"type": "result", "is_error": True, "result": "failed"}))
        with self.assertRaises(ValueError):
            agent.parse_answer('{"text":"hello","usage":{"prompt_tokens":-1}}')


if __name__ == "__main__":
    unittest.main()
