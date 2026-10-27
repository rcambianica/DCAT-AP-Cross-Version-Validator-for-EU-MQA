from src.hub import HubClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.url = "https://data.europa.eu/api/hub/search/search"

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self.responses.pop(0)


def test_get_categories_uses_facet_and_italian_labels():
    client = HubClient()
    client.session = FakeSession([
        FakeResponse({
            "result": {
                "facets": [{
                    "id": "categories",
                    "items": [
                        {"id": "ENVI", "count": 12, "title": {"en": "Environment", "it": "Ambiente"}},
                        {"id": "ECON", "count": 8, "title": {"en": "Economy and finance"}},
                    ],
                }]
            }
        })
    ])

    categories = client.get_categories("dati-gov-it")

    assert categories == [
        {"id": "ENVI", "label": "Ambiente", "count": 12},
        {"id": "ECON", "label": "Economy and finance", "count": 8},
    ]
    call = client.session.calls[0]
    assert call["url"].endswith("/search")
    assert call["params"]["limit"] == 0
    assert '"catalog":["dati-gov-it"]' in call["params"]["facets"]


def test_get_dataset_ids_by_categories_uses_scroll_and_deduplicates():
    client = HubClient()
    client.session = FakeSession([
        FakeResponse({"result": {"results": [
            {"id": "dataset-1"}, {"id": "dataset-2"},
        ]}, "scrollId": "scroll-1"}),
        FakeResponse({"result": {"results": [
            {"id": "dataset-2"}, {"id": "dataset-3"},
        ]}, "scrollId": "scroll-2"}),
        FakeResponse({"result": {"results": []}}),
    ])

    ids = client.get_dataset_ids_by_categories(
        "dati-gov-it", ["ENVI", "ECON"], category_count=10
    )

    assert ids == ["dataset-1", "dataset-2", "dataset-3"]
    assert len(client.session.calls) == 3
    params = client.session.calls[0]["params"]
    assert params["page"] == 0
    assert params["limit"] == 1000
    assert params["scroll"] == "true"
    assert '"categories":["ENVI","ECON"]' in params["facets"]
    assert client.session.calls[1]["url"].endswith("/scroll")
    assert client.session.calls[1]["params"] == {"scrollId": "scroll-1"}


def test_get_dataset_ids_by_category_respects_limit():
    client = HubClient()
    client.session = FakeSession([
        FakeResponse({"result": {"results": [{"id": "dataset-1"}, {"id": "dataset-2"}]}}),
    ])

    ids = client.get_dataset_ids_by_category("dati-gov-it", "ENVI", limit=1, page_size=2)

    assert ids == ["dataset-1"]
    assert len(client.session.calls) == 1


def test_discovery_persists_category_id_and_preserves_it_on_unfiltered_discovery(tmp_path):
    from src.db import Database

    db = Database(tmp_path / "analysis.db")
    db.init_schema()

    inserted, total = db.add_discovered_datasets(
        ["dataset-2", "dataset-1", "dataset-1"], "dati-gov-it", category_id="ENVI"
    )
    assert (inserted, total) == (2, 2)
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT dataset_id, category_id FROM datasets ORDER BY dataset_id"
        ).fetchall()
    assert [(row["dataset_id"], row["category_id"]) for row in rows] == [
        ("dataset-1", "ENVI"), ("dataset-2", "ENVI")
    ]

    db.add_discovered_datasets(["dataset-1"], "dati-gov-it")
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT category_id FROM datasets WHERE dataset_id='dataset-1'"
        ).fetchone()
    assert row["category_id"] == "ENVI"


def test_existing_database_migration_adds_category_id(tmp_path):
    import sqlite3
    from src.db import Database

    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE datasets (
                dataset_id TEXT PRIMARY KEY, catalogue_id TEXT, source_url TEXT,
                rdf_path TEXT, sha256 TEXT, fetch_status TEXT NOT NULL DEFAULT 'pending',
                fetch_http_status INTEGER, fetch_error TEXT, position INTEGER,
                discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, fetched_at TIMESTAMP,
                validation_status TEXT NOT NULL DEFAULT 'pending', validation_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("INSERT INTO datasets(dataset_id, catalogue_id) VALUES ('old-dataset', 'dati-gov-it')")

    db = Database(db_path)
    db.init_schema()
    with db.get_connection() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(datasets)")}
        row = conn.execute("SELECT category_id FROM datasets WHERE dataset_id='old-dataset'").fetchone()
    assert "category_id" in columns
    assert row["category_id"] is None
