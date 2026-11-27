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
        self.datalists = []
        self._form = None
        self._label = None
        self._datalist = None

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
        elif tag == "datalist":
            self._datalist = {"attrs": attrs, "options": [], "option_attributes": []}
            self.datalists.append(self._datalist)
        elif tag == "option" and self._datalist is not None:
            self._datalist["options"].append(attrs.get("value"))
            self._datalist["option_attributes"].append(attrs)

    def handle_data(self, data):
        if self._label is not None:
            self._label["text"] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "label":
            self._label = None
        elif tag == "datalist":
            self._datalist = None


def dashboard_forms(client):
    response = client.get("/")
    assert response.status_code == 200
    parser = DashboardFormParser()
    parser.feed(response.text)
    return parser


@pytest.mark.parametrize("action", ["/care/manual-check", "/care/fetch-check", "/snapshot"])
@pytest.mark.parametrize("account", ["Existing Client", 'New Église & "Care" <ops>', ""])
def test_observation_forms_suggest_existing_accounts_without_restricting_submission(
    form_client, monkeypatch, action, account,
):
    from markupsafe import escape

    client, main = form_client
    special_account = 'Église & "Mission" <ops>'
    for index, label in enumerate(("Zulu Client", "Existing Client", "Existing Client", "", special_account)):
        main.store.upsert_site(f"Registered {index}", f"https://registry-{index}.example", label)
    before = (main.store.list_sites(), main.store.latest_dashboard(), main.store.latest_care_checks())
    response = client.get("/")
    assert response.status_code == 200
    parser = DashboardFormParser()
    parser.feed(response.text)
    assert len(parser.ids) == len(set(parser.ids))
    suggestions = next(item for item in parser.datalists
                       if item["attrs"]["id"] == "existing-client-accounts")
    expected = sorted({"Zulu Client", "Existing Client", "Unassigned", special_account}, key=str.casefold)
    assert suggestions["options"] == expected, "Registry-only accounts must remain discoverable."
    list_id = suggestions["attrs"]["id"]
    controls = [control for form in parser.forms for control in form["controls"]
                if control.get("id") in {"care-client", "snapshot-client"}]
    assert len(controls) == 2
    assert all(control.get("list") == list_id for control in controls)
    assert all("required" not in control and "pattern" not in control for control in controls)
    assert f'<option value="{escape(special_account)}">' in response.text
    assert f'<option value="{special_account}">' not in response.text
    assert (main.store.list_sites(), main.store.latest_dashboard(), main.store.latest_care_checks()) == before

    form_action = "/care/manual-check" if action == "/care/fetch-check" else action
    form = next(form for form in parser.forms if form["attrs"]["action"] == form_action)
    payload = {control["name"]: control.get("value", "") for control in form["controls"]}
    payload.update(name="Suggested Client Site", url="https://suggested-client.example", client=account)
    monkeypatch.setattr(main, "fetch_basic_site_check", lambda name, url, **kwargs: main.evaluate_site(
        name, url, 200, 250, 60, kwargs["wordpress_version"],
        kwargs["update_count"], kwargs["backup_age_hours"], {},
    ))
    saved = client.post(action, data=payload, follow_redirects=False)
    assert saved.status_code == 303
    site = next(site for site in main.store.list_sites() if site["url"] == payload["url"])
    assert site["client"] == account
    assert len(main.store.latest_dashboard()) == len(main.store.latest_care_checks()) == 1


def test_empty_registry_has_no_dangling_client_suggestions(form_client):
    client, main = form_client
    parser = dashboard_forms(client)
    assert parser.datalists == []
    controls = [control for form in parser.forms for control in form["controls"]
                if control.get("id") in {"care-client", "snapshot-client"}]
    assert len(controls) == 2
    assert all("list" not in control for control in controls)
    assert main.store.list_sites() == []


