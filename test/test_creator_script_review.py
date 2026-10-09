"""Risk review must quote the draft and preserve facts without real model calls."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.services.creator import script_review as review, topics


class ScriptReviewTests(unittest.TestCase):
    def setUp(self):
        self.source = "我们在上海开店，售价99元。这款工具绝对适合每个人。"
        self.optimized = self.source.replace("绝对", "可能")

    def response(self, **updates):
        payload = {
            "optimized_text": self.optimized,
            "risks": [{"quote": "绝对", "category": "保证性措辞", "reason": "可能被理解为无条件保证，需核查适用范围。",
                       "suggestion": "弱化保证，保留价格、地址及已提供事实。"}],
            "summary": "标注一处潜在保证性表达，事实依据仍需核查。",
        }
        payload.update(updates)
        return json.dumps(payload, ensure_ascii=False)

    def test_configured_model_returns_anchored_risks_and_minimal_safe_optimization(self):
        progress = []
        config = {"llm_provider": "test"}
        with patch.object(topics, "_generate", return_value=self.response()) as model:
            result = review.review_script(self.source, app_config=config, progress=lambda *args: progress.append(args))
        self.assertEqual(result["engine"], "configured_llm")
        self.assertEqual(result["source_text"], self.source)
        self.assertEqual(result["optimized_text"], self.optimized)
        row = result["risks"][0]
        self.assertEqual(self.source[row["start"]:row["end"]], "绝对")
        self.assertEqual(model.call_args.kwargs["app_config"], config)
        self.assertIn("保持客户提供的事实", model.call_args.args[0])
        self.assertEqual(progress[-1][1], 100)

    def test_edit_outside_quoted_span_is_rejected_without_losing_review(self):
        with patch.object(topics, "_generate", return_value=self.response(optimized_text=self.optimized.replace("上海", "北京"))):
            result = review.review_script(self.source)
        self.assertEqual(result["optimized_text"], self.source)
        self.assertEqual(result["engine"], "configured_llm")
        self.assertIn("未标注", result["summary"])

    def test_new_certification_claim_cannot_be_inserted_by_the_model(self):
        changed = self.source.replace("绝对", "国家认证")
        with patch.object(topics, "_generate", return_value=self.response(optimized_text=changed)):
            result = review.review_script(self.source)
        self.assertEqual(result["optimized_text"], self.source)
        self.assertIn("新增了认证", result["summary"])

    def test_new_customer_name_inside_risk_span_is_not_a_safe_edit(self):
        with patch.object(topics, "_generate", return_value=self.response(optimized_text=self.source.replace("绝对", "清华专用"))):
            result = review.review_script(self.source)
        self.assertEqual(result["optimized_text"], self.source)
        self.assertIn("新内容", result["summary"])

    def test_existing_price_and_count_are_not_deleted_or_changed(self):
        for replacement in ("999元", "优惠价"):
            with self.subTest(replacement=replacement), patch.object(topics, "_generate", return_value=self.response(
                    optimized_text=self.optimized.replace("99元", replacement))):
                result = review.review_script(self.source)
            self.assertEqual(result["optimized_text"], self.source)
            self.assertIn("数字", result["summary"])

    def test_missing_or_fabricated_quote_uses_explicit_local_fallback(self):
        for response in ("not json", self.response(risks=[{"quote": "治愈", "category": "效果", "reason": "需核查", "suggestion": "核对依据"}])):
            with self.subTest(response=response), patch.object(topics, "_generate", return_value=response):
                result = review.review_script(self.source)
            self.assertEqual(result["engine"], "local_rules")
            self.assertEqual(result["optimized_text"], self.source)
            self.assertIn("本地措辞初筛", result["summary"])
            self.assertIn("未自动改写", result["summary"])

    def test_model_cannot_claim_legal_approval(self):
        with patch.object(topics, "_generate", return_value=self.response(summary="已经通过法务审核，完全合法合规。")):
            result = review.review_script(self.source)
        self.assertEqual(result["engine"], "local_rules")
        self.assertNotIn("已经通过", result["summary"])

    def test_model_failure_flags_terms_and_unverified_facts_without_fabricating_an_optimization(self):
        source = "这是全网最低价，销量100万，已获得国家认证，保证有效。"
        with patch.object(topics, "_generate", side_effect=topics.TopicGenerationError("test offline")):
            result = review.review_script(source)
        self.assertEqual(result["source_text"], source)
        self.assertEqual(result["optimized_text"], source)
        self.assertEqual(result["engine"], "local_rules")
        self.assertGreaterEqual(len(result["risks"]), 4)
        self.assertTrue(any(row["category"] == "事实依据待核查" for row in result["risks"]))
        for row in result["risks"]:
            self.assertEqual(source[row["start"]:row["end"]], row["quote"])
        self.assertIn("不是事实核验", result["summary"])

    def test_negated_example_is_not_reported_as_an_absolute_claim(self):
        with patch.object(topics, "_generate", side_effect=topics.TopicGenerationError("offline")):
            result = review.review_script("不要使用绝对有效，避免全网最低价，介绍具体的产品特点。")
        self.assertFalse(result["risks"])
        self.assertIn("未命中", result["summary"])

    def test_empty_and_non_text_input_never_calls_a_model(self):
        with patch.object(topics, "_generate") as model:
            for text in ("", " ", None, 123, "x" * 12001):
                with self.subTest(text=str(text)[:20]), self.assertRaises(ValueError):
                    review.review_script(text)
        model.assert_not_called()

    def test_keywords_are_stable_grouped_and_present_in_original(self):
        source = "数字人口播清晰自然，数字人口播真实完整，让大家放心。关注并收藏，分享给喜欢数字人的朋友。"
        result = review.extract_keywords(source)
        self.assertEqual(set(result), {"main", "description", "action", "emotion"})
        self.assertEqual(result, review.extract_keywords(source))
        self.assertIn("清晰", result["description"])
        self.assertIn("关注", result["action"])
        self.assertIn("放心", result["emotion"])
        for values in result.values():
            self.assertLessEqual(len(values), 12)
            self.assertEqual(len(values), len(set(values)))
            self.assertTrue(all(word in source for word in values))


if __name__ == "__main__":
    unittest.main()
