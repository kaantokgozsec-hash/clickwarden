"""Token prices used to turn API usage into dollars.

Heavy-model prices are Anthropic first-party list prices in USD per million tokens, as listed on
PRICES_CHECKED. Cache writes use the 5-minute TTL rate (1.25x input). Re-check https://claude.com/pricing
before publishing; BENCHMARK.md prints the table that was actually used.

The vision model's price for the headline comes from the command line (``--vision-price-in/out``). Its token
counts are always reported, whatever its price. VISION_PRICE_SCENARIOS re-prices the same measured vision
tokens at other published rates, so the report shows what the run would cost on a paid endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PRICES_CHECKED = "2026-11-05"

HEAVY_PRICES = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01},
}

# USD per million prompt / completion tokens, from OpenRouter's public models API
# (https://openrouter.ai/api/v1/models and .../models/nvidia/nemotron-3.5-super-vl/endpoints), fetched
# VISION_PRICES_CHECKED. Only the Nemotron rows are like-for-like with the measured tokens: other models
# tokenize text and images differently, so their rows are an approximation (same token count assumed).
VISION_PRICES_CHECKED = "2026-11-05 05:12 +0400"
VISION_PRICE_SCENARIOS = [
    {"label": "Free tier (what this run used)", "model": "nvidia/nemotron-3.5-super-vl:free",
     "in": 0.00, "out": 0.00, "same_model": True},
    {"label": "Nemotron 3.5 Super VL, cheapest host (Darkbloom)", "model": "nvidia/nemotron-3.5-super-vl",
     "in": 0.06, "out": 0.35, "same_model": True},
    {"label": "Nemotron 3.5 Super VL, OpenRouter list price", "model": "nvidia/nemotron-3.5-super-vl",
     "in": 0.15, "out": 0.50, "same_model": True},
    {"label": "Nemotron 3.5 Super VL, priciest host (Nebius)", "model": "nvidia/nemotron-3.5-super-vl",
     "in": 0.30, "out": 0.90, "same_model": True},
    {"label": "Claude Haiku 5.5 price", "model": "anthropic/claude-haiku-5.5", "in": 0.10, "out": 0.50,
     "same_model": False},
    {"label": "Qwen3-VL-30B-A3B price", "model": "qwen/qwen3-vl-30b-a3b-instruct", "in": 0.15, "out": 0.60,
     "same_model": False},
    {"label": "GPT-5.4 nano price", "model": "openai/gpt-5.4-nano", "in": 0.20, "out": 1.25, "same_model": False},
    {"label": "Gemini 3.1 Flash-Lite price", "model": "google/gemini-3.1-flash-lite", "in": 0.25, "out": 1.50,
     "same_model": False},
]


@dataclass
class HeavyUsage:
    """Sum of the Messages API ``usage`` fields over every request in one run."""
    input: int = 0          # usage.input_tokens (uncached input)
    cache_write: int = 0    # usage.cache_creation_input_tokens
    cache_read: int = 0     # usage.cache_read_input_tokens
    output: int = 0         # usage.output_tokens (includes thinking)
    requests: int = 0
    per_request: list = field(default_factory=list)

    def add(self, usage) -> None:
        row = {"input": int(getattr(usage, "input_tokens", 0) or 0),
               "cache_write": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
               "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
               "output": int(getattr(usage, "output_tokens", 0) or 0)}
        self.input += row["input"]
        self.cache_write += row["cache_write"]
        self.cache_read += row["cache_read"]
        self.output += row["output"]
        self.requests += 1
        self.per_request.append(row)

    @property
    def total_input(self) -> int:
        """Every input token the model processed, cached or not."""
        return self.input + self.cache_write + self.cache_read


def heavy_cost(usage: HeavyUsage, model: str, prices: dict | None = None) -> float:
    table = prices or HEAVY_PRICES.get(model)
    if not table:
        raise KeyError(f"no price for {model}; add it to benchmarks/ab/pricing.py")
    return (usage.input * table["input"] + usage.cache_write * table["cache_write"]
            + usage.cache_read * table["cache_read"] + usage.output * table["output"]) / 1e6


def vision_cost(prompt_tokens: int, completion_tokens: int, price_in: float, price_out: float) -> float:
    return (prompt_tokens * price_in + completion_tokens * price_out) / 1e6
