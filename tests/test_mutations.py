"""Update and delete mutations, and the driver actions built on them.

Ownership is the attribution column. A writer can change or remove only the rows
it wrote; a row somebody else wrote is, from the outside, indistinguishable from
one that does not exist. The population driver learns which rows are its own from
the row id the site returns on insert, never from the database.
"""

from __future__ import annotations

import copy
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tools.compose_fastapi_sqlite_v1 import WRITER_HEADER, ComposeError, classify_spec, compose_app
from tools.population import Choreography, Population, PopulationDriver
from tools.population.driver import Actor, Behaviour, Cohort, ScriptStep

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"


def with_edit_routes(spec: dict) -> dict:
    """The example spec plus an edit and a delete route for replies."""
    spec = copy.deepcopy(spec)
    spec["routes"] += [
        {"id": "r_reply_edit", "path": "/reply/{reply_id}/edit", "method": "POST",
         "form": "f_reply", "mutation": "m_update_reply"},
        {"id": "r_reply_delete", "path": "/reply/{reply_id}/delete", "method": "POST",
         "form": "f_delete", "mutation": "m_delete_reply"},
    ]
    spec["forms"].append({"id": "f_delete", "fields": [{"name": "confirm", "type": "text", "required": False}]})
    spec["mutations"] += [
        {"id": "m_update_reply", "table": "replies", "op": "update", "from_form": "f_reply",
         "bind": {"id": "{reply_id}"}},
        {"id": "m_delete_reply", "table": "replies", "op": "delete", "from_form": "f_delete",
         "bind": {"id": "{reply_id}"}},
    ]
    return spec


@pytest.fixture
def site(tmp_path):
    work = tmp_path / "content"
    shutil.copytree(EXAMPLE / "content", work / "content")
    shutil.copytree(EXAMPLE / "index", work / "index")
    spec = with_edit_routes(json.loads((EXAMPLE / "spec" / "site.json").read_text()))
    assert classify_spec(spec).supported
    db = tmp_path / "episode.sqlite"
    shutil.copyfile(work / "content" / "seed.sqlite", db)
    composed = compose_app(spec, work, db)
    client = TestClient(composed.app, base_url=f"http://{composed.hostname}")
    client.__enter__()
    try:
        yield spec, client, db
    finally:
        client.__exit__(None, None, None)


def body_of(db: Path, row_id: int):
    return sqlite3.connect(db).execute("SELECT body FROM replies WHERE id = ?", (row_id,)).fetchone()


class TestOwnership:
    def test_insert_returns_the_row_id_to_the_writer(self, site):
        _, client, db = site
        r = client.post("/thread/3/reply", data={"body": "mine"}, headers={WRITER_HEADER: "u_1"}, follow_redirects=False)
        assert r.status_code == 303
        row_id = int(r.headers["x-row-id"])
        assert body_of(db, row_id) == ("mine",)

    def test_writer_can_edit_and_delete_its_own_row(self, site):
        _, client, db = site
        row_id = int(client.post("/thread/3/reply", data={"body": "v1"}, headers={WRITER_HEADER: "u_1"},
                                 follow_redirects=False).headers["x-row-id"])
        r = client.post(f"/reply/{row_id}/edit", data={"body": "v2"}, headers={WRITER_HEADER: "u_1"}, follow_redirects=False)
        assert r.status_code == 303
        assert body_of(db, row_id) == ("v2",)
        r = client.post(f"/reply/{row_id}/delete", data={}, headers={WRITER_HEADER: "u_1"}, follow_redirects=False)
        assert r.status_code == 303
        assert body_of(db, row_id) is None

    def test_another_writer_gets_not_found(self, site):
        _, client, db = site
        row_id = int(client.post("/thread/3/reply", data={"body": "v1"}, headers={WRITER_HEADER: "u_1"},
                                 follow_redirects=False).headers["x-row-id"])
        for path, data in ((f"/reply/{row_id}/edit", {"body": "hijack"}), (f"/reply/{row_id}/delete", {})):
            r = client.post(path, data=data, headers={WRITER_HEADER: "agent"}, follow_redirects=False)
            assert r.status_code == 404
        missing = client.post("/reply/999999/delete", data={}, headers={WRITER_HEADER: "agent"}, follow_redirects=False)
        assert missing.status_code == 404, "missing and not-owned must look the same"
        assert body_of(db, row_id) == ("v1",)

    def test_seed_rows_belong_to_nobody(self, site):
        _, client, db = site
        seed_id = sqlite3.connect(db).execute("SELECT id FROM replies WHERE writer IS NULL LIMIT 1").fetchone()[0]
        r = client.post(f"/reply/{seed_id}/delete", data={}, headers={WRITER_HEADER: "agent"}, follow_redirects=False)
        assert r.status_code == 404

    def test_unattributed_edit_is_refused(self, site):
        _, client, _ = site
        assert client.post("/reply/1/edit", data={"body": "x"}, follow_redirects=False).status_code == 400

    def test_update_without_a_bind_is_unsupported(self, site, tmp_path):
        spec, _, _ = site
        spec = copy.deepcopy(spec)
        del spec["mutations"][1]["bind"]
        assert "m_update_reply" in classify_spec(spec).unsupported
        shutil.copyfile(tmp_path / "content" / "content" / "seed.sqlite", tmp_path / "unbound.sqlite")
        with pytest.raises(ComposeError, match="needs a bind"):
            compose_app(spec, tmp_path / "content", tmp_path / "unbound.sqlite")


