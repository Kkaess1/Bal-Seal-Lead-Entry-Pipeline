"""OpenAI API access shared by extraction, screening, and translation.

The standard ``OPENAI_API_KEY`` name is preferred. ``CHATGPT_API`` remains an
accepted alias because that is the name used by the existing local deployment.
"""

import base64
import json
import os
import re
import time

import requests

import secrets_store


# Pin the snapshot so extraction behavior cannot silently change underneath a
# production import. GPT-5 mini is intended for precise, high-volume tasks and
# supports the Responses API used below.
DEFAULT_OPENAI_MODEL = 'gpt-5-mini-2025-08-07'
OPENAI_RESPONSES_URL = 'https://api.openai.com/v1/responses'


def get_openai_key():
    """Return the OpenAI key from Credential Manager or the environment."""
    for name in ('OPENAI_API_KEY', 'CHATGPT_API'):
        value = (secrets_store.get_secret(name) or '').strip()
        if value:
            return value
    return ''


def get_gemini_key():
    """Deprecated compatibility alias; returns the configured OpenAI key."""
    return get_openai_key()


def get_ai_provider(provider_override=None):
    """Return the active provider and its key (legacy argument is ignored)."""
    return 'openai', get_openai_key()


def _response_text(data):
    """Extract assistant text from a raw Responses API response."""
    if data.get('output_text'):
        return str(data['output_text']).strip()

    parts = []
    for item in data.get('output') or []:
        for content in item.get('content') or []:
            if content.get('type') == 'output_text' and content.get('text'):
                parts.append(str(content['text']))
            elif content.get('type') == 'refusal' and content.get('refusal'):
                raise RuntimeError(f"OpenAI API refused the request: {content['refusal']}")
    if parts:
        return '\n'.join(parts).strip()
    raise RuntimeError('OpenAI API returned no text output.')


def _post_responses(payload: dict, api_key: str) -> dict:
    """POST one Responses API request with bounded retries; return raw JSON."""
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }

    for attempt in range(1, 4):
        try:
            response = requests.post(
                OPENAI_RESPONSES_URL, headers=headers, json=payload, timeout=90,
            )
            if response.status_code in (408, 409, 429) or response.status_code >= 500:
                if attempt < 3:
                    time.sleep(2 * attempt)
                    continue
            if not response.ok:
                raise RuntimeError(
                    f'OpenAI API error ({response.status_code}): {response.text}'
                )
            try:
                return response.json()
            except ValueError as exc:
                raise RuntimeError('OpenAI API returned invalid JSON.') from exc
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt == 3:
                raise
            time.sleep(2 * attempt)

    raise RuntimeError('OpenAI API request failed after retries.')


def _call_openai(prompt: str, api_key: str, max_tokens: int = 2048,
                 model: str = None) -> str:
    """Call the OpenAI Responses API with bounded retries."""
    return _response_text(_post_responses({
        'model': model or DEFAULT_OPENAI_MODEL,
        'input': prompt,
        'max_output_tokens': max_tokens,
    }, api_key))


def call_llm(prompt: str, provider: str = None, max_tokens: int = 2048,
             model: str = None) -> str:
    """Execute a text request using OpenAI (legacy provider argument ignored)."""
    api_key = get_openai_key()
    if not api_key:
        raise ValueError(
            'No OpenAI API key available '
            '(OPENAI_API_KEY or CHATGPT_API missing in secrets/environment).'
        )
    return _call_openai(prompt, api_key, max_tokens=max_tokens, model=model)


def call_llm_with_images(prompt: str, images, max_tokens: int = 2048,
                         model: str = None) -> str:
    """Execute a text+image request. `images` is an iterable of
    (mime_type, raw_bytes) pairs.

    Separate from call_llm() rather than an optional argument on it: the
    Responses API takes a bare string for text alone but needs the full
    role/content-parts form the moment an image is attached, and every
    existing caller passes text. Images are inlined as data URLs, so nothing
    has to be uploaded first or hosted anywhere reachable.
    """
    api_key = get_openai_key()
    if not api_key:
        raise ValueError(
            'No OpenAI API key available '
            '(OPENAI_API_KEY or CHATGPT_API missing in secrets/environment).'
        )

    content = [{'type': 'input_text', 'text': prompt}]
    for mime_type, raw in images or ():
        encoded = base64.b64encode(raw).decode('ascii')
        content.append({
            'type': 'input_image',
            'image_url': f'data:{mime_type};base64,{encoded}',
        })

    return _response_text(_post_responses({
        'model': model or DEFAULT_OPENAI_MODEL,
        'input': [{'role': 'user', 'content': content}],
        'max_output_tokens': max_tokens,
    }, api_key))


def call_llm_with_tools(prompt: str, tools: list, dispatch, max_rounds: int = 8,
                        max_tokens: int = 2048, model: str = None,
                        text_format: dict = None) -> str:
    """Run the model with callable tools until it answers in plain text.

    `dispatch(name, arguments)` runs one tool call and returns anything
    json-serializable; whatever it returns is handed straight back to the
    model. The tool itself owns its own limits -- this loop only bounds how
    many turns it may take, and the last turn is offered no tools at all, so
    a model still browsing when the budget runs out has to answer from what
    it has already seen rather than losing the work to an exception.

    `text_format` is the Responses API `text.format` object -- pass a
    json_schema one to make the final answer parseable by construction. A
    mid-browse model asked for JSON in the prompt alone will sometimes narrate
    its next step instead ("I will now fetch..."), which is not an answer and
    not JSON; the schema is what stops that.
    """
    api_key = get_openai_key()
    if not api_key:
        raise ValueError(
            'No OpenAI API key available '
            '(OPENAI_API_KEY or CHATGPT_API missing in secrets/environment).'
        )

    conversation = [{'role': 'user', 'content': prompt}]
    for turn in range(max_rounds):
        payload = {
            'model': model or DEFAULT_OPENAI_MODEL,
            'input': conversation,
            'tools': tools if turn < max_rounds - 1 else [],
            'max_output_tokens': max_tokens,
        }
        if text_format:
            payload['text'] = {'format': text_format}
        data = _post_responses(payload, api_key)

        output = data.get('output') or []
        calls = [item for item in output if item.get('type') == 'function_call']
        if not calls:
            return _response_text(data)

        # Reasoning items are echoed back alongside the calls: the Responses
        # API expects the previous turn returned intact, not summarized.
        conversation.extend(output)
        for call in calls:
            try:
                arguments = json.loads(call.get('arguments') or '{}')
            except ValueError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            try:
                result = dispatch(call.get('name'), arguments)
            except Exception as exc:  # a tool failure is the model's to handle
                result = {'error': f'{type(exc).__name__}: {exc}'}
            conversation.append({
                'type': 'function_call_output',
                'call_id': call.get('call_id'),
                'output': json.dumps(result, default=str),
            })

    raise RuntimeError(f'Tool loop did not produce an answer in {max_rounds} turns.')


def parse_json_response(raw: str) -> dict:
    """Robustly extract and parse a JSON object from model output text."""
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

    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass

    # One response can carry more than one output_text part, and a model under
    # a json_schema format answers in each of them -- so the joined text is two
    # complete objects in a row, which is "Extra data", not malformed JSON.
    try:
        parsed, _ = json.JSONDecoder(strict=False).raw_decode(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    cleaned = re.sub(r',\s*([\}\]])', r'\1', text)
    try:
        return json.loads(cleaned, strict=False)
    except json.JSONDecodeError:
        pass

    cleaned = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', text)
    cleaned = re.sub(r',\s*([\}\]])', r'\1', cleaned)
    return json.loads(cleaned, strict=False)
