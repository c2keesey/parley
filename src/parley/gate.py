"""Speech gate: an adaptive noise floor plus a voice-activity detector.

The listener's cheapest layer decides which stretches of microphone audio are
worth showing to Whisper. A fixed RMS threshold cannot do that well: the right
level depends on the microphone, its input gain, the distance to the speaker,
and the room. Set it for a loud USB microphone and a laptop's built-in one only
crosses it on the loudest syllable of each word; set it for the laptop and a
fan near the USB microphone opens it permanently.

So the gate measures the room instead:

  * a noise floor follows the level of non-speech frames, quickly downward and
    slowly upward, so a burst of speech never drags it up but a new fan does;
  * a frame can open a burst only when it is well above that floor (and above
    a small absolute minimum) and, when available, WebRTC's VAD calls it speech;
  * once open, a lower "stay open" level applies (hysteresis), so soft
    consonants and word endings do not split one utterance into fragments;
  * a ring buffer keeps the frames just before the onset (pre-roll), so the
    soft start of "send it" is not clipped to "end it";
  * two consecutive speech frames are needed to open, which ignores most
    keyboard clicks and desk knocks;
  * while Parley itself is talking the opening bar is raised a little.

The gate is pure: feed it frames, it returns events. Nothing here touches the
microphone, so tests and the offline evaluator can drive it with recordings.
"""
import collections
import math
import os
import struct

RATE = 16000
FRAME = 480                 # 30 ms: one of the frame sizes WebRTC's VAD accepts
FRAME_SECONDS = FRAME / RATE

# An absolute minimum opening level. A legacy PARLEY_MIC_THRESHOLD still wins,
# so a hand-tuned value keeps working, but it is now a floor under an adaptive
# threshold rather than the whole decision.
MIN_RMS = int(os.environ.get(
    "PARLEY_MIC_THRESHOLD", os.environ.get("PARLEY_MIN_RMS", "60")))
OPEN_RATIO = float(os.environ.get("PARLEY_GATE_OPEN_RATIO", "3.0"))   # ~+9.5 dB
STAY_RATIO = float(os.environ.get("PARLEY_GATE_STAY_RATIO", "1.8"))   # ~+5 dB
PLAYING_RATIO = float(os.environ.get("PARLEY_GATE_PLAYING_RATIO", "1.5"))
VAD_MODE = int(os.environ.get("PARLEY_VAD_MODE", "2"))
ONSET_FRAMES = 2
PREROLL_SECONDS = 0.30
MIN_SPEECH = float(os.environ.get("PARLEY_MIN_SPEECH", "0.12"))
SHORT_SPEECH = 0.05         # shorter than this is not even worth a log line
END_SILENCE = 0.7
INTERRUPT_SILENCE = 0.25
MAX_BURST = 15.0
FLOOR_INITIAL = 30.0
FLOOR_DOWN = 0.25           # per-frame smoothing when the room gets quieter
FLOOR_UP = 0.01             # ...and when it gets louder (about 3 s to follow)
# Inside a burst the floor still creeps up, much more slowly. Speech barely
# moves it, but a fan that switches on cannot hold the gate open forever.
FLOOR_IN_BURST = 0.002


def rms(chunk):
    usable = len(chunk) // 2 * 2
    if usable < 2:
        return 0
    samples = struct.unpack(f"{usable // 2}h", chunk[:usable])
    return int(math.sqrt(sum(s * s for s in samples) / len(samples)))


_VAD_UNSET = object()
_vad_cache = _VAD_UNSET


def default_vad():
    """Return a frame classifier from webrtcvad, or None without it."""
    global _vad_cache
    if _vad_cache is not _VAD_UNSET:
        return _vad_cache
    if os.environ.get("PARLEY_VAD", "webrtc").lower() in {"0", "off", "none"}:
        _vad_cache = None
        return None
    try:
        import webrtcvad
    except ImportError:
        _vad_cache = None
        return None
    detector = webrtcvad.Vad(max(0, min(3, VAD_MODE)))

    def classify(frame):
        if len(frame) != FRAME * 2:
            return True
        try:
            return detector.is_speech(frame, RATE)
        except Exception:
            return True

    _vad_cache = classify
    return classify


class Burst(list):
    """The frames of one utterance, plus what the gate measured about it."""

    def __init__(self, frames=(), voiced=0.0, peak=0, floor=0.0,
                 threshold=0.0, overlapped=False, short=False):
        super().__init__(frames)
        self.voiced = voiced
        self.peak = peak
        self.floor = floor
        self.threshold = threshold
        self.overlapped = overlapped
        self.short = short

    def describe(self):
        return (f"voiced={self.voiced:.2f}s peak={self.peak} "
                f"floor={self.floor:.0f} open={self.threshold:.0f}")


