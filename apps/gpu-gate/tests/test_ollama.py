import logging
import unittest

from gpu_gate.ollama import OllamaClient

from .fakes import StubOllama


class OllamaClientTest(unittest.TestCase):
    def setUp(self):
        self.stub = StubOllama(["qwen3:14b", "nomic-embed-text:latest"])
        self.client = OllamaClient(self.stub.url + "/", timeout=2)
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)
        self.stub.close()

    def test_unload_all_posts_keep_alive_zero_per_model(self):
        self.assertEqual(self.client.unload_all(), ["qwen3:14b", "nomic-embed-text:latest"])
        self.assertEqual(self.stub.requests, [
            ("GET", "/api/ps", None),
            ("POST", "/api/generate", {"model": "qwen3:14b", "keep_alive": 0}),
            ("POST", "/api/generate", {"model": "nomic-embed-text:latest", "keep_alive": 0}),
        ])

    def test_nothing_loaded(self):
        self.stub.models = []
        self.assertEqual(self.client.unload_all(), [])
        self.assertEqual(self.stub.requests, [("GET", "/api/ps", None)])

    def test_ps_error_is_not_fatal(self):
        self.stub.fail["/api/ps"] = 500
        self.assertEqual(self.client.unload_all(), [])

    def test_one_failed_unload_does_not_stop_the_rest(self):
        self.stub.fail["qwen3:14b"] = 500
        self.assertEqual(self.client.unload_all(), ["nomic-embed-text:latest"])
        self.assertEqual(len(self.stub.requests), 3)

    def test_ollama_down_is_not_fatal(self):
        self.stub.close()
        self.assertEqual(self.client.unload_all(), [])


if __name__ == "__main__":
    unittest.main()
