"""
LLM call provider dedicated to Gemini (Google).
Handles API key retrieval, Gemini model calls, retries, and JSON parsing.
"""

import os
import json
import time
import requests
import secrets_store

DEFAULT_GEMINI_MODEL = 'gemini-flash-latest'


def get_gemini_key():
    """Retrieves the Gemini API key from secrets_store or environment."""
    return secrets_store.get_secret('GEMINI_API_KEY').strip() or os.getenv('GEMINI_API_KEY', '').strip()


def get_ai_provider(provider_override=None):
    """Compatibility function returning provider 'gemini' and its API key."""
    return 'gemini', get_gemini_key()


def _call_gemini(prompt: str, api_key: str, max_tokens: int = 2048, model: str = None) -> str:
    target_model = model or DEFAULT_GEMINI_MODEL
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
            max_output_tokens=max_tokens,
        )
        response = client.models.generate_content(
            model=target_model,
            contents=prompt,
            config=config,
        )
        return response.text.strip()
    except Exception:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent?key={api_key}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": max_tokens,
                "temperature": 0.1
            }
        }
        # Try up to 3 attempts with brief pause on 503 (High Demand), 429, or read timeouts
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=60)
                if resp.status_code in (503, 429) and attempt < max_attempts:
                    time.sleep(2 * attempt)
                    continue
                if not resp.ok:
                    raise RuntimeError(f"Gemini API error ({resp.status_code}): {resp.text}")
                data = resp.json()
                try:
                    return data['candidates'][0]['content']['parts'][0]['text'].strip()
                except (KeyError, IndexError):
                    raise RuntimeError(f"Unexpected response format from Gemini API: {data}")
            except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout) as e:
                if attempt == max_attempts:
                    raise e
                time.sleep(2)


def call_llm(prompt: str, provider: str = None, max_tokens: int = 2048, model: str = None) -> str:
    """
    Executes an LLM request using Gemini.

    Parameters
    ----------
    prompt      : prompt text string
    provider    : ignored (retained for signature backwards compatibility)
    max_tokens  : max tokens for response
    model       : optional specific Gemini model string

    Returns
    -------
    Raw text response from Gemini.
    """
    api_key = get_gemini_key()
    if not api_key:
        raise ValueError("No Gemini API key available (GEMINI_API_KEY missing in secrets/env).")

    return _call_gemini(prompt, api_key, max_tokens=max_tokens, model=model)


def parse_json_response(raw: str) -> dict:
    """
    Robustly extracts and parses a JSON object from model output text,
    stripping markdown backticks, commentary, control characters, or extra text.
    """
    import re
    text = (raw or '').strip()
    if '```' in text:
        for chunk in text.split('```'):
            chunk = chunk.strip()
            if chunk.startswith('json'):
                chunk = chunk[4:].strip()
            if chunk.startswith('{') and chunk.endswith('}'):
                text = chunk
                break
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    # Attempt 1: Standard json.loads with strict=False (allows unescaped newlines/tabs)
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass

    # Attempt 2: Remove trailing commas before } or ]
    cleaned = re.sub(r',\s*([\}\]])', r'\1', text)
    try:
        return json.loads(cleaned, strict=False)
    except json.JSONDecodeError:
        pass

    # Attempt 3: Remove invalid control characters
    cleaned = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', text)
    cleaned = re.sub(r',\s*([\}\]])', r'\1', cleaned)
    return json.loads(cleaned, strict=False)
