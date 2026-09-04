import ctypes

from parley import audio


def test_core_audio_default_device_query(monkeypatch):
    class API:
        @staticmethod
        def AudioObjectGetPropertyData(
                object_id, address, qualifier_size, qualifier, size, value):
            assert object_id == audio.SYSTEM_OBJECT
            ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32)).contents.value = 42
            return 0

    monkeypatch.setattr(audio, "_core_audio", lambda: API())

    assert audio.default_input_device() == 42
    assert audio.default_output_device() == 42


def test_core_audio_query_degrades_when_unavailable(monkeypatch):
    monkeypatch.setattr(audio, "_core_audio", lambda: None)

    assert audio.default_input_device() is None
    assert audio.default_output_device() is None
