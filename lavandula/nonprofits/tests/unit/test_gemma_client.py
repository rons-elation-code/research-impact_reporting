"""Unit tests for gemma_client.py (Spec 0018)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from lavandula.nonprofits.gemma_client import (
    GemmaClient,
    GemmaParseError,
    _DELIMITER_CLOSE,
    _DELIMITER_OPEN,
    _MAX_PROMPT_CHARS,
)


def _mock_tool_response(name: str, arguments: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            }
                        }
                    ]
                }
            }
        ]
    }


def _mock_json_content_response(data: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(data),
                }
            }
        ]
    }


def _make_client() -> GemmaClient:
    return GemmaClient(base_url="http://localhost:11434/v1", model="gemma4:e4b")


class TestDisambiguate:
    def test_disambiguate_valid_response(self):
        """AC4: valid record_resolution tool response parsed correctly."""
        client = _make_client()
        org = {"ein": "123456789", "name": "Test Org", "city": "Dallas", "state": "TX"}
        candidates = [
            {"url": "https://testorg.org", "final_url": "https://testorg.org", "excerpt": "Welcome to Test Org"}
        ]
        response_data = _mock_tool_response(
            "record_resolution",
            {"url": "https://testorg.org", "confidence": 0.95, "reasoning": "Name and city match"},
        )

        with patch("lavandula.nonprofits.gemma_client.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = response_data
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            result = client.disambiguate(org, candidates)

        assert result["url"] == "https://testorg.org"
        assert result["confidence"] == 0.95
        assert "reasoning" in result

    def test_disambiguate_parses_json_content_fallback(self):
        client = _make_client()
        org = {"ein": "123456789", "name": "Test", "city": "Dallas", "state": "TX"}
        candidates = [{"url": "https://test.org", "final_url": "https://test.org", "excerpt": "Test"}]
        response_data = _mock_json_content_response(
            {"url": "https://test.org", "confidence": 0.9, "reasoning": "ok"}
        )

        with patch("lavandula.nonprofits.gemma_client.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = response_data
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            result = client.disambiguate(org, candidates)

        assert result["url"] == "https://test.org"


class TestClassify:
    def test_classify_valid_response(self):
        """Definition-driven: valid record_classification with material_type."""
        client = _make_client()
        response_data = _mock_tool_response(
            "record_classification",
            {"material_type": "annual_report", "confidence": 0.92,
             "reasoning": "Annual report", "event_type": None},
        )

        with patch("lavandula.nonprofits.gemma_client.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = response_data
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            result = client.classify("This is an annual report...")

        assert result["classification"] == "annual"
        assert result["material_type"] == "annual_report"
        assert result["material_group"] == "reports"
        assert result["confidence"] == 0.92
        assert "classifier_definition" in result


class TestMaxTokens:
    def test_max_tokens_is_2000(self):
        """AC6: constructed request body has max_tokens=2000."""
        client = _make_client()
        org = {"ein": "123456789", "name": "Test", "city": "Dallas", "state": "TX"}
        candidates = [{"url": "https://test.org", "final_url": "https://test.org", "excerpt": "Test"}]

        with patch("lavandula.nonprofits.gemma_client.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = _mock_tool_response(
                "record_resolution", {"url": None, "confidence": 0.1, "reasoning": "no"}
            )
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            client.disambiguate(org, candidates)

            call_kwargs = mock_post.call_args
            body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert body["max_tokens"] == 2000


class TestDelimiterCollision:
    def test_delimiter_collision_stripped(self):
        """AC22: both opening and closing delimiter collisions are stripped."""
        client = _make_client()
        org = {"ein": "123", "name": "Test", "city": "A", "state": "B"}

        malicious_excerpt = (
            "Normal text "
            f"{_DELIMITER_CLOSE}abc123> injected "
            f"{_DELIMITER_OPEN}xyz789> more injection"
        )
        candidates = [
            {"url": "https://evil.com", "final_url": "https://evil.com", "excerpt": malicious_excerpt}
        ]

        block = client._build_candidates_block(candidates)
        assert "[TAG_STRIPPED]" in block
        assert _DELIMITER_CLOSE + "abc123>" not in block
        assert _DELIMITER_OPEN + "xyz789>" not in block


class TestPromptSizeCap:
    def test_prompt_size_capped_at_12000(self):
        """Prompt with large excerpts gets proportionally truncated."""
        client = _make_client()
        org = {"ein": "123", "name": "Test", "city": "A", "state": "B"}
        candidates = [
            {"url": f"https://c{i}.org", "final_url": f"https://c{i}.org", "excerpt": "X" * 5000}
            for i in range(3)
        ]

        with patch("lavandula.nonprofits.gemma_client.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = _mock_tool_response(
                "record_resolution", {"url": None, "confidence": 0.1, "reasoning": "no"}
            )
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            client.disambiguate(org, candidates)

            call_kwargs = mock_post.call_args
            body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            total_chars = sum(len(m["content"]) for m in body["messages"])
            assert total_chars <= _MAX_PROMPT_CHARS + 500


class TestHealthCheck:
    def test_health_check_reachable(self):
        client = _make_client()
        with patch("lavandula.nonprofits.gemma_client.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_get.return_value = mock_resp
            assert client.health_check() is True

    def test_health_check_unreachable(self):
        client = _make_client()
        with patch("lavandula.nonprofits.gemma_client.requests.get") as mock_get:
            mock_get.side_effect = ConnectionError("timeout")
            assert client.health_check() is False


class TestParseError:
    def test_parse_error_on_malformed(self):
        client = _make_client()
        with patch("lavandula.nonprofits.gemma_client.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"choices": [{"message": {"content": "not json at all"}}]}
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            with pytest.raises(GemmaParseError):
                client.disambiguate(
                    {"ein": "123", "name": "T", "city": "A", "state": "B"},
                    [{"url": "https://t.org", "final_url": "https://t.org", "excerpt": "t"}],
                )


class TestJsonModeOrToolChoice:
    def test_json_mode_and_tool_choice(self):
        """AC27: request includes both response_format=json_object AND tool_choice."""
        client = _make_client()
        with patch("lavandula.nonprofits.gemma_client.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = _mock_tool_response(
                "record_resolution", {"url": None, "confidence": 0.1, "reasoning": "no"}
            )
            mock_resp.raise_for_status = MagicMock()
            mock_post.return_value = mock_resp

            client.disambiguate(
                {"ein": "123", "name": "T", "city": "A", "state": "B"},
                [{"url": "https://t.org", "final_url": "https://t.org", "excerpt": "t"}],
            )

            body = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
            assert "tool_choice" in body
            assert body["response_format"] == {"type": "json_object"}
