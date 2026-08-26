from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .checks import SiteCheck, normalize_client_name, normalize_site_name, normalize_site_url
from .fleet import Alert, FleetSite


class FleetOpsStore:
    def __init__(self, path: str | Path = "data/fleetops.sqlite3"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        # FastAPI runs synchronous handlers in a thread pool, so brief overlap
        # between dashboard reads and snapshot writes is expected. Give the
        # active writer time to finish instead of surfacing transient lock errors.
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        # SQLite declares foreign keys in the schema but does not enforce them
        # unless every connection opts in. Keep checks and snapshots tied to a
        # real site so orphan history cannot silently disappear from reports.
        con.execute("pragma foreign_keys = on")
        con.execute("pragma busy_timeout = 30000")
        return con

    def _init(self):
        with self._connect() as con:
            # WAL lets readers continue while a snapshot or care check is being
            # persisted. The setting is durable for this database and applies to
            # subsequent connections after initialization.
            con.execute("pragma journal_mode = wal")
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
            # Client-scoped history reads should enter through the small matching
            # site set instead of scanning every care check or fleet snapshot.
            # Include the join key so SQLite can resolve site IDs from the index.
            con.execute(
                "create index if not exists idx_sites_client_id "
                "on sites(client, id)"
            )
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
            # Latest and per-site history APIs filter by site_id and then read
            # newest IDs first. Install these indexes for both new and existing
            # databases so history growth does not turn those reads into scans.
            con.execute(
                "create index if not exists idx_care_checks_site_id_id "
                "on care_checks(site_id, id desc)"
            )
            con.execute(
                "create index if not exists idx_snapshots_site_id_id "
                "on snapshots(site_id, id desc)"
            )

    @staticmethod
    def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
        return any(row[1] == column for row in con.execute(f"pragma table_info({table})"))

    def upsert_site(self, name: str, url: str, client: str = "") -> int:
        name = normalize_site_name(name)
        client = normalize_client_name(client)
        url = normalize_site_url(url)
        with self._connect() as con:
            return self._upsert_site(con, name, url, client)

    @staticmethod
    def _upsert_site(con: sqlite3.Connection, name: str, url: str, client: str) -> int:
        """Upsert a normalized site using the caller's active transaction."""
        cur = con.execute(
            "insert or ignore into sites(name,url,client) values(?,?,?)",
            (name, url, client),
        )
        if cur.lastrowid:
            return int(cur.lastrowid)
        con.execute(
            "update sites set name=?, client=coalesce(nullif(?, ''), client) where url=?",
            (name, client, url),
        )
        return int(con.execute("select id from sites where url=?", (url,)).fetchone()["id"])

    def list_sites(self) -> list[dict]:
        with self._connect() as con:
            return [dict(r) for r in con.execute("select * from sites order by name")]

    def health_counts(self) -> dict[str, int]:
        """Return persistence counters after proving the database is writable.

        A successful read is insufficient for readiness because a read-only PVC
        mount would accept traffic but fail every care-check and snapshot write.
        Acquire a write transaction and execute a no-op update, then roll it back
        so the probe validates write authority without changing application data.
        """
        with self._connect() as con:
            # Ordinary writes may wait up to 30 seconds for brief contention, but
            # Kubernetes gives /ready two seconds. Bound this connection's wait so
            # the endpoint can return 503 instead of leaving a timed-out probe's
            # worker thread blocked long after kubelet has abandoned the request.
            con.execute("pragma busy_timeout = 1000")
            counts = {
                "sites": int(con.execute("select count(*) from sites").fetchone()[0]),
                "care_checks": int(con.execute("select count(*) from care_checks").fetchone()[0]),
                "fleet_snapshots": int(con.execute("select count(*) from snapshots").fetchone()[0]),
            }
            con.execute("begin immediate")
            try:
                con.execute("update sites set name=name where 0")
            finally:
                con.rollback()
            return counts

    def save_care_check(self, site_id: int, check: SiteCheck) -> int:
        with self._connect() as con:
            return self._insert_care_check(con, site_id, check)

    @staticmethod
    def _insert_care_check(
        con: sqlite3.Connection,
        site_id: int,
        check: SiteCheck,
    ) -> int:
        """Insert one care check using the caller's active transaction."""
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
        if cur.lastrowid is None:
            raise RuntimeError("Care check insert did not return a row ID.")
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

    def recent_care_checks(
        self,
        limit: int = 25,
        offset: int = 0,
        url: str | None = None,
        client: str | None = None,
    ) -> list[dict]:
        """Return a page of care checks, optionally scoped by site or client."""
        sql = """
        select s.name, s.url, s.client, c.* from care_checks c
        join sites s on s.id=c.site_id
        """
        params: list[object] = []
        filters = []
        if url is not None:
            filters.append("s.url=?")
            params.append(url)
        if client == "Unassigned":
            filters.append("trim(s.client) = ''")
        elif client is not None:
            filters.append("s.client=?")
            params.append(client)
        if filters:
            sql += " where " + " and ".join(filters)
        sql += " order by c.id desc limit ? offset ?"
        params.extend((limit, offset))
        with self._connect() as con:
            rows = []
            for r in con.execute(sql, params):
                d = dict(r)
                d["actions"] = json.loads(d.pop("actions_json"))
                d["security_headers"] = json.loads(d.pop("raw_json")).get("security_headers", {})
                rows.append(d)
            return rows

    def count_care_checks(self, url: str | None = None, client: str | None = None) -> int:
        """Return the care-check count, optionally scoped by site or client."""
        sql = "select count(*) from care_checks c join sites s on s.id=c.site_id"
        params: list[str] = []
        filters = []
        if url is not None:
            filters.append("s.url=?")
            params.append(url)
        if client == "Unassigned":
            filters.append("trim(s.client) = ''")
        elif client is not None:
            filters.append("s.client=?")
            params.append(client)
        if filters:
            sql += " where " + " and ".join(filters)
        with self._connect() as con:
            return int(con.execute(sql, params).fetchone()[0])

    def save_snapshot(self, site_id: int, site: FleetSite, score: int, alerts: list[Alert]) -> int:
        with self._connect() as con:
            return self._insert_snapshot(con, site_id, site, score, alerts)

    @staticmethod
    def _insert_snapshot(
        con: sqlite3.Connection,
        site_id: int,
        site: FleetSite,
        score: int,
        alerts: list[Alert],
    ) -> int:
        """Insert one fleet snapshot using the caller's active transaction."""
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
        if cur.lastrowid is None:
            raise RuntimeError("Fleet snapshot insert did not return a row ID.")
        return int(cur.lastrowid)

    def save_observation(
        self,
        name: str,
        url: str,
        client: str,
        site: FleetSite,
        score: int,
        alerts: list[Alert],
        check: SiteCheck,
    ) -> dict[str, int]:
        """Atomically persist a site and its paired fleet and care evidence.

        Combined capture endpoints derive both records from one observation. If
        either insert fails, retaining only its partner creates an artificial
        monitoring gap and can mislead operational reports. Keep the site upsert
        and both inserts in one SQLite transaction so they commit or roll back as
        a unit.
        """
        name = normalize_site_name(name)
        url = normalize_site_url(url)
        client = normalize_client_name(client)
        if normalize_site_name(site.name) != name or normalize_site_name(check.name) != name:
            raise ValueError("Paired observation names must match the tracked site.")
        if normalize_site_url(site.url) != url or normalize_site_url(check.url) != url:
            raise ValueError("Paired observation URLs must match the tracked site.")

        with self._connect() as con:
            site_id = self._upsert_site(con, name, url, client)
            snapshot_id = self._insert_snapshot(con, site_id, site, score, alerts)
            care_check_id = self._insert_care_check(con, site_id, check)
            return {
                "site_id": site_id,
                "snapshot_id": snapshot_id,
                "care_check_id": care_check_id,
            }

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

    def recent_snapshots(
        self,
        limit: int = 25,
        offset: int = 0,
        url: str | None = None,
        client: str | None = None,
    ) -> list[dict]:
        """Return a page of fleet snapshots, optionally scoped by site or client."""
        sql = """
        select s.name,s.url,s.client, sn.* from snapshots sn
        join sites s on s.id=sn.site_id
        """
        params: list[object] = []
        filters = []
        if url is not None:
            filters.append("s.url=?")
            params.append(url)
        if client == "Unassigned":
            filters.append("trim(s.client) = ''")
        elif client is not None:
            filters.append("s.client=?")
            params.append(client)
        if filters:
            sql += " where " + " and ".join(filters)
        sql += " order by sn.id desc limit ? offset ?"
        params.extend((limit, offset))
        with self._connect() as con:
            rows = []
            for r in con.execute(sql, params):
                d = dict(r)
                d["alerts"] = json.loads(d.pop("alerts_json"))
                rows.append(d)
            return rows

    def count_snapshots(self, url: str | None = None, client: str | None = None) -> int:
        """Return the snapshot count, optionally scoped by site or client."""
        sql = "select count(*) from snapshots sn join sites s on s.id=sn.site_id"
        params: list[str] = []
        filters = []
        if url is not None:
            filters.append("s.url=?")
            params.append(url)
        if client == "Unassigned":
            filters.append("trim(s.client) = ''")
        elif client is not None:
            filters.append("s.client=?")
            params.append(client)
        if filters:
            sql += " where " + " and ".join(filters)
        with self._connect() as con:
            return int(con.execute(sql, params).fetchone()[0])

    def recent_trend_snapshots(self, limit: int = 100) -> list[dict]:
        """Return a bounded history with at most two recent snapshots per site."""
        sql = """
        with ranked_snapshots as (
            select s.name,s.url,s.client, sn.*,
                   row_number() over (partition by sn.site_id order by sn.id desc) as site_snapshot_rank
            from snapshots sn
            join sites s on s.id=sn.site_id
        )
        select * from ranked_snapshots
        where site_snapshot_rank <= 2
        order by id desc
        limit ?
        """
        with self._connect() as con:
            rows = []
            for r in con.execute(sql, (limit,)):
                d = dict(r)
                d.pop("site_snapshot_rank", None)
                d["alerts"] = json.loads(d.pop("alerts_json"))
                rows.append(d)
            return rows

    def site_snapshots(self, url: str, limit: int = 25, offset: int = 0) -> list[dict]:
        """Return snapshots for a single site by URL, newest first."""
        return self.recent_snapshots(limit, offset, url=url)

    def count_site_snapshots(self, url: str) -> int:
        """Return the total number of snapshots persisted for one site URL."""
        return self.count_snapshots(url=url)

    def delete_site(self, url: str) -> bool:
        """Remove a site and all its care checks and snapshots.

        The ``sites`` table uses ``ON DELETE CASCADE`` is not declared in the
        schema (it relies on foreign keys with ``cascade`` in the constraint), so
        we delete child rows explicitly before removing the site record. Foreign
        key enforcement is enabled per-connection via ``pragma foreign_keys``,
        but to keep the delete self-contained and unambiguous we always remove
        children first.

        Returns ``True`` if a site was found and removed, ``False`` if no site
        with that URL exists.
        """
        with self._connect() as con:
            row = con.execute("select id from sites where url=?", (url,)).fetchone()
            if row is None:
                return False
            site_id = int(row["id"])
            con.execute("delete from care_checks where site_id=?", (site_id,))
            con.execute("delete from snapshots where site_id=?", (site_id,))
            con.execute("delete from sites where id=?", (site_id,))
            return True
