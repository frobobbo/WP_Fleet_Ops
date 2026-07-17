from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .checks import SiteCheck, normalize_site_url
from .fleet import Alert, FleetSite


class FleetOpsStore:
    def __init__(self, path: str | Path = "data/fleetops.sqlite3"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        # SQLite declares foreign keys in the schema but does not enforce them
        # unless every connection opts in. Keep checks and snapshots tied to a
        # real site so orphan history cannot silently disappear from reports.
        con.execute("pragma foreign_keys = on")
        return con

    def _init(self):
        with self._connect() as con:
            con.execute(
                """
                create table if not exists sites(
                    id integer primary key autoincrement,
                    name text not null,
                    url text not null unique,
                    client text not null default '',
                    created_at text not null default current_timestamp
                )
                """
            )
            con.execute("alter table sites add column client text not null default ''") if not self._has_column(con, "sites", "client") else None
            con.execute(
                """
                create table if not exists care_checks(
                    id integer primary key autoincrement,
                    site_id integer not null references sites(id),
                    checked_at text not null,
                    status text not null,
                    score integer not null,
                    http_status integer not null,
                    latency_ms integer not null,
                    ssl_days_remaining integer not null,
                    wordpress_version text not null,
                    update_count integer not null,
                    backup_age_hours integer not null,
                    summary text not null,
                    actions_json text not null,
                    raw_json text not null
                )
                """
            )
            con.execute(
                """
                create table if not exists snapshots(
                    id integer primary key autoincrement,
                    site_id integer not null references sites(id),
                    captured_at text not null default current_timestamp,
                    score integer not null,
                    uptime_ok integer not null,
                    ssl_days integer not null,
                    wp_updates integer not null,
                    backup_age_hours integer not null,
                    response_ms integer not null,
                    security_header_count integer not null,
                    alerts_json text not null,
                    raw_json text not null
                )
                """
            )

    @staticmethod
    def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
        return any(row[1] == column for row in con.execute(f"pragma table_info({table})"))

    def upsert_site(self, name: str, url: str, client: str = "") -> int:
        name = name.strip()
        client = client.strip()
        if not name:
            raise ValueError("Site name must not be blank.")
        url = normalize_site_url(url)
        with self._connect() as con:
            cur = con.execute("insert or ignore into sites(name,url,client) values(?,?,?)", (name, url, client))
            if cur.lastrowid:
                return int(cur.lastrowid)
            con.execute("update sites set name=?, client=coalesce(nullif(?, ''), client) where url=?", (name, client, url))
            return int(con.execute("select id from sites where url=?", (url,)).fetchone()["id"])

    def list_sites(self) -> list[dict]:
        with self._connect() as con:
            return [dict(r) for r in con.execute("select * from sites order by name")]

    def health_counts(self) -> dict[str, int]:
        """Return minimal persistence counters for readiness checks."""
        with self._connect() as con:
            return {
                "sites": int(con.execute("select count(*) from sites").fetchone()[0]),
                "care_checks": int(con.execute("select count(*) from care_checks").fetchone()[0]),
                "fleet_snapshots": int(con.execute("select count(*) from snapshots").fetchone()[0]),
            }

    def save_care_check(self, site_id: int, check: SiteCheck) -> int:
        with self._connect() as con:
            cur = con.execute(
                """
                insert into care_checks(site_id,checked_at,status,score,http_status,latency_ms,ssl_days_remaining,
                wordpress_version,update_count,backup_age_hours,summary,actions_json,raw_json)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    site_id,
                    check.checked_at,
                    check.status,
                    check.score,
                    check.http_status,
                    check.latency_ms,
                    check.ssl_days_remaining,
                    check.wordpress_version,
                    check.update_count,
                    check.backup_age_hours,
                    check.summary,
                    json.dumps(check.actions),
                    json.dumps(check.to_dict()),
                ),
            )
            return int(cur.lastrowid)

    def latest_care_checks(self) -> list[dict]:
        sql = """
        select s.name, s.url, s.client, c.* from care_checks c
        join sites s on s.id=c.site_id
        where c.id in (select max(id) from care_checks group by site_id)
        order by s.name
        """
        with self._connect() as con:
            rows = []
            for r in con.execute(sql):
                d = dict(r)
                d["actions"] = json.loads(d.pop("actions_json"))
                d["security_headers"] = json.loads(d["raw_json"]).get("security_headers", {})
                rows.append(d)
            return rows

    def save_snapshot(self, site_id: int, site: FleetSite, score: int, alerts: list[Alert]) -> int:
        with self._connect() as con:
            cur = con.execute(
                """
                insert into snapshots(site_id,score,uptime_ok,ssl_days,wp_updates,backup_age_hours,response_ms,
                security_header_count,alerts_json,raw_json) values(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    site_id,
                    score,
                    int(site.uptime_ok),
                    site.ssl_days,
                    site.wp_updates,
                    site.backup_age_hours,
                    site.response_ms,
                    site.security_header_count,
                    json.dumps([a.__dict__ for a in alerts]),
                    json.dumps(site.to_dict()),
                ),
            )
            return int(cur.lastrowid)

    def latest_dashboard(self) -> list[dict]:
        sql = """
        select s.name,s.url,s.client, sn.* from snapshots sn
        join sites s on s.id=sn.site_id
        where sn.id in (select max(id) from snapshots group by site_id)
        order by sn.score asc, s.name
        """
        with self._connect() as con:
            rows = []
            for r in con.execute(sql):
                d = dict(r)
                d["alerts"] = json.loads(d.pop("alerts_json"))
                rows.append(d)
            return rows

    def recent_snapshots(self, limit: int = 25) -> list[dict]:
        """Return recent fleet snapshots across all sites, newest first."""
        sql = """
        select s.name,s.url,s.client, sn.* from snapshots sn
        join sites s on s.id=sn.site_id
        order by sn.id desc
        limit ?
        """
        with self._connect() as con:
            rows = []
            for r in con.execute(sql, (limit,)):
                d = dict(r)
                d["alerts"] = json.loads(d.pop("alerts_json"))
                rows.append(d)
            return rows