@pytest.mark.parametrize("action", ["/care/manual-check", "/care/fetch-check", "/snapshot"])
@pytest.mark.parametrize("registered_url", [False, True])
def test_observation_forms_suggest_registered_sites_without_restricting_new_urls(
    form_client, monkeypatch, action, registered_url,
):
    from markupsafe import escape

    client, main = form_client
    special_name = '<img src=x onerror=alert(1)> Église & "Care"'
    special_client = 'Mission & "Partners" <ops>'
    existing_url = "https://registered.example?care=1&mode=%22review%22"
    main.store.upsert_site(special_name, existing_url, special_client)
    main.store.upsert_site("Unassigned Site", "https://unassigned.example", "")
    # A repeated canonical registry identity must not duplicate suggestions.
    main.store.upsert_site(special_name, existing_url, special_client)
    sites = main.store.list_sites()
    before = (sites, main.store.latest_dashboard(), main.store.latest_care_checks())
    response = client.get("/")
    assert response.status_code == 200
    parser = DashboardFormParser()
    parser.feed(response.text)
    assert len(parser.ids) == len(set(parser.ids))
    lists = [item for item in parser.datalists if item["attrs"]["id"] == "existing-site-urls"]
    assert len(lists) == 1, "Both forms should share one registry-backed URL list."
    suggestions = lists[0]
    assert suggestions["options"] == [site["url"] for site in sites]
    assert len(suggestions["options"]) == len(set(suggestions["options"])) == 2
    assert [item["label"] for item in suggestions["option_attributes"]] == [
        f"{site['name']} · {site['client'] or 'Unassigned'}" for site in sites
    ]
    controls = [control for form in parser.forms for control in form["controls"]
                if control.get("id") in {"care-url", "snapshot-url"}]
    assert len(controls) == 2
    assert all(control.get("list") == "existing-site-urls" for control in controls)
    assert all("required" in control and control["maxlength"] == "2048" for control in controls)
    assert f'value="{escape(existing_url)}"' in response.text
    assert f'label="{escape(special_name)} · {escape(special_client)}"' in response.text
    assert special_name not in response.text
    assert (main.store.list_sites(), main.store.latest_dashboard(), main.store.latest_care_checks()) == before

    form_action = "/care/manual-check" if action == "/care/fetch-check" else action
    form = next(form for form in parser.forms if form["attrs"]["action"] == form_action)
    payload = {control["name"]: control.get("value", "") for control in form["controls"]}
    selected_url = existing_url if registered_url else "https://new-observation.example"
    payload.update(name="Observed Site", url=selected_url, client="Care Client")
    monkeypatch.setattr(main, "fetch_basic_site_check", lambda name, url, **kwargs: main.evaluate_site(
        name, url, 200, 250, 60, kwargs["wordpress_version"],
        kwargs["update_count"], kwargs["backup_age_hours"], {},
    ))
    saved = client.post(action, data=payload, follow_redirects=False)
    assert saved.status_code == 303
    assert saved.headers["location"] == "/"
    assert main.store.latest_dashboard()[0]["url"] == selected_url
    assert main.store.latest_care_checks()[0]["url"] == selected_url
    assert len(main.store.list_sites()) == (2 if registered_url else 3)


def test_empty_registry_has_no_dangling_site_suggestions(form_client):
    client, main = form_client
    parser = dashboard_forms(client)
    assert not any(item["attrs"]["id"] == "existing-site-urls" for item in parser.datalists)
    controls = [control for form in parser.forms for control in form["controls"]
                if control.get("id") in {"care-url", "snapshot-url"}]
    assert len(controls) == 2
    assert all("list" not in control for control in controls)
    assert main.store.health_counts() == {"sites": 0, "care_checks": 0, "fleet_snapshots": 0}


