from src.hub import HubClient


class FakeResponse:
    def __init__(self):
        self.status_code = 200
        self.headers = {"Content-Type": "text/turtle; charset=UTF-8"}
        self.content = b"<https://example.test/dataset> a <http://www.w3.org/ns/dcat#Dataset> ."
        self.text = self.content.decode("utf-8")
        self.url = "https://data.europa.eu/api/hub/repo/resources/dataset-1"

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.calls = []
        self.response = FakeResponse()

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        return self.response


def test_fetch_dataset_rdf_uses_current_resources_endpoint():
    client = HubClient()
    session = FakeSession()
    client.session = session

    content, source_url = client.fetch_dataset_rdf("dataset-1")

    assert content == session.response.text
    assert source_url == session.response.url
    assert session.calls[0]["url"] == "https://data.europa.eu/api/hub/repo/resources/dataset-1"
    assert session.calls[0]["headers"]["Accept"].startswith("text/turtle")
    assert session.calls[0]["timeout"] == client.timeout


def test_fetch_dataset_rdf_quotes_path_segments():
    client = HubClient()
    session = FakeSession()
    client.session = session

    client.fetch_dataset_rdf("id/with:reserved")

    assert session.calls[0]["url"].endswith("/resources/id%2Fwith%3Areserved")
