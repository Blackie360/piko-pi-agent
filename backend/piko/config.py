"""Paths, explicit configuration and model settings."""
import json, os
from pathlib import Path
from urllib.parse import urlsplit
from agents import set_tracing_disabled
from openai import APIConnectionError, APITimeoutError

BASE = Path(__file__).resolve().parents[2]

WORKSPACE = Path(os.environ.get('PI_AGENT_WORKSPACE', str(Path.home() / 'piko-workspace'))).expanduser().resolve()

WORKSPACE.mkdir(mode=448, parents=True, exist_ok=True)

def configured_origins(value):
    """Accept explicit HTTPS origins only; an empty set denies browser mutations."""
    result = set()
    for origin in value.split(','):
        origin = origin.strip()
        if not origin:
            continue
        parsed = urlsplit(origin)
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError('PI_AGENT_ALLOWED_ORIGINS contains an invalid port') from exc
        if parsed.scheme != 'https' or not parsed.hostname or '*' in origin or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or (origin != f'https://{parsed.netloc}'):
            raise ValueError('PI_AGENT_ALLOWED_ORIGINS must contain explicit HTTPS origins without paths')
        result.add(origin)
    return frozenset(result)

ALLOWED_ORIGINS = configured_origins(os.environ.get('PI_AGENT_ALLOWED_ORIGINS', ''))

DATA = Path(os.environ.get('PI_AGENT_DATA', str(BASE / 'data')))

DATA.mkdir(mode=448, exist_ok=True)

os.umask(63)

set_tracing_disabled(True)

WHATSAPP_ROUTE_ENABLED = os.environ.get('PI_WHATSAPP_ENABLED') == '1'

def settings():
    p = DATA / 'settings.json'
    return json.loads(p.read_text()) if p.exists() else {'model': 'gpt-6.1-sol', 'key': ''}

MODEL_OPTIONS = [{'id': 'gpt-6.1-sol', 'label': 'GPT-6.1 Sol · Latest', 'group': 'GPT-6'}, {'id': 'gpt-6-astra', 'label': 'GPT-6 Astra', 'group': 'GPT-6'}, {'id': 'gpt-6-sol', 'label': 'GPT-6 Sol', 'group': 'GPT-6'}, {'id': 'gpt-6-luna', 'label': 'GPT-6 Luna', 'group': 'GPT-6'}, {'id': 'gpt-5.6-sol', 'label': 'GPT-5.6 Sol', 'group': 'GPT-5'}, {'id': 'gpt-5.6-terra', 'label': 'GPT-5.6 Terra', 'group': 'GPT-5'}, {'id': 'gpt-5.6-luna', 'label': 'GPT-5.6 Luna', 'group': 'GPT-5'}, {'id': 'gpt-5.5', 'label': 'GPT-5.5', 'group': 'GPT-5'}, {'id': 'gpt-5.4', 'label': 'GPT-5.4', 'group': 'GPT-5'}, {'id': 'gpt-5.4-mini', 'label': 'GPT-5.4 Mini', 'group': 'GPT-5'}, {'id': 'gpt-5.2', 'label': 'GPT-5.2', 'group': 'GPT-5'}, {'id': 'gpt-5', 'label': 'GPT-5', 'group': 'GPT-5'}, {'id': 'gpt-5-mini', 'label': 'GPT-5 Mini', 'group': 'GPT-5'}, {'id': 'gpt-5-nano', 'label': 'GPT-5 Nano', 'group': 'GPT-5'}]

PERSONAS = {'warm': ('Warm and direct', 'Use warm, plain language and lead with the answer.'), 'concise': ('Concise', 'Use compact sentences and omit optional explanation.'), 'guide': ('Patient guide', 'Explain the key reason or next step patiently in plain language.')}

def persona_instructions():
    return 'Tone only: ' + PERSONAS.get(settings().get('persona', 'warm'), PERSONAS['warm'])[1] + ' This never changes tools, permissions or approval requirements.'

def api_error(exc):
    code = getattr(exc, 'code', None)
    if code in ('credit_balance_exhausted', 'insufficient_quota'):
        return 'Your OpenAI API credit balance is exhausted. Add credits in OpenAI Platform billing, then try again.'
    status = getattr(exc, 'status_code', None)
    if status == 401:
        return 'OpenAI rejected this API key. Check that it is current and complete.'
    if status == 403:
        return 'This API key does not have permission. Check its project and API permissions.'
    if status == 404:
        return 'This model is unavailable for your API project. Choose a model you can access.'
    if status == 429:
        return 'OpenAI rate limit reached. Wait a moment and try again.'
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return 'The Pi could not connect to OpenAI. Try again shortly.'
    return 'OpenAI request failed (' + type(exc).__name__ + '). Try again or check your model settings.'
