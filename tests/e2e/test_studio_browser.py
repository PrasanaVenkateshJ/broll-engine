"""Optional browser smoke test of the Studio (needs Playwright + its Chromium; skipped otherwise).

Install it with:  .venv\\Scripts\\python -m pip install playwright ; .venv\\Scripts\\python -m playwright install chromium
It starts the real API in this process, opens the Studio, enters the token, fills the "Saving money" example,
clicks Generate and checks that the progress view appears. No worker runs, so the job just stays queued.
"""
import socket
import threading
import time

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed (optional test)")

from tests.conftest import TOKEN  # noqa: E402


@pytest.fixture
def live_server():
    import uvicorn

    from app.main import app

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            pytest.fail("the test API did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_studio_loads_fills_a_preset_and_shows_progress(live_server):
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # Playwright installed, but its browser was never downloaded
            pytest.skip(f"Chromium for Playwright is not available: {e}")
        page = browser.new_page()
        errors = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        page.goto(live_server + "/")
        page.wait_for_selector("#ready-pill.pill-ok")
        page.fill("#token", TOKEN)
        # The Studio's CSP forbids eval, so no wait_for_function: wait for the 2nd voice option instead.
        page.wait_for_selector("#voice option:nth-child(2)", state="attached")  # config loaded
        page.click("[data-preset=saving-money]")
        assert "Saving money" in page.input_value("#script")
        page.click("#generate")
        page.wait_for_selector("#job:not([hidden])")
        page.wait_for_selector("#stepper .step")
        assert page.text_content("#job-id").startswith("Job ")
        browser.close()
    assert errors == [], errors
