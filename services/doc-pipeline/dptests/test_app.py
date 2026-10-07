import time

from docpipe.llm import Fake
from docpipe.main import create_app
from docpipe.settings import Settings
from dp_helpers import BILL, make_pdf
from fastapi.testclient import TestClient
from test_pipeline import CLOUD, LOCAL, good


def wait(c, jid):
    for _ in range(100):
        j = c.get(f"/v1/jobs/{jid}").json()
        if j["state"] in ("succeeded", "failed"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_upload_job_result_and_url_fetch():
    pdf = make_pdf(BILL)

    async def fetch(url):
        assert url.startswith("http://store/")
        return pdf

    app = create_app(Settings(), llm=Fake({LOCAL: good(), CLOUD: good()}), fetch=fetch)
    with TestClient(app) as c:
        j = wait(
            c,
            c.post("/v1/parse/upload", files={"file": ("b.pdf", pdf, "application/pdf")}).json()[
                "job_id"
            ],
        )
        assert j["state"] == "succeeded" and j["result"]["doc_type"] == "final_bill"
        j2 = wait(
            c,
            c.post(
                "/v1/parse",
                json={"document_id": "d1", "case_id": "c1", "url": "http://store/obj?sig=1"},
            ).json()["job_id"],
        )
        assert j2["result"]["document_id"] == "d1" and len(j2["result"]["passes"]) == 2
        # each pass is exactly what hospital-api's /parse callback accepts
        keys = {"pass_no", "engine", "typed_json", "confidence", "entities", "duration_ms"}
        assert all(
            set(p) <= keys | {"engine_version", "masked_text_key", "raw_markdown_key"}
            for p in j2["result"]["passes"]
        )
        assert c.get("/v1/jobs/nope").status_code == 404
        assert (
            c.post(
                "/v1/parse/upload", files={"file": ("x.txt", b"hello", "text/plain")}
            ).status_code
            == 422
        )
        assert c.get("/v1/health").json()["status"] == "ok"


def test_bad_document_is_a_failed_job_with_a_code():
    async def fetch(url):
        return b"%PDF-1.4 not really"

    with TestClient(create_app(Settings(), llm=Fake({}), fetch=fetch)) as c:
        j = wait(c, c.post("/v1/parse", json={"url": "http://store/x"}).json()["job_id"])
        assert j["state"] == "failed" and j["error"] in ("corrupt_file", "ocr_failed")


def test_mask_endpoint_returns_no_raw_values():
    with TestClient(create_app(Settings(), llm=Fake({}))) as c:
        r = c.post("/v1/mask", json={"text": "Patient Name: Ravi Kumar call 9876543210"}).json()
        assert (
            "Ravi" not in r["masked_text"]
            and "9876543210" not in r["masked_text"]
            and {e["type"] for e in r["entities"]} == {"PERSON", "PHONE"}
        )
