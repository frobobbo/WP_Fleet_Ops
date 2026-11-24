"""Wide observation tables must remain usable without a pointer."""
import importlib
import warnings
from contextlib import closing
from html.parser import HTMLParser

import pytest


@pytest.fixture
def accessible_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_FLEET_OPS_DB", str(tmp_path / "accessible.sqlite3"))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import wp_fleet_ops.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, main


class TableAccessibilityParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.headings = {}
        self.wrappers = []
        self._heading = None
        self._wrapper = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "h3":
            self._heading = attrs.get("id")
            if self._heading:
                self.headings[self._heading] = ""
        if tag == "div" and "table-wrap" in (attrs.get("class") or "").split():
            self._wrapper = {"attrs": attrs, "tables": [], "headers": []}
            self.wrappers.append(self._wrapper)
        elif tag == "table" and self._wrapper is not None:
            self._wrapper["tables"].append(attrs)
        elif tag == "th" and self._wrapper is not None:
            self._wrapper["headers"].append(attrs)

    def handle_data(self, data):
        if self._heading:
            self.headings[self._heading] += data

    def handle_endtag(self, tag):
        if tag == "h3":
            self._heading = None
        elif tag == "table":
            self._wrapper = None


@pytest.mark.parametrize("state", ["empty", "current", "stale"])
def test_observation_tables_are_named_keyboard_scroll_regions(accessible_client, state):
    client, main = accessible_client
    if state != "empty":
        response = client.post("/snapshot", data={
            "name": "Accessible Site", "url": "https://accessible.example",
            "client": "Care Client", "uptime_ok": "true", "ssl_days": 90,
            "wp_updates": 0, "backup_age_hours": 12,
            "response_ms": 100, "security_header_count": 3,
        }, follow_redirects=False)
        assert response.status_code == 303
        if state == "stale":
            with closing(main.store._connect()) as con, con:
                con.execute("update snapshots set captured_at = '2000-01-01 00:00:00'")
    before = (main.store.latest_dashboard(), main.store.latest_care_checks())
    summary = client.get("/api/summary").json()

    response = client.get("/")
    assert response.status_code == 200
    parser = TableAccessibilityParser()
    parser.feed(response.text)
    assert len(parser.ids) == len(set(parser.ids)), "Accessible label IDs must be unique."
    assert len(parser.wrappers) == 2
    for wrapper, title in zip(
        parser.wrappers, ("Fleet operations", "Client care checks"), strict=True,
    ):
        attrs = wrapper["attrs"]
        if state == "empty":
            assert not wrapper["tables"]
            assert "tabindex" not in attrs, "Empty placeholders must not add tab stops."
            assert "role" not in attrs
            assert "aria-labelledby" not in attrs
        else:
            assert attrs.get("tabindex") == "0", "Overflow must be reachable in normal tab order."
            assert attrs.get("role") == "region"
            label = attrs.get("aria-labelledby")
            assert label and parser.headings.get(label) == title
            assert len(wrapper["tables"]) == 1
            assert wrapper["tables"][0].get("aria-labelledby") == label
            assert wrapper["headers"]
            assert all(header.get("scope") == "col" for header in wrapper["headers"])
    # Focus styling stays local and visible without changing overflow behavior.
    assert ".table-wrap:focus-visible" in response.text
    assert "outline:2px solid var(--brand-2)" in response.text
    assert "outline-offset:-2px" in response.text
    assert ".table-wrap{overflow-x:auto}" in response.text
    assert (main.store.latest_dashboard(), main.store.latest_care_checks()) == before
    after_summary = client.get("/api/summary").json()
    assert {k: v for k, v in after_summary.items() if k != "generated_at"} == {
        k: v for k, v in summary.items() if k != "generated_at"
    }