POOLS = {"cp_replies": [{"body": f"ambient reply {i}", "author": f"user{i}"} for i in range(5)],
         "cp_none": [{}]}


class TestDriverOwnActions:
    def driver(self, site, population=None, choreography=None, episode="e1"):
        spec, client, _ = site
        return PopulationDriver(client, spec, run_id="r1", episode_id=episode, site_id="site-000001",
                                population=population, choreography=choreography, pools=POOLS)

    def test_edit_own_and_delete_own_act_only_on_the_actors_rows(self, site):
        _, _, db = site
        choreography = Choreography(
            "c", "site-000001", "q",
            actors=(Actor("a_one", "u_00000001", script=(
                ScriptStep(0, "form_submit", "f_reply", "r_reply", "cp_replies", 0),
                ScriptStep(1, "edit_own", "f_reply", "r_reply_edit", "cp_replies", 3),
                ScriptStep(2, "delete_own", "f_delete", "r_reply_delete", "cp_none", 0),
            )),),
        )
        d = self.driver(site, choreography=choreography)
        assert d.tick(0) and len(d.owned("a_one", "replies")) == 1
        (row_id,) = d.owned("a_one", "replies")
        assert body_of(db, row_id) == ("ambient reply 0",)
        assert d.tick(1)
        assert body_of(db, row_id) == ("ambient reply 3",)
        assert d.tick(2)
        assert body_of(db, row_id) is None
        assert d.owned("a_one", "replies") == []
        assert d.counters.post_failures == 0

    def test_edit_own_with_nothing_owned_is_a_missing_target_not_a_forgery(self, site):
        _, _, db = site
        before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM replies").fetchone()[0]
        choreography = Choreography(
            "c", "site-000001", "q",
            actors=(Actor("a_new", "u_00000002", script=(
                ScriptStep(0, "delete_own", "f_delete", "r_reply_delete", "cp_none", 0),
            )),),
        )
        d = self.driver(site, choreography=choreography)
        assert d.tick(0) == []
        assert d.counters.targets_unavailable == 1
        assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM replies").fetchone()[0] == before

    def test_ambient_cohort_can_edit_its_own_earlier_replies(self, site):
        _, _, db = site
        population = Population("p", "site-000001", cohorts=(
            Cohort("c_regulars", 50, behaviours=(
                # Modest rates: the per-episode action cap is 50, and a flood of
                # replies would spend it before any edit got a turn.
                Behaviour("b_reply", "form_submit", "f_reply", "r_reply", "cp_replies", rate_per_hour=2.0),
                Behaviour("b_edit", "edit_own", "f_reply", "r_reply_edit", "cp_replies", rate_per_hour=2.0),
            )),
        ))
        d = self.driver(site, population=population)
        for step in range(12):
            d.tick(step)
        edits = [a for a in d.performed if a.route.endswith("/edit")]
        assert edits, "no edit happened; raise the rate or steps"
        assert d.counters.post_failures == 0, "an edit landed on a row the cohort did not write"
        writers = {r[0] for r in sqlite3.connect(db).execute("SELECT DISTINCT writer FROM replies WHERE writer IS NOT NULL")}
        assert writers == {"c_regulars"}

    def test_vote_is_a_form_submit(self, site):
        population = Population("p", "site-000001", cohorts=(
            Cohort("c", 50, behaviours=(Behaviour("b_vote", "vote", "f_reply", "r_reply", "cp_replies", rate_per_hour=60.0),)),
        ))
        d = self.driver(site, population=population)
        for step in range(3):
            d.tick(step)
        assert d.counters.actions_performed > 0 and d.counters.post_failures == 0
