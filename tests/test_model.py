"""Tests for the profile-driven model client (BUG_HUNTER_SPEC.md §3).

No live endpoint — the transport hooks (_http_post/_http_get) are patched.
"""
import unittest
from unittest import mock

from harness import model
from harness.model import (
    ModelProfile, ModelError, extract_json, resolve_model, chat, chat_batch,
    _build_payload, clear_model_cache,
)


def _resp(content, usage=None, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 50}}


class ProfileTests(unittest.TestCase):
    def test_from_config_ignores_unknown_keys(self):
        p = ModelProfile.from_config(
            {"endpoint": "http://x/v1", "model": "m", "bogus": 1,
             "reasoning_model": False})
        self.assertEqual(p.endpoint, "http://x/v1")
        self.assertEqual(p.model, "m")
        self.assertFalse(p.reasoning_model)


class ExtractJsonTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_strips_reasoning_trace(self):
        self.assertEqual(extract_json('<think>musing...</think>\n{"a": 1}'), {"a": 1})

    def test_strips_code_fence(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_surrounding_prose_and_nested_braces(self):
        self.assertEqual(
            extract_json('Here you go: {"a": {"b": 2}} — done'),
            {"a": {"b": 2}})

    def test_braces_inside_strings_dont_miscount(self):
        self.assertEqual(extract_json('{"code": "if x { y }"}'), {"code": "if x { y }"})

    def test_empty_and_nojson_raise(self):
        with self.assertRaises(ModelError):
            extract_json("")
        with self.assertRaises(ModelError):
            extract_json("no json here")


class ResolveModelTests(unittest.TestCase):
    def setUp(self):
        clear_model_cache()

    def test_explicit_model_returned_as_is(self):
        p = ModelProfile(endpoint="http://x/v1", model="deepseek-v4-flash")
        with mock.patch.object(model, "_http_get") as g:
            self.assertEqual(resolve_model(p), "deepseek-v4-flash")
            g.assert_not_called()                     # no /models probe

    def test_auto_discovers_first_and_caches(self):
        p = ModelProfile(endpoint="http://x/v1", model="auto")
        with mock.patch.object(model, "_http_get",
                               return_value={"data": [{"id": "srv-model"}]}) as g:
            self.assertEqual(resolve_model(p), "srv-model")
            self.assertEqual(resolve_model(p), "srv-model")   # cached
            g.assert_called_once()

    def test_auto_empty_raises(self):
        p = ModelProfile(endpoint="http://y/v1", model="auto")
        with mock.patch.object(model, "_http_get", return_value={"data": []}):
            with self.assertRaises(ModelError):
                resolve_model(p)


class PayloadTests(unittest.TestCase):
    def test_json_object_format_and_server_default_omits_sampling(self):
        p = ModelProfile(endpoint="e", sampling="server_default")
        pl = _build_payload(p, "m", "sys", "usr", None)
        self.assertEqual(pl["response_format"], {"type": "json_object"})
        for k in ("temperature", "top_p", "top_k", "min_p"):
            self.assertNotIn(k, pl)

    def test_explicit_sampling_included_incompatible_dropped(self):
        p = ModelProfile(endpoint="e",
                         sampling={"temperature": 1.0, "top_p": 0.95, "min_p": 0.01},
                         sampling_incompatible=["min_p"])
        pl = _build_payload(p, "m", "sys", "usr", None)
        self.assertEqual(pl["temperature"], 1.0)
        self.assertEqual(pl["top_p"], 0.95)
        self.assertNotIn("min_p", pl)                 # dropped for the spec-decode lane


class ChatTests(unittest.TestCase):
    def setUp(self):
        clear_model_cache()
        self.p = ModelProfile(endpoint="http://x/v1", model="m")

    def test_success_returns_parsed_and_stats(self):
        with mock.patch.object(model, "_http_post",
                               return_value=_resp('{"findings": []}')):
            data, stats = chat(self.p, "sys", "usr")
        self.assertEqual(data, {"findings": []})
        self.assertEqual(stats.model, "m")
        self.assertEqual(stats.completion_tokens, 50)
        self.assertEqual(stats.attempt, 1)

    def test_retries_once_then_succeeds(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("blip")
            return _resp('{"ok": true}')

        with mock.patch.object(model, "_http_post", side_effect=flaky):
            data, stats = chat(self.p, "sys", "usr")
        self.assertEqual(data, {"ok": True})
        self.assertEqual(stats.attempt, 2)

    def test_raises_after_attempts_exhausted(self):
        with mock.patch.object(model, "_http_post", side_effect=TimeoutError("down")):
            with self.assertRaises(ModelError):
                chat(self.p, "sys", "usr", attempts=2)

    def test_unparseable_content_is_retried_then_raises(self):
        with mock.patch.object(model, "_http_post", return_value=_resp("not json")):
            with self.assertRaises(ModelError):
                chat(self.p, "sys", "usr", attempts=2)


class ChatBatchTests(unittest.TestCase):
    def setUp(self):
        clear_model_cache()
        self.p = ModelProfile(endpoint="http://x/v1", model="m")

    def test_isolates_a_failing_batch(self):
        def per_item(url, payload, timeout, headers):
            # fail the batch whose user prompt says "boom"
            u = payload["messages"][1]["content"]
            if "boom" in u:
                raise TimeoutError("down")
            return _resp('{"findings": [1]}')

        items = [("good", "s", "fine"), ("bad", "s", "boom"), ("good2", "s", "fine")]
        with mock.patch.object(model, "_http_post", side_effect=per_item):
            results = chat_batch(self.p, items)
        self.assertEqual([r.ok for r in results], [True, False, True])
        self.assertEqual(results[1].label, "bad")
        self.assertIn("down", results[1].error)
        self.assertEqual(results[0].data, {"findings": [1]})


if __name__ == "__main__":
    unittest.main()
