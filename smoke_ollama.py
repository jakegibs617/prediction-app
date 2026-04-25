"""Smoke test: confirm Ollama /api/generate with mistral:7b returns valid JSON."""
import json
import sys

import httpx


def main() -> int:
    payload = {
        "model": "mistral:7b",
        "prompt": "Return valid JSON only with one key 'status' set to 'ok'.",
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": 64},
    }
    try:
        resp = httpx.post(
            "http://localhost:11434/api/generate",
            json=payload,
            timeout=120.0,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"REQUEST FAILED: {e}")
        return 2

    data = resp.json()
    content = data.get("response", "")
    print(f"raw_response_chars={len(content)}")
    print(f"prompt_eval_count={data.get('prompt_eval_count')}")
    print(f"eval_count={data.get('eval_count')}")
    print(f"model={data.get('model')}")
    # Try to parse the response as JSON
    try:
        parsed = json.loads(content)
        print(f"PARSED_JSON_OK keys={list(parsed.keys())}")
        print(f"value_status={parsed.get('status')}")
        return 0
    except json.JSONDecodeError as e:
        print(f"JSON_PARSE_FAILED: {e}")
        # Print first 200 chars (ascii-only) to avoid Windows charmap errors
        safe = content.encode("ascii", "replace").decode("ascii")
        print(f"first_200={safe[:200]}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
