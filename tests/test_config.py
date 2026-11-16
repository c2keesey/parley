import stat

from parley import config


def test_log_and_state_directory_are_private(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(config, "STATE", state)
    monkeypatch.setattr(config, "LOG", state / "speak.log")

    config.log("a privacy-safe event")

    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.LOG.stat().st_mode) == 0o600
    assert "a privacy-safe event" in config.LOG.read_text()


def test_private_write_replaces_symlink_without_touching_target(tmp_path):
    target = tmp_path / "unrelated"
    target.write_text("preserve me")
    state = tmp_path / "state"
    state.mkdir()
    path = state / "snapshot"
    path.symlink_to(target)

    config.private_write(path, "private snapshot")

    assert target.read_text() == "preserve me"
    assert not path.is_symlink()
    assert path.read_text() == "private snapshot"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
