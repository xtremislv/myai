"""Load test: how the model server copes with several chats at once.

    .venv/bin/python -m scripts.load_test                    # 1, 4, 8 and 16 at once, straight to vLLM
    .venv/bin/python -m scripts.load_test --levels 1 8 --prompt-tokens 3000

Each simulated chat streams one reply. Reported per level: time to first word (p50/p95), whole
reply time (p50/p95), and total words per second across all chats. Uses LLM_BASE_URL from .env.
"""
from __future__ import annotations

import argparse
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

from app.config import get_settings

FILLER = (
    "The warehouse in Pune received 40 laptops, 12 monitors and 200 cables on Monday. "
    "Two monitors were damaged in transit and returned to the supplier. "
)


def one_chat(client: OpenAI, model: str, prompt: str, max_tokens: int) -> tuple[float, float, int]:
    start = time.perf_counter()
    first = None
    tokens = 0
    stream = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}], max_tokens=max_tokens,
        temperature=0.7, stream=True, stream_options={"include_usage": True},
    )
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content and first is None:
            first = time.perf_counter() - start
        if chunk.usage:
            tokens = chunk.usage.completion_tokens
    return first or 0.0, time.perf_counter() - start, tokens


def pct(values: list[float], p: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, round(p * (len(values) - 1)))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--levels", type=int, nargs="+", default=[1, 4, 8, 16])
    parser.add_argument("--prompt-tokens", type=int, default=1500, help="rough prompt size")
    parser.add_argument("--max-tokens", type=int, default=300)
    parser.add_argument("--rounds", type=int, default=2, help="chats per concurrent slot")
    args = parser.parse_args()

    settings = get_settings()
    client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=600)
    filler = FILLER * max(1, args.prompt_tokens // 35)
    print(f"model {settings.llm_model}, prompt ~{args.prompt_tokens} tokens, reply up to {args.max_tokens}")
    print(f"{'at once':>8} {'first word p50/p95':>20} {'whole reply p50/p95':>21} {'tokens/s total':>15} {'per chat':>9}")
    for level in args.levels:
        prompts = [f"[{i}] {filler}\nSummarize the stock movements above in a short report." for i in range(level * args.rounds)]
        started = time.perf_counter()
        with ThreadPoolExecutor(level) as pool:
            results = list(pool.map(lambda p: one_chat(client, settings.llm_model, p, args.max_tokens), prompts))
        wall = time.perf_counter() - started
        firsts = [r[0] for r in results]
        totals = [r[1] for r in results]
        tokens = sum(r[2] for r in results)
        per_chat = statistics.mean(r[2] / max(r[1] - r[0], 1e-6) for r in results)
        print(f"{level:>8} {pct(firsts, .5):>9.2f}s/{pct(firsts, .95):>6.2f}s {pct(totals, .5):>10.2f}s/{pct(totals, .95):>6.2f}s "
              f"{tokens / wall:>15.0f} {per_chat:>9.1f}")


if __name__ == "__main__":
    main()
