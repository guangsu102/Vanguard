import pytest

from app.core.ai.keyword_generator import (
    KeywordGenerator,
    normalize_keyword_text,
    validate_search_keyword_text,
)
from app.core.keyword.models import KeywordType


class UnconfiguredLLM:
    async def generate(self, *args, **kwargs) -> str:
        return "API not configured. Please set API key."


@pytest.mark.asyncio
async def test_fallback_generation_returns_requested_unique_keywords():
    generator = KeywordGenerator()

    keywords = await generator.generate(category="demand", count=20)

    assert len(keywords) == 20
    assert len({item.text.lower() for item in keywords}) == 20
    assert all(item.type == KeywordType.DEMAND for item in keywords)
    assert all(validate_search_keyword_text(item.text)[0] for item in keywords)
    assert any(item.text.endswith(("群", "圈")) for item in keywords)


@pytest.mark.asyncio
async def test_unconfigured_llm_response_uses_fallback_keywords():
    generator = KeywordGenerator(UnconfiguredLLM())

    keywords = await generator.generate(category="price", count=10)

    assert len(keywords) == 10
    assert all("api not configured" not in item.text.lower() for item in keywords)
    assert all(item.type == KeywordType.PRICE for item in keywords)


@pytest.mark.asyncio
async def test_fallback_generation_excludes_existing_keywords():
    generator = KeywordGenerator()

    keywords = await generator.generate(
        category="demand",
        count=5,
        avoid_keywords=[" 跨境 ", "外贸", "出海"],
    )

    generated = {normalize_keyword_text(item.text) for item in keywords}
    assert len(keywords) == 5
    assert normalize_keyword_text("跨境") not in generated
    assert normalize_keyword_text("外贸") not in generated
    assert normalize_keyword_text("出海") not in generated


@pytest.mark.asyncio
async def test_fallback_generation_covers_industries_and_general_groups():
    generator = KeywordGenerator()

    demand = {item.text for item in await generator.generate(category="demand", count=100)}
    general = {item.text for item in await generator.generate(category="competitor", count=80)}

    assert {"餐饮群", "房产群", "医疗群", "新能源"}.issubset(demand)
    assert {"交流群", "同城群", "资源群", "兴趣群"}.issubset(general)


@pytest.mark.asyncio
async def test_fallback_generation_avoids_unnatural_suffix_combinations():
    generator = KeywordGenerator()
    bad_suffixes = ("号", "服", "区", "法", "课", "营", "局", "会", "社", "帮")

    generated = []
    for category in ("demand", "inquiry", "price", "competitor"):
        generated.extend(await generator.generate(category=category, count=120))

    assert [item.text for item in generated if item.text.endswith(bad_suffixes)] == []


def test_search_keyword_quality_rules():
    assert validate_search_keyword_text("跨境")[0]
    assert validate_search_keyword_text("跨境群")[0]
    assert validate_search_keyword_text("亚马逊")[0]
    assert validate_search_keyword_text("跨境电商")[0]
    assert not validate_search_keyword_text("跨境电商卖家")[0]
    assert validate_search_keyword_text("TikTok运营")[0]
    assert validate_search_keyword_text("TikTok群")[0]
    assert validate_search_keyword_text("ChatGPT")[0]
    assert validate_search_keyword_text("Freelance")[0]
    assert validate_search_keyword_text("Etsy卖家")[0]
    assert validate_search_keyword_text("外贸群")[0]
    assert validate_search_keyword_text("交流群")[0]
    assert validate_search_keyword_text("同城群")[0]
    assert validate_search_keyword_text("餐饮群")[0]
    assert validate_search_keyword_text("医疗群")[0]
    assert not validate_search_keyword_text("学习会")[0]
    assert not validate_search_keyword_text("兴趣社")[0]
    assert not validate_search_keyword_text("资源帮")[0]
    assert not validate_search_keyword_text("支付群")[0]
    assert not validate_search_keyword_text("日区")[0]
    assert not validate_search_keyword_text("美区")[0]
    assert not validate_search_keyword_text("美国")[0]
    assert not validate_search_keyword_text("日本")[0]
    assert not validate_search_keyword_text("支付")[0]
    assert not validate_search_keyword_text("变现")[0]
    assert not validate_search_keyword_text("VeryLongEnglishKeyword")[0]
    assert not validate_search_keyword_text("VPN")[0]
    assert not validate_search_keyword_text("机场")[0]
    assert not validate_search_keyword_text("怎么用AI")[0]
