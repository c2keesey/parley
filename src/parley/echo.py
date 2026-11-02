"""Recognise Parley's own voice coming back through the microphone.

Without headphones the microphone hears every reply. That is fine for ordinary
words — only a wake or stop phrase is trusted in audio that overlaps playback —
but a reply that itself says "okay computer" used to wake the listener, which
then paused the reply and captured the rest of it as a dictated message.

The player records the text of each block it plays, with start and end times.
The listener compares what local Whisper heard with that text: a trigger heard
during, or just after, a block that contains the same trigger, in words that
are mostly that block's words, is the speaker, not the person.
"""
import json
import time

from parley import config

RECENT = 4
# A burst can hold up to MAX_BURST seconds of audio and is classified after it
# ends, so a block finished this recently may still be in what was heard.
TAIL_SECONDS = 20.0
OVERLAP = 0.6


def _path():
    return config.STATE / "spoken-recent.json"


def _load():
    try:
        entries = json.loads(_path().read_text())
    except (OSError, ValueError):
        return []
    return entries if isinstance(entries, list) else []


def remember(text, now=None):
    """Note that a block of text has started playing."""
    entries = _load()[-(RECENT - 1):]
    entries.append({"text": text or "", "started": now or time.time(),
                    "ended": None})
    try:
        config.STATE.mkdir(parents=True, exist_ok=True)
        config.private_write(_path(), json.dumps(entries))
    except OSError:
        pass


def finished(now=None):
    """Mark the most recent block as no longer playing."""
    entries = _load()
    if not entries:
        return
    if entries[-1].get("ended") is None:
        entries[-1]["ended"] = now or time.time()
    try:
        config.private_write(_path(), json.dumps(entries))
    except OSError:
        pass


def recent_texts(now=None):
    """Text of blocks playing now or that stopped within TAIL_SECONDS."""
    now = now or time.time()
    texts = []
    for entry in _load():
        ended = entry.get("ended")
        if ended is None or now - float(ended) <= TAIL_SECONDS:
            texts.append(str(entry.get("text", "")))
    return texts


def words(text):
    cleaned = "".join(
        c if c.isalnum() or c.isspace() else " "
        for c in (text or "").lower().replace("'", "").replace("’", ""))
    return cleaned.split()


def _contains(haystack, needle):
    width = len(needle)
    return bool(width) and any(
        haystack[index:index + width] == needle
        for index in range(len(haystack) - width + 1))


def is_echo(heard, trigger_variants, spoken_texts):
    """Whether a trigger in `heard` is better explained by Parley's speech.

    trigger_variants is a sequence of word tuples for one trigger (for the
    wake phrase: "okay computer", "ok computer", ...). It is echo only when a
    recent block itself contains one of those variants and the other words
    heard alongside it mostly come from the same block. A person saying the
    trigger over a reply that never mentions it is always a real trigger.
    """
    heard_words = words(heard)
    variants = [list(variant) for variant in trigger_variants if variant]
    for text in spoken_texts:
        spoken = words(text)
        if not any(_contains(spoken, variant) for variant in variants):
            continue
        trigger_words = {word for variant in variants for word in variant}
        rest = [word for word in heard_words if word not in trigger_words]
        if not rest:
            return True
        vocabulary = set(spoken)
        shared = sum(word in vocabulary for word in rest)
        if shared / len(rest) >= OVERLAP:
            return True
    return False
