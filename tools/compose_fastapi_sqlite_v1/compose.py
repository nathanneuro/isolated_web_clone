"""Build an ASGI app from a site spec and a directory of decrypted content.

Every SQL statement here is assembled from spec-declared identifiers only. Those are
constrained by site.schema.json to ^[a-z][a-z0-9_]{0,62}$, so interpolating them is
safe; every *value* is bound as a parameter. The distinction is the whole reason the
spec schema refuses literals in `where` and `bind` maps and accepts only `{param}`
references: it means a value can never arrive by way of the structure.

The app serves only its own hostname and /static/. It makes no outbound request of
any kind, which is what `no_external_requests` verifies.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import DictLoader, Environment, select_autoescape

TOKEN = re.compile(r"[a-z0-9']+")
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
PARAM_REF = re.compile(r"^\{([a-z][a-z0-9_]{0,62})\}$")


# Write attribution (synthetic-population-spec §6). Every table a mutation can write
# gets this column, added by the composer so no reconstruction has to remember it.
# Every POST must name its writer in this header or it is refused: the agent's own
# path (the env broker) sends "agent", drivers send their actor id, go-live sends
# "golive". Seed rows are NULL. The scorer credits only "agent", so a write that
# arrives with no attribution is a loud 400 rather than a silent point.
WRITER_COLUMN = "writer"
WRITER_HEADER = "x-writer"
WRITER_VALUE = re.compile(r"^[a-z][a-z0-9_\-]{0,62}$")


class ComposeError(Exception):
    """The spec asked for a pattern this generator does not support.

    Raised, never worked around. The worker's contract is to fail closed on
    UNSUPPORTED rather than improvise, and improvising here would be the same bug
    one layer down.
    """


@dataclass
class ComposedSite:
    app: FastAPI
    db_path: Path
    hostname: str


def _check_identifier(value: str, what: str) -> str:
    """Belt and braces. The schema already guarantees this; SQL interpolation is
    where being wrong would matter most, so it is asserted again at the point of use."""
    if not IDENTIFIER.match(value):
        raise ComposeError(f"unsafe {what}: {value!r}")
    return value


def _load_templates(spec: dict, content_dir: Path) -> Environment:
    sources = {}
    for template in spec.get("templates", []):
        blob = content_dir / template["blob_ref"]
        if not blob.is_file():
            raise ComposeError(f"template blob missing: {template['blob_ref']}")
        sources[template["id"]] = blob.read_text()
    return Environment(
        loader=DictLoader(sources),
        autoescape=select_autoescape(default=True, default_for_string=True),
    )


def _build_query(query: dict) -> tuple[str, list[str]]:
    """Compile a spec query descriptor into SQL plus its ordered parameter names."""
    table = _check_identifier(query["table"], "table")
    sql = f"SELECT * FROM {table}"
    params: list[str] = []

    for column, reference in (query.get("where") or {}).items():
        _check_identifier(column, "column")
        match = PARAM_REF.match(reference)
        if not match:
            # The schema forbids this, so reaching here means the schema and the
            # generator have drifted apart. Say so rather than embedding a literal.
            raise ComposeError(f"where value is not a param reference: {reference!r}")
        sql += f" {'AND' if params else 'WHERE'} {column} = ?"
        params.append(match.group(1))

    if order_by := query.get("order_by"):
        column, direction = order_by.split()
        _check_identifier(column, "order column")
        sql += f" ORDER BY {column} {'DESC' if direction == 'desc' else 'ASC'}"
    if limit := query.get("limit"):
        sql += f" LIMIT {int(limit)}"
    return sql, params


def _run_query(db: sqlite3.Connection, query: dict, path_params: dict) -> list[dict]:
    sql, names = _build_query(query)
    missing = [name for name in names if name not in path_params]
    if missing:
        raise ComposeError(f"query {query['id']} needs path params {missing}")
    rows = db.execute(sql, [path_params[name] for name in names]).fetchall()
    return [dict(row) for row in rows]


class Bm25Shard:
    """Reads the shard bundle-build shipped. Search is served, never rebuilt inside."""

    def __init__(self, path: Path) -> None:
        data = json.loads(path.read_text())
        if data.get("format") != "bm25-shard-v1":
            raise ComposeError(f"unsupported shard format: {data.get('format')}")
        self.postings = data["postings"]
        self.titles = data["titles"]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        scores: dict[str, float] = {}
        for term in TOKEN.findall(query.lower()):
            for doc_id, score in self.postings.get(term, {}).items():
                scores[doc_id] = scores.get(doc_id, 0.0) + score
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], int(kv[0])))[:limit]
        return [
            {"id": int(doc_id), "title": self.titles[doc_id], "score": round(score, 4)}
            for doc_id, score in ranked
        ]


def compose_app(spec: dict, content_dir: Path, db_path: Path) -> ComposedSite:
    """Compose the site. Raises ComposeError on anything unsupported."""
    if spec.get("framework") != "fastapi-sqlite-v1":
        raise ComposeError(f"this generator builds fastapi-sqlite-v1, not {spec.get('framework')}")

    content_dir, db_path = Path(content_dir), Path(db_path)
    env = _load_templates(spec, content_dir)
    queries = {q["id"]: q for q in spec.get("queries", [])}
    forms = {f["id"]: f for f in spec.get("forms", [])}
    mutations = {m["id"]: m for m in spec.get("mutations", [])}
    searches = {s["id"]: s for s in spec.get("search", [])}
    shards = {
        s["id"]: Bm25Shard(content_dir / s["shard_refs"][0]) for s in spec.get("search", [])
    }

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.site_id = spec["site_id"]
    app.state.external_requests = 0  # nothing here makes one; asserted by the suite

    def connect() -> sqlite3.Connection:
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    assets = {a["path"]: content_dir / a["blob_ref"] for a in spec.get("assets", [])}
    _ensure_writer_column(connect, mutations)

    @app.get("/static/{asset:path}")
    def serve_asset(asset: str) -> Response:
        source = assets.get(f"/static/{asset}")
        if source is None or not source.is_file():
            return Response(status_code=404)
        media = "text/css" if asset.endswith(".css") else "application/octet-stream"
        return Response(source.read_bytes(), media_type=media)

    for route in spec["routes"]:
        _register_route(
            app, route, spec, env, queries, forms, mutations, searches, shards, connect
        )

    return ComposedSite(app=app, db_path=db_path, hostname=spec["hostname"])


def _ensure_writer_column(connect, mutations: dict) -> None:
    """Add the attribution column to every table a mutation names, if absent."""
    db = connect()
    try:
        for mutation in mutations.values():
            table = _check_identifier(mutation["table"], "table")
            columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            if not columns:
                raise ComposeError(f"mutation {mutation['id']} names missing table {table}")
            if WRITER_COLUMN not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {WRITER_COLUMN} TEXT")
        db.commit()
    finally:
        db.close()


def _register_route(
    app, route, spec, env, queries, forms, mutations, searches, shards, connect
) -> None:
    path, route_id = route["path"], route["id"]

    if route["method"] == "GET":
        template_id = route.get("template")
        if template_id is None:
            raise ComposeError(f"GET route {route_id} has no template")

        async def handler(request: Request, _route=route, _template=template_id):
            context = {
                "site_name": spec["site_id"],
                "query": request.query_params.get("q", ""),
            }
            path_params = dict(request.path_params)
            db = connect()
            try:
                for query_id in _route.get("queries", []):
                    rows = _run_query(db, queries[query_id], path_params)
                    where = queries[query_id].get("where")
                    # A single-row lookup keyed on a path param renders as one object;
                    # a listing renders as a sequence. The template knows which.
                    context[query_id] = (rows[0] if rows else None) if where and "id" in where else rows
            finally:
                db.close()

            if search_id := _route.get("search"):
                if search_id not in searches:
                    raise ComposeError(f"route {_route['id']} references unknown search")
                context[search_id] = shards[search_id].search(context["query"])

            if any(value is None for key, value in context.items() if key.startswith("q_")):
                return HTMLResponse("not found", status_code=404)
            return HTMLResponse(env.get_template(_template).render(**context))

        app.add_api_route(path, handler, methods=["GET"], name=route_id)
        return

    mutation_id = route.get("mutation")
    if mutation_id is None:
        raise ComposeError(f"POST route {route_id} has no mutation")
    mutation = mutations[mutation_id]
    op = mutation["op"]
    if op not in ("insert", "update", "delete"):
        raise ComposeError(f"unsupported mutation op: {op}")
    form = forms[mutation["from_form"]]
    table = _check_identifier(mutation["table"], "table")
    field_names = [_check_identifier(f["name"], "form field") for f in form["fields"]]
    bindings = {
        _check_identifier(column, "bind column"): PARAM_REF.match(ref).group(1)
        for column, ref in (mutation.get("bind") or {}).items()
    }
    if op in ("update", "delete") and not bindings:
        # A row to change must be named by the path. An unbound update is a
        # whole-table write, which no site interface offers.
        raise ComposeError(f"mutation {mutation_id}: {op} needs a bind")

    def _read_fields(body, *, required: bool) -> tuple[list[str], list[str]] | HTMLResponse:
        columns, values = [], []
        for field in form["fields"]:
            value = body.get(field["name"])
            if required and field.get("required") and not value:
                return HTMLResponse("missing required field", status_code=400)
            if value is not None:
                columns.append(field["name"])
                values.append(value)
        return columns, values

    async def post_handler(request: Request, _route=route):
        writer = request.headers.get(WRITER_HEADER, "")
        if not WRITER_VALUE.match(writer):
            return HTMLResponse("missing writer attribution", status_code=400)
        body = await request.form()
        parent = request.url.path.rsplit("/", 1)[0] or "/"
        db = connect()
        try:
            if op == "insert":
                read = _read_fields(body, required=True)
                if isinstance(read, HTMLResponse):
                    return read
                columns, values = [WRITER_COLUMN, *read[0]], [writer, *read[1]]
                for column, param in bindings.items():
                    columns.append(column)
                    values.append(request.path_params[param])
                columns.append("created_at")
                values.append(__import__("datetime").datetime.now().isoformat(" ", "seconds"))
                placeholders = ",".join("?" for _ in columns)
                cursor = db.execute(
                    f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})", values
                )
                db.commit()
                # The new row's id, for the writer only. The env broker returns a
                # PageView and never a header, so the agent does not see this; the
                # population driver uses it to know which rows are its own.
                return RedirectResponse(parent, status_code=303, headers={"x-row-id": str(cursor.lastrowid)})

            # update and delete: the path names the row, and the writer must own it.
            # Ownership is the attribution column; there is no other identity.
            where = " AND ".join([f"{column} = ?" for column in bindings] + [f"{WRITER_COLUMN} = ?"])
            keys = [request.path_params[param] for param in bindings.values()] + [writer]
            if op == "update":
                read = _read_fields(body, required=False)
                if isinstance(read, HTMLResponse):
                    return read
                if not read[0]:
                    return HTMLResponse("nothing to update", status_code=400)
                assignments = ", ".join(f"{column} = ?" for column in read[0])
                cursor = db.execute(f"UPDATE {table} SET {assignments} WHERE {where}", [*read[1], *keys])
            else:
                cursor = db.execute(f"DELETE FROM {table} WHERE {where}", keys)
            db.commit()
            if cursor.rowcount == 0:
                # Missing, or somebody else's. The two are indistinguishable on
                # purpose: a 404 does not confirm that a row exists.
                return HTMLResponse("not found", status_code=404)
            return RedirectResponse(parent.rsplit("/", 1)[0] or "/", status_code=303)
        finally:
            db.close()

    app.add_api_route(path, post_handler, methods=["POST"], name=route_id)
