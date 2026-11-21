"""Observation controls need unambiguous labels without changing form behavior."""
import importlib
import warnings
from html.parser import HTMLParser

import pytest


@pytest.fixture
def form_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WP_FLEET_OPS_DB", str(tmp_path / "forms.sqlite3"))
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated.*")
        from fastapi.testclient import TestClient
    import wp_fleet_ops.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client, main


class DashboardFormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.forms = []
        self.labels = []
        self._form = None
        self._label = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "form":
            self._form = {"attrs": attrs, "controls": [], "buttons": []}
            self.forms.append(self._form)
        elif tag in {"input", "select", "textarea"} and self._form is not None:
            self._form["controls"].append(attrs)
        elif tag == "button" and self._form is not None:
            self._form["buttons"].append(attrs)
        elif tag == "label":
            self._label = {"for": attrs.get("for"), "text": ""}
            self.labels.append(self._label)

    def handle_data(self, data):
        if self._label is not None:
            self._label["text"] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "label":
            self._label = None


def dashboard_forms(client):
    response = client.get("/")
    assert response.status_code == 200
    parser = DashboardFormParser()
    parser.feed(response.text)
    return parser


@pytest.mark.parametrize("registered", [False, True])
def test_dashboard_controls_have_unique_ids_and_explicit_visible_labels(form_client, registered):
    client, main = form_client
    if registered:
        main.store.upsert_site("Example", "https://forms.example", "Care Client")
    parser = dashboard_forms(client)
    assert len(parser.ids) == len(set(parser.ids)), "Dashboard IDs must be unique across forms."
    controls = [control for form in parser.forms for control in form["controls"]]
    assert len(controls) == (19 if registered else 18)
    for control in controls:
        identifier = control.get("id")
        assert identifier, f"{control['name']} is missing a label target ID."
        labels = [label for label in parser.labels if label["for"] == identifier]
        assert len(labels) == 1, f"{identifier} needs exactly one explicit label."
        assert labels[0]["text"].strip(), f"{identifier} needs visible label text."


@pytest.mark.parametrize("action", ["/care/manual-check", "/snapshot"])
def test_labeled_observation_forms_preserve_default_submission_contract(form_client, action):
    client, main = form_client
    parser = dashboard_forms(client)
    form = next(form for form in parser.forms if form["attrs"]["action"] == action)
    assert form["attrs"]["method"] == "post"
    fields = {control["name"]: control for control in form["controls"]}
    expected_names = (
        {"name", "url", "client", "http_status", "latency_ms", "ssl_days_remaining",
         "wordpress_version", "update_count", "backup_age_hours"}
        if action == "/care/manual-check" else
        {"name", "url", "client", "uptime_ok", "ssl_days", "wp_updates",
         "backup_age_hours", "response_ms", "security_header_count"}
    )
    assert set(fields) == expected_names
    for name, limit in (("name", "200"), ("url", "2048"), ("client", "200")):
        assert fields[name]["maxlength"] == limit
    assert "required" in fields["name"] and "required" in fields["url"]
    if action == "/care/manual-check":
        assert fields["http_status"]["min"] == "0"
        assert fields["http_status"]["max"] == "599"
        assert fields["wordpress_version"]["maxlength"] == "100"
        assert any(button.get("formaction") == "/care/fetch-check"
                   and button.get("formmethod") == "post" for button in form["buttons"])
    else:
        assert fields["url"]["pattern"] == "https?://.*"
        assert fields["uptime_ok"]["type"] == "checkbox"
        assert "checked" in fields["uptime_ok"]
        assert fields["security_header_count"]["max"] == "3"

    # Submit the rendered default values, not a parallel hand-written payload.
    payload = {name: field.get("value", "") for name, field in fields.items()}
    payload.update(name="Accessible Site", url="https://forms.example", client="Care Client")
    response = client.post(action, data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert len(main.store.latest_dashboard()) == len(main.store.latest_care_checks()) == 1
    assert main.store.list_sites()[0]["name"] == "Accessible Site"


@pytest.mark.parametrize("action", ["/care/manual-check", "/snapshot"])
def test_dashboard_numeric_ranges_match_server_validation(form_client, action):
    client, main = form_client
    form = next(form for form in dashboard_forms(client).forms if form["attrs"]["action"] == action)
    numeric_controls = [control for control in form["controls"] if control.get("type") == "number"]
    assert len(numeric_controls) == 5
    for control in numeric_controls:
        maximum = {"http_status": 599, "security_header_count": 3}.get(
            control["name"], main.SQLITE_INTEGER_MAX,
        )
        assert control.get("min") == "0", control["name"]
        assert control.get("max") == str(maximum), control["name"]


@pytest.mark.parametrize("action", ["/care/manual-check", "/snapshot"])
@pytest.mark.parametrize("boundary", [0, (1 << 63) - 1])
def test_dashboard_telemetry_boundaries_remain_accepted(form_client, action, boundary):
    client, main = form_client
    form = next(form for form in dashboard_forms(client).forms if form["attrs"]["action"] == action)
    payload = {control["name"]: control.get("value", "") for control in form["controls"]}
    fields = (
        ("latency_ms", "ssl_days_remaining", "update_count", "backup_age_hours")
        if action == "/care/manual-check" else
        ("ssl_days", "wp_updates", "backup_age_hours", "response_ms")
    )
    payload.update(name="Boundary Site", url="https://boundary.example", client="Care Client")
    payload.update(dict.fromkeys(fields, str(boundary)))
    response = client.post(action, data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    saved = main.store.latest_care_checks()[0] if action == "/care/manual-check" else main.store.latest_dashboard()[0]
    assert {field: saved[field] for field in fields} == dict.fromkeys(fields, boundary)
    assert main.store.health_counts() == {"sites": 1, "care_checks": 1, "fleet_snapshots": 1}
