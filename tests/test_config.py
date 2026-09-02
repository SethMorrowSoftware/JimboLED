import json

from jimboled.config import DEFAULT_CONFIG, ConfigStore


def test_creates_default_config(data_dir):
    store = ConfigStore(data_dir)
    assert (data_dir / "config.json").exists()
    cfg = store.get()
    assert cfg["dashboard"]["title"] == "JimboLED"
    assert cfg["version"] == DEFAULT_CONFIG["version"]


def test_update_is_atomic_and_backed_up(data_dir):
    store = ConfigStore(data_dir)
    store.update(lambda c: c["dashboard"].__setitem__("title", "Jim"), backup_reason="test")
    assert json.load(open(data_dir / "config.json"))["dashboard"]["title"] == "Jim"
    assert store.list_backups()
    # no temp files left behind
    assert not list(data_dir.glob(".config-*"))


def test_update_rolls_back_on_exception(data_dir):
    store = ConfigStore(data_dir)

    def bad(c):
        c["dashboard"]["title"] = "broken"
        raise ValueError("nope")

    try:
        store.update(bad)
    except ValueError:
        pass
    assert store.get()["dashboard"]["title"] == "JimboLED"


def test_broken_file_is_preserved_not_destroyed(data_dir):
    data_dir.mkdir()
    (data_dir / "config.json").write_text("{not json")
    store = ConfigStore(data_dir)
    assert store.get()["dashboard"]["title"] == "JimboLED"
    assert list(data_dir.glob("config.broken-*.json"))


def test_missing_keys_are_filled_from_defaults(data_dir):
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({"devices": [{"id": "x", "host": "1.2.3.4"}]}))
    store = ConfigStore(data_dir)
    cfg = store.get()
    assert cfg["devices"][0]["id"] == "x"
    assert cfg["gpio"]["switches"] == []
    assert cfg["dashboard"]["theme"] == "midnight"


def test_listeners_called_with_before_after(data_dir):
    store = ConfigStore(data_dir)
    seen = []
    store.on_change(lambda b, a: seen.append((b["dashboard"]["title"], a["dashboard"]["title"])))
    store.update(lambda c: c["dashboard"].__setitem__("title", "New"))
    store.update(lambda c: None)  # no change -> no notification
    assert seen == [("JimboLED", "New")]
