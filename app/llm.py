"""Gemini generateContent call through the vertex gateway (REST, function calling)."""
import time

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}


def make_generate(base: str, key: str, model: str):
    def generate(body: dict) -> dict:
        for attempt in range(4):
            r = requests.post(f"{base}/{model}:generateContent", json=body, timeout=120,
                              headers={"x-goog-api-key": key})
            if r.status_code in RETRY_STATUS and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"LLM call failed: HTTP {r.status_code}")  # body deliberately not echoed
            return r.json()
        raise RuntimeError("LLM call failed after retries")
    return generate
