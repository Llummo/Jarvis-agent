from meta_harness.project_config import ProjectConfigStore


def test_get_base_url_returns_none_when_unset(tmp_path):
    store = ProjectConfigStore(tmp_path / "cfg.json")

    assert store.get_base_url("sigo-front") is None


def test_set_and_get_base_url(tmp_path):
    store = ProjectConfigStore(tmp_path / "cfg.json")

    store.set_base_url("sigo-front", "https://sigo-front.vercel.app")

    assert store.get_base_url("sigo-front") == "https://sigo-front.vercel.app"


def test_set_base_url_persists_across_instances(tmp_path):
    path = tmp_path / "cfg.json"
    ProjectConfigStore(path).set_base_url("gru-po", "https://gru-po.example.com")

    reloaded = ProjectConfigStore(path)

    assert reloaded.get_base_url("gru-po") == "https://gru-po.example.com"


def test_set_base_url_overwrites_existing(tmp_path):
    store = ProjectConfigStore(tmp_path / "cfg.json")
    store.set_base_url("sigo-front", "https://old.example.com")

    store.set_base_url("sigo-front", "https://new.example.com")

    assert store.get_base_url("sigo-front") == "https://new.example.com"


def test_list_all_returns_every_configured_project(tmp_path):
    store = ProjectConfigStore(tmp_path / "cfg.json")
    store.set_base_url("sigo-front", "https://sigo-front.example.com")
    store.set_base_url("gru-po", "https://gru-po.example.com")

    assert store.list_all() == {
        "sigo-front": "https://sigo-front.example.com",
        "gru-po": "https://gru-po.example.com",
    }
