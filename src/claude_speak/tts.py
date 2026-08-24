"""Speech synthesis against the OpenAI audio API."""
import json
import urllib.error
import urllib.request

from claude_speak import config

ENDPOINT = "https://api.openai.com/v1/audio/speech"


def synthesize(text, voice=None, model=None):
    """MP3 bytes for text. Falls back down the model list if one is retired."""
    key = config.api_key()
    if not key:
        raise RuntimeError(
            "No OPENAI_API_KEY. Set it in the environment or in "
            "~/.config/claude-speak/env"
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
            ENDPOINT,
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