class SpeechGate:
    """Turn a stream of fixed-size PCM frames into speech bursts."""

    def __init__(self, vad=_VAD_UNSET, min_rms=None, open_ratio=None,
                 stay_ratio=None, playing_ratio=None, preroll=PREROLL_SECONDS,
                 onset_frames=ONSET_FRAMES, min_speech=None,
                 end_silence=END_SILENCE, interrupt_silence=INTERRUPT_SILENCE,
                 max_burst=MAX_BURST, floor=FLOOR_INITIAL,
                 frame_seconds=FRAME_SECONDS):
        self.vad = default_vad() if vad is _VAD_UNSET else vad
        self.min_rms = MIN_RMS if min_rms is None else min_rms
        self.open_ratio = OPEN_RATIO if open_ratio is None else open_ratio
        self.stay_ratio = STAY_RATIO if stay_ratio is None else stay_ratio
        self.playing_ratio = (
            PLAYING_RATIO if playing_ratio is None else playing_ratio)
        self.onset_frames = max(1, onset_frames)
        self.min_speech = MIN_SPEECH if min_speech is None else min_speech
        self.end_silence = end_silence
        self.interrupt_silence = interrupt_silence
        self.max_burst = max_burst
        self.frame_seconds = frame_seconds
        self.floor = float(floor)
        self.preroll = collections.deque(
            maxlen=max(1, round(preroll / frame_seconds)))
        self._reset()

    def _reset(self):
        self.buffer = None
        self.voiced = 0.0
        self.quiet = 0.0
        self.peak = 0
        self.overlapped = False
        self.onset = []
        self.opened_at = 0.0

    @property
    def open(self):
        return self.buffer is not None

    def open_level(self, playing=False):
        level = max(float(self.min_rms), self.floor * self.open_ratio)
        return level * (self.playing_ratio if playing else 1.0)

    def stay_level(self):
        return max(self.min_rms * 0.6, self.floor * self.stay_ratio)

    def _track_floor(self, level, rate=None):
        if rate is None:
            rate = FLOOR_DOWN if level < self.floor else FLOOR_UP
        self.floor += (level - self.floor) * rate
        self.floor = max(1.0, self.floor)

    def classify(self, chunk, playing=False):
        """(level, opens, stays, vad) for one frame, without changing state."""
        level = rms(chunk)
        is_voice = True if self.vad is None else bool(self.vad(chunk))
        opens = level >= self.open_level(playing) and is_voice
        stays = level >= self.stay_level() and is_voice
        return level, opens, stays, is_voice

    def feed(self, chunk, playing=False):
        """Consume one frame. Returns (event, burst).

        event is "start" on the frame that opens a burst, "end" when a burst
        of real speech completes, "short" when an opened burst turned out too
        brief to be speech, or None.
        """
        level, opens, stays, _ = self.classify(chunk, playing)

        if self.buffer is None:
            if opens:
                self.onset.append(chunk)
                if len(self.onset) < self.onset_frames:
                    return None, None
                self.buffer = list(self.preroll) + list(self.onset)
                self.preroll.clear()
                self.voiced = len(self.onset) * self.frame_seconds
                self.peak = level
                self.quiet = 0.0
                self.overlapped = playing
                self.onset = []
                return "start", None
            if self.onset:
                # A single loud frame (click, knock) never opens the gate, and
                # it stays out of the noise floor too.
                self.preroll.extend(self.onset)
                self.onset = []
            else:
                self._track_floor(level)
            self.preroll.append(chunk)
            return None, None

        self.buffer.append(chunk)
        self.overlapped = self.overlapped or playing
        if level > self.floor:
            self._track_floor(level, FLOOR_IN_BURST)
        if stays:
            self.voiced += self.frame_seconds
            self.quiet = 0.0
            self.peak = max(self.peak, level)
        else:
            self.quiet += self.frame_seconds
        limit = self.interrupt_silence if self.overlapped else self.end_silence
        if self.quiet < limit and self.voiced < self.max_burst:
            return None, None

        burst = Burst(
            self.buffer, voiced=self.voiced, peak=self.peak, floor=self.floor,
            threshold=self.open_level(self.overlapped),
            overlapped=self.overlapped,
            short=self.voiced < self.min_speech)
        self._reset()
        return ("short" if burst.short else "end"), burst
