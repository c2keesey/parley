"""Speech synthesis against OpenAI or ElevenLabs."""
import json
import urllib.error
import urllib.parse
import urllib.request

from parley import config

OPENAI_ENDPOINT = "https://api.openai.com/v1/audio/speech"
ELEVENLABS_ENDPOINT = "https://api.elevenlabs.io/v1/text-to-speech"
ELEVENLABS_VOICES_ENDPOINT = "https://api.elevenlabs.io/v2/voices"


def synthesize(text, voice=None, model=None, provider=None):
    """MP3 bytes for text from the configured provider."""
    provider = provider or config.provider()
    if provider == "elevenlabs":
        return _elevenlabs(text, voice, model)
    if provider != "openai":
        raise RuntimeError(f"unknown queued TTS provider: {provider}")
    return _openai(text, voice, model)


def _openai(text, voice=None, model=None):
    """OpenAI speech, falling down the model list if one is retired."""
    key = config.api_key()
    if not key:
        raise RuntimeError(
            "No OPENAI_API_KEY. Set it in the environment or in "
            "~/.config/parley/env"
        )
    voice = voice or config.VOICE
    model = model or config.MODEL

    def call(name):
        body = {
            "model": name,
            "voice": voice,
            "input": text[: config.MAX_CHARS],
            "response_format": "mp3",
            "speed": config.SPEED,
        }
        # Only the gpt-* speech models accept delivery instructions.
        if name.startswith("gpt-") and config.INSTRUCTIONS:
            body["instructions"] = config.INSTRUCTIONS
        request = urllib.request.Request(
            OPENAI_ENDPOINT,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        return urllib.request.urlopen(request, timeout=90).read()

    failure = None
    for name in [model] + [m for m in config.FALLBACKS if m != model]:
        try:
            return call(name)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode(errors="replace")
            failure = f"{name} -> {exc.code}: {detail}"
            config.log(f"synth failed {failure}")
    raise RuntimeError(failure or "synthesis failed")


def _elevenlabs(text, voice=None, model=None):
    key = config.elevenlabs_api_key()
    if not key:
        raise RuntimeError(
            "No ELEVENLABS_API_KEY. Set it in the environment or in "
            "~/.config/parley/env"
        )
    voice = voice or config.ELEVENLABS_VOICE
    model = model or config.ELEVENLABS_MODEL
    endpoint = (
        f"{ELEVENLABS_ENDPOINT}/{urllib.parse.quote(voice, safe='')}"
        "?output_format=mp3_44100_128"
    )
    body = {
        "text": text[: config.MAX_CHARS],
        "model_id": model,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers={
            "xi-api-key": key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    try:
        return urllib.request.urlopen(request, timeout=90).read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode(errors="replace")
        failure = f"elevenlabs -> {exc.code}: {detail}"
        config.log(f"synth failed {failure}")
        raise RuntimeError(failure) from exc


def voices():
    """The caller's ElevenLabs voices, for choosing a stable voice ID."""
    key = config.elevenlabs_api_key()
    if not key:
        raise RuntimeError(
            "No ELEVENLABS_API_KEY. Set it before listing voices.")
    request = urllib.request.Request(
        ELEVENLABS_VOICES_ENDPOINT + "?page_size=100&include_total_count=false",
        headers={"xi-api-key": key, "Accept": "application/json"},
    )
    return json.loads(urllib.request.urlopen(request, timeout=30).read())["voices"]