@pytest.mark.parametrize("action", ["/care/manual-check", "/care/fetch-check", "/snapshot"])
@pytest.mark.parametrize("entered_url, expected_url", [
    ("forms.example", "https://forms.example"),
    ("HTTP://FORMS.EXAMPLE:80/path", "http://forms.example/path"),
    ("https://église.example/été", "https://xn--glise-9ra.example/%C3%A9t%C3%A9"),
])
def test_url_controls_allow_server_normalization(form_client, monkeypatch, action, entered_url, expected_url):
    client, main = form_client
    parser = dashboard_forms(client)
    form_action = "/care/manual-check" if action == "/care/fetch-check" else action
    form = next(form for form in parser.forms if form["attrs"]["action"] == form_action)
    url_control = next(control for control in form["controls"] if control["name"] == "url")
    # An absolute-URL input or case-sensitive pattern blocks valid server input
    # before the shared normalizer can add HTTPS or canonicalize the address.
    assert url_control.get("type", "text") == "text"
    assert "pattern" not in url_control
    assert url_control.get("inputmode") == "url"
    assert "required" in url_control
    assert url_control["maxlength"] == "2048"
    help_id = url_control.get("aria-describedby")
    assert help_id == ("snapshot-url-help" if action == "/snapshot" else "care-url-help")
    assert parser.ids.count(help_id) == 1
    assert "Omit the scheme to use HTTPS." in client.get("/").text

    payload = {control["name"]: control.get("value", "") for control in form["controls"]}
    payload.update(name="Normalized URL", url=entered_url, client="Care Client")
    monkeypatch.setattr(main, "fetch_basic_site_check", lambda name, url, **kwargs: main.evaluate_site(
        name, url, 200, 250, 60, kwargs["wordpress_version"],
        kwargs["update_count"], kwargs["backup_age_hours"], {},
    ))
    response = client.post(action, data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert main.store.list_sites()[0]["url"] == expected_url
    assert main.store.latest_dashboard()[0]["url"] == expected_url
    assert main.store.latest_care_checks()[0]["url"] == expected_url
    assert main.store.health_counts() == {"sites": 1, "care_checks": 1, "fleet_snapshots": 1}


@pytest.mark.parametrize("action", ["/care/manual-check", "/care/fetch-check", "/snapshot"])
@pytest.mark.parametrize("entered_url", ["ftp://forms.example", "javascript:alert(1)", "https://bad_host.example"])
def test_text_url_controls_retain_server_safety_checks(form_client, monkeypatch, action, entered_url):
    from wp_fleet_ops import checks

    client, main = form_client
    form_action = "/care/manual-check" if action == "/care/fetch-check" else action
    form = next(form for form in dashboard_forms(client).forms if form["attrs"]["action"] == form_action)
    payload = {control["name"]: control.get("value", "") for control in form["controls"]}
    payload.update(name="Invalid URL", url=entered_url)

    def no_network(*_args, **_kwargs):
        pytest.fail("Rejected URLs must not reach network probes.")

    monkeypatch.setattr(checks.urllib.request, "urlopen", no_network)
    monkeypatch.setattr(checks, "ssl_days_remaining", no_network)
    response = client.post(action, data=payload, follow_redirects=False)
    assert response.status_code == 422
    assert response.json()["detail"] == "Site URL must be a valid HTTP or HTTPS URL."
    assert main.store.health_counts() == {"sites": 0, "care_checks": 0, "fleet_snapshots": 0}


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


@pytest.mark.parametrize("registered", [False, True])
def test_dashboard_forms_are_named_by_unique_visible_headings(form_client, registered):
    client, main = form_client
    if registered:
        main.store.upsert_site("Registered Site", "https://forms.example", "Care Client")
    before = (main.store.list_sites(), main.store.latest_dashboard(), main.store.latest_care_checks())
    response = client.get("/")
    assert response.status_code == 200
    parser = DashboardFormParser()
    parser.feed(response.text)
    expected = {
        "/care/manual-check": ("care-form-heading", "Care check", "post"),
        "/snapshot": ("snapshot-form-heading", "Fleet snapshot", "post"),
    }
    if registered:
        expected["/report"] = ("client-report-heading", "Client report", "get")
    assert len(parser.forms) == len(expected)
    assert len(parser.ids) == len(set(parser.ids))
    for form in parser.forms:
        attrs = form["attrs"]
        heading_id, heading, method = expected[attrs["action"]]
        assert attrs.get("aria-labelledby") == heading_id, "Each form needs an accessible name."
        assert parser.ids.count(heading_id) == 1, "A form name must resolve to exactly one heading."
        assert f'<h3 id="{heading_id}">{heading}</h3>' in response.text
        assert attrs["method"] == method
        assert "aria-hidden" not in attrs and "novalidate" not in attrs
    assert (main.store.list_sites(), main.store.latest_dashboard(), main.store.latest_care_checks()) == before


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
        assert fields["url"]["type"] == "text"
        assert fields["url"]["inputmode"] == "url"
        assert "pattern" not in fields["url"]
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


@pytest.mark.parametrize("action", ["/care/manual-check", "/care/fetch-check"])
def test_care_form_explains_operator_supplied_metadata(form_client, monkeypatch, action):
    client, main = form_client
    before = main.store.health_counts()
    response = client.get("/")
    assert response.status_code == 200
    parser = DashboardFormParser()
    parser.feed(response.text)
    form = next(form for form in parser.forms
                if form["attrs"]["action"] == "/care/manual-check")
    fields = {control["name"]: control for control in form["controls"]}
    help_id = "care-metadata-help"
    assert parser.ids.count(help_id) == 1
    assert (
        f'<p id="{help_id}">Live checks refresh HTTP status, latency, SSL, and security headers. '
        "They use the WP version, update count, and backup age you enter; "
        "verify these values before saving either kind of check.</p>"
    ) in response.text
    for name in ("wordpress_version", "update_count", "backup_age_hours"):
        assert help_id in fields[name].get("aria-describedby", "").split()
    assert main.store.health_counts() == before, "Viewing guidance must not create evidence."

    payload = {name: field.get("value", "") for name, field in fields.items()}
    payload.update(name="Entered Care Metadata", url="https://entered-metadata.example",
                   wordpress_version="6.6.2", update_count="4", backup_age_hours="48")
    fetched = []

    def fetch_check(name, url, *, wordpress_version, update_count, backup_age_hours):
        fetched.append((wordpress_version, update_count, backup_age_hours))
        return main.evaluate_site(
            name, url, 200, 250, 60, wordpress_version, update_count, backup_age_hours, {},
        )

    monkeypatch.setattr(main, "fetch_basic_site_check", fetch_check)
    saved = client.post(action, data=payload, follow_redirects=False)
    assert saved.status_code == 303
    assert saved.headers["location"] == "/"
    care = main.store.latest_care_checks()[0]
    snapshot = main.store.latest_dashboard()[0]
    assert (care["wordpress_version"], care["update_count"], care["backup_age_hours"]) == ("6.6.2", 4, 48)
    assert (snapshot["wp_updates"], snapshot["backup_age_hours"]) == (4, 48)
    assert fetched == ([("6.6.2", 4, 48)] if action == "/care/fetch-check" else [])
    assert main.store.health_counts() == {"sites": 1, "care_checks": 1, "fleet_snapshots": 1}


@pytest.mark.parametrize("action", ["/care/manual-check", "/care/fetch-check"])
@pytest.mark.parametrize("version", [None, "", "  6.6.2  "])
def test_care_form_does_not_invent_a_wordpress_version(form_client, monkeypatch, action, version):
    client, main = form_client
    form = next(form for form in dashboard_forms(client).forms
                if form["attrs"]["action"] == "/care/manual-check")
    fields = {control["name"]: control for control in form["controls"]}
    assert fields["wordpress_version"].get("value", "") == "unknown"
    payload = {name: field.get("value", "") for name, field in fields.items()}
    payload.update(name="Version Evidence", url="https://version-evidence.example", client="Care Client")
    if version is not None:
        payload["wordpress_version"] = version
    expected = "unknown" if version in (None, "") else "6.6.2"
    fetched_versions = []

    def fetch_check(name, url, *, wordpress_version, update_count, backup_age_hours):
        # Exercise the form-to-fetch handoff without contacting an external site.
        fetched_versions.append(wordpress_version)
        return main.evaluate_site(
            name, url, 200, 250, 60, wordpress_version, update_count, backup_age_hours, {},
        )

    monkeypatch.setattr(main, "fetch_basic_site_check", fetch_check)
    response = client.post(action, data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert main.store.latest_care_checks()[0]["wordpress_version"] == expected
    assert main.store.health_counts() == {"sites": 1, "care_checks": 1, "fleet_snapshots": 1}
    if action == "/care/fetch-check":
        assert fetched_versions == [payload["wordpress_version"] or "unknown"]
    else:
        assert fetched_versions == []


@pytest.mark.parametrize("zero_values", [False, True])
def test_snapshot_form_explains_manual_evidence_without_changing_submission(form_client, zero_values):
    client, main = form_client
    before = (main.store.list_sites(), main.store.latest_dashboard(), main.store.latest_care_checks())
    response = client.get("/")
    assert response.status_code == 200
    parser = DashboardFormParser()
    parser.feed(response.text)
    form = next(form for form in parser.forms if form["attrs"]["action"] == "/snapshot")
    fields = {control["name"]: control for control in form["controls"]}
    help_id = "snapshot-evidence-help"
    assert parser.ids.count(help_id) == 1
    assert (
        f'<p id="{help_id}">Saving a snapshot does not run site checks. '
        "All measurements and the uptime setting are entered by you; "
        "replace pre-filled values with verified observations before saving.</p>"
    ) in response.text
    observation_fields = {
        "uptime_ok", "ssl_days", "wp_updates", "backup_age_hours", "response_ms", "security_header_count",
    }
    for name in observation_fields:
        assert help_id in fields[name].get("aria-describedby", "").split()
    assert (main.store.list_sites(), main.store.latest_dashboard(), main.store.latest_care_checks()) == before

    payload = {name: field.get("value", "") for name, field in fields.items()}
    payload.update(name="Entered Snapshot Evidence", url="https://entered-snapshot.example")
    if zero_values:
        payload.update({name: "0" for name in observation_fields if name != "uptime_ok"})
        payload.pop("uptime_ok")
    saved = client.post(form["attrs"]["action"], data=payload, follow_redirects=False)
    assert saved.status_code == 303
    assert saved.headers["location"] == "/"
    snapshot = main.store.latest_dashboard()[0]
    for name in observation_fields - {"uptime_ok"}:
        assert snapshot[name] == int(payload[name])
    assert bool(snapshot["uptime_ok"]) is (not zero_values)
    assert main.store.health_counts() == {"sites": 1, "care_checks": 1, "fleet_snapshots": 1}


def test_snapshot_numeric_controls_require_explicit_values_without_rejecting_zero(form_client):
    client, main = form_client
    form = next(form for form in dashboard_forms(client).forms
                if form["attrs"]["action"] == "/snapshot")
    numeric_controls = [control for control in form["controls"] if control.get("type") == "number"]
    assert {control["name"] for control in numeric_controls} == {
        "ssl_days", "wp_updates", "backup_age_hours", "response_ms", "security_header_count",
    }
    for control in numeric_controls:
        assert "required" in control, f"Clearing {control['name']} must not silently save a default."
    assert "novalidate" not in form["attrs"]
    assert all("formnovalidate" not in button for button in form["buttons"])

    # Zero is observed data, not an empty field: derive and submit the actual form.
    payload = {control["name"]: control.get("value", "") for control in form["controls"]}
    payload.update(name="Explicit Zero", url="https://explicit-zero.example")
    payload.update({control["name"]: "0" for control in numeric_controls})
    response = client.post(form["attrs"]["action"], data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    saved = main.store.latest_dashboard()[0]
    assert all(saved[control["name"]] == 0 for control in numeric_controls)
    assert main.store.health_counts() == {"sites": 1, "care_checks": 1, "fleet_snapshots": 1}


@pytest.mark.parametrize("action", ["/care/manual-check", "/care/fetch-check"])
@pytest.mark.parametrize("boundary", [0, (1 << 63) - 1])
def test_care_metadata_requires_explicit_values_without_rejecting_boundaries(
    form_client, monkeypatch, action, boundary,
):
    client, main = form_client
    form = next(form for form in dashboard_forms(client).forms
                if form["attrs"]["action"] == "/care/manual-check")
    fields = {control["name"]: control for control in form["controls"]}
    for name in ("update_count", "backup_age_hours"):
        assert "required" in fields[name], f"Clearing {name} must not silently save a default."
    assert "novalidate" not in form["attrs"]
    assert all("formnovalidate" not in button for button in form["buttons"])
    # Live checks replace these measurements, so requiring them would block a
    # useful fetch just because an irrelevant manual-only field was cleared.
    for name in ("http_status", "latency_ms", "ssl_days_remaining"):
        assert "required" not in fields[name]

    payload = {name: field.get("value", "") for name, field in fields.items()}
    payload.update(name="Explicit Care Metadata", url="https://care-metadata.example",
                   update_count=str(boundary), backup_age_hours=str(boundary))
    if action == "/care/fetch-check":
        payload.update(http_status="", latency_ms="", ssl_days_remaining="")
    fetched_metadata = []

    def fetch_check(name, url, *, wordpress_version, update_count, backup_age_hours):
        fetched_metadata.append((update_count, backup_age_hours))
        return main.evaluate_site(
            name, url, 200, 250, 60, wordpress_version, update_count, backup_age_hours, {},
        )

    monkeypatch.setattr(main, "fetch_basic_site_check", fetch_check)
    response = client.post(action, data=payload, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    care = main.store.latest_care_checks()[0]
    snapshot = main.store.latest_dashboard()[0]
    assert (care["update_count"], care["backup_age_hours"]) == (boundary, boundary)
    assert (snapshot["wp_updates"], snapshot["backup_age_hours"]) == (boundary, boundary)
    assert fetched_metadata == ([(boundary, boundary)] if action == "/care/fetch-check" else [])
    assert main.store.health_counts() == {"sites": 1, "care_checks": 1, "fleet_snapshots": 1}


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
