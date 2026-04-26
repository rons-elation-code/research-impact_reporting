"""Unit tests for classifier v2 (Spec 0023, Phase 2).

ACs: AC12, AC13, AC14, AC15, AC16, AC17, AC18, AC19, AC29, AC30, AC34, AC37
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lavandula.reports.classify import (
    CLASSIFIER_TOOL_V2,
    ClassifierError,
    ClassificationResult,
    build_messages_v2,
    classify_first_page_v2,
    _validate_tool_input_v2,
)
from lavandula.reports.taxonomy import (
    build_taxonomy_prompt_section,
    load_taxonomy,
)

YAML_PATH = Path(__file__).parents[3] / "docs" / "collateral_taxonomy.yaml"


@pytest.fixture
def taxonomy():
    return load_taxonomy(YAML_PATH)


@pytest.fixture
def prompt_section(taxonomy):
    return build_taxonomy_prompt_section(taxonomy)


class _StubResponse:
    def __init__(self, tool_input: dict,
                 input_tokens: int = 1000, output_tokens: int = 100):
        self.content = [
            type(
                "Block", (),
                {"type": "tool_use", "name": "record_classification",
                 "input": tool_input},
            )
        ]
        self.usage = type(
            "Usage", (),
            {"input_tokens": input_tokens, "output_tokens": output_tokens},
        )


def _stub_client(tool_input):
    class _C:
        class messages:
            @staticmethod
            def create(**kwargs):
                return _StubResponse(tool_input)
    return _C()


# --- AC12: tool schema ---


def test_tool_v2_schema_structure():
    assert CLASSIFIER_TOOL_V2["name"] == "record_classification"
    props = CLASSIFIER_TOOL_V2["input_schema"]["properties"]
    assert "material_type" in props
    assert "event_type" in props
    assert "confidence" in props
    assert "reasoning" in props
    assert CLASSIFIER_TOOL_V2["input_schema"]["required"] == [
        "material_type", "confidence", "reasoning"
    ]


# --- AC13: prompt includes taxonomy ---


def test_prompt_includes_taxonomy(prompt_section):
    sys_prompt, user = build_messages_v2("test text", prompt_section)
    assert "annual_report" in sys_prompt
    assert "gala" in sys_prompt
    assert "MATERIAL TYPES:" in sys_prompt


# --- AC18: untrusted_document tags preserved ---


def test_untrusted_document_tags_preserved(prompt_section):
    sys_prompt, user = build_messages_v2("test text", prompt_section)
    assert "<untrusted_document>" in user
    assert "</untrusted_document>" in user
    assert "test text" in user
    assert "DATA ONLY" in sys_prompt


# --- AC14/AC15: validation ---


def test_valid_annual_report(taxonomy):
    data = {
        "material_type": "annual_report",
        "event_type": None,
        "confidence": 0.95,
        "reasoning": "Clearly an annual report",
    }
    mt, mg, et, conf, reason = _validate_tool_input_v2(data, taxonomy)
    assert mt == "annual_report"
    assert mg == "reports"
    assert et is None
    assert conf == 0.95
    assert reason == "Clearly an annual report"


def test_valid_event_collateral(taxonomy):
    data = {
        "material_type": "event_invitation",
        "event_type": "gala",
        "confidence": 0.9,
        "reasoning": "Gala invitation",
    }
    mt, mg, et, conf, reason = _validate_tool_input_v2(data, taxonomy)
    assert mt == "event_invitation"
    assert mg == "invitations"
    assert et == "gala"


# --- AC14: invalid material_type rejected ---


def test_invalid_material_type_rejected(taxonomy):
    data = {
        "material_type": "completely_fake_type",
        "confidence": 0.9,
        "reasoning": "test",
    }
    with pytest.raises(ClassifierError, match="not in taxonomy"):
        _validate_tool_input_v2(data, taxonomy)


# --- AC15/AC37: invalid event_type rejected ---


def test_invalid_event_type_rejected(taxonomy):
    data = {
        "material_type": "annual_report",
        "event_type": "nonexistent_event",
        "confidence": 0.9,
        "reasoning": "test",
    }
    with pytest.raises(ClassifierError, match="not in taxonomy"):
        _validate_tool_input_v2(data, taxonomy)


# --- AC15: null event_type accepted ---


def test_null_event_type_accepted(taxonomy):
    data = {
        "material_type": "annual_report",
        "event_type": None,
        "confidence": 0.9,
        "reasoning": "test",
    }
    _, _, et, _, _ = _validate_tool_input_v2(data, taxonomy)
    assert et is None


# --- AC34: event-shaped material type with null event_type ---


def test_event_shaped_null_event(taxonomy):
    data = {
        "material_type": "event_invitation",
        "event_type": None,
        "confidence": 0.85,
        "reasoning": "Generic invitation, no named event",
    }
    mt, mg, et, conf, reason = _validate_tool_input_v2(data, taxonomy)
    assert mt == "event_invitation"
    assert mg == "invitations"
    assert et is None


# --- AC16: material_group derived from taxonomy, not LLM ---


def test_material_group_not_from_llm(taxonomy):
    client = _stub_client({
        "material_type": "annual_report",
        "event_type": None,
        "confidence": 0.9,
        "reasoning": "test",
    })
    result = classify_first_page_v2(
        "test", client=client, taxonomy=taxonomy,
    )
    assert result.material_group == "reports"


# --- AC17: legacy classification derived ---


def test_legacy_mapping_annual(taxonomy):
    client = _stub_client({
        "material_type": "annual_report",
        "confidence": 0.95,
        "reasoning": "annual",
    })
    result = classify_first_page_v2(
        "test", client=client, taxonomy=taxonomy,
    )
    assert result.classification == "annual"
    assert result.material_type == "annual_report"
    assert result.material_group == "reports"


def test_legacy_mapping_not_relevant(taxonomy):
    client = _stub_client({
        "material_type": "not_relevant",
        "confidence": 0.99,
        "reasoning": "not nonprofit",
    })
    result = classify_first_page_v2(
        "test", client=client, taxonomy=taxonomy,
    )
    assert result.classification == "not_a_report"


def test_legacy_mapping_other_material_types(taxonomy):
    client = _stub_client({
        "material_type": "sponsor_prospectus",
        "confidence": 0.9,
        "reasoning": "sponsorship doc",
    })
    result = classify_first_page_v2(
        "test", client=client, taxonomy=taxonomy,
    )
    assert result.classification == "other"


# --- AC29: parametrized legacy mapping for all material types ---


def test_legacy_mapping_all_types(taxonomy):
    valid_legacy = {"annual", "impact", "hybrid", "other", "not_a_report"}
    for mt in taxonomy.raw.material_types:
        legacy = taxonomy.material_type_to_legacy(mt.id)
        assert legacy in valid_legacy, (
            f"{mt.id} maps to {legacy!r}"
        )


# --- AC30: confidence out of range ---


def test_confidence_out_of_range_high(taxonomy):
    data = {
        "material_type": "annual_report",
        "confidence": 1.5,
        "reasoning": "test",
    }
    with pytest.raises(ClassifierError, match="out of"):
        _validate_tool_input_v2(data, taxonomy)


def test_confidence_out_of_range_low(taxonomy):
    data = {
        "material_type": "annual_report",
        "confidence": -0.1,
        "reasoning": "test",
    }
    with pytest.raises(ClassifierError, match="out of"):
        _validate_tool_input_v2(data, taxonomy)


def test_confidence_not_numeric(taxonomy):
    data = {
        "material_type": "annual_report",
        "confidence": "high",
        "reasoning": "test",
    }
    with pytest.raises(ClassifierError, match="not numeric"):
        _validate_tool_input_v2(data, taxonomy)


# --- AC19: runtime guard — error result on invalid types ---


def test_runtime_guard_invalid_material_type(taxonomy):
    client = _stub_client({
        "material_type": "hallucinated_type",
        "confidence": 0.9,
        "reasoning": "test",
    })
    result = classify_first_page_v2(
        "test", client=client, taxonomy=taxonomy,
        raise_on_error=False,
    )
    assert result.classification is None
    assert result.material_type is None
    assert result.error


def test_runtime_guard_invalid_event_type(taxonomy):
    client = _stub_client({
        "material_type": "annual_report",
        "event_type": "fake_event",
        "confidence": 0.9,
        "reasoning": "test",
    })
    result = classify_first_page_v2(
        "test", client=client, taxonomy=taxonomy,
        raise_on_error=False,
    )
    assert result.classification is None
    assert result.error


# --- AC19: raise_on_error=True raises ---


def test_runtime_guard_raises_when_requested(taxonomy):
    client = _stub_client({
        "material_type": "hallucinated_type",
        "confidence": 0.9,
        "reasoning": "test",
    })
    with pytest.raises(ClassifierError):
        classify_first_page_v2(
            "test", client=client, taxonomy=taxonomy,
            raise_on_error=True,
        )


# --- No tool use block ---


def test_no_tool_use_block(taxonomy):
    class _NoToolClient:
        class messages:
            @staticmethod
            def create(**kwargs):
                resp = type("R", (), {
                    "content": [type("B", (), {"type": "text", "text": "oops"})],
                    "usage": type("U", (), {"input_tokens": 10, "output_tokens": 5}),
                })()
                return resp
    result = classify_first_page_v2(
        "test", client=_NoToolClient(), taxonomy=taxonomy,
        raise_on_error=False,
    )
    assert result.classification is None
    assert "no tool_use" in result.error
