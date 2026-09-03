from __future__ import annotations

import http.client
import io
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

import pytest

from ins_posts.webui import server as ui

TOKEN = "test-startup-token"


def job_payload(**overrides):
    value = {
        "mode": "target",
        "target": "instagram",
        "include": ["posts", "reels"],
        "max_posts": 25,
        "authentication": "firefox",
        "download_media": True,
        "request_delay": "6-12",
        "filters": {
            "since": "2026-01-01",
            "until": "2026-08-31",
            "min_likes": 10,
            "max_likes": 5000,
            "keywords": ["launch", "上海"],
            "keyword_mode": "all",
            "hashtags": ["#design", "travel"],
            "hashtag_mode": "any",
            "media_type": "video",
            "max_results": 10,
        },
    }
    value.update(overrides)
    return value


def random_job_payload(**overrides):
    value = {
        "mode": "random",
        "discovery": {"breadth": "standard", "result_count": 12},
        "authentication": "anonymous",
        "download_media": False,
        "request_delay": "6-12",
        "filters": {
            "since": None,
            "until": None,
            "min_likes": None,
            "max_likes": None,
            "keywords": ["city"],
            "keyword_mode": "any",
            "hashtags": [],
            "hashtag_mode": "any",
            "media_type": "all",
        },
    }
    value.update(overrides)
    return value


class FakeProcess:
    calls: ClassVar[list] = []

    def __init__(self, command, **kwargs):
        type(self).calls.append((command, kwargs))
        output = Path(command[command.index("--output") + 1])
        post = {
            "post_id": "1",
            "shortcode": "ABC",
            "post_url": "https://www.instagram.com/p/ABC/",
            "username": "instagram",
            "published_at": "2026-08-30T12:00:00Z",
            "caption": "<img src=x onerror=alert(1)> launch 上海",
            "hashtags": ["#design"],
            "like_count": 120,
            "media_count": 1,
            "media": [{"media_type": "video"}],
        }
        (output / "current.jsonl").write_text(
            json.dumps(post, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (output / "run.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "scanned_this_run": 3,
                    "matched_this_run": 1,
                    "new_posts": 1,
                    "stored_posts": 1,
                    "errors": [],
                }
            ),
            encoding="utf-8",
        )
        self.stdout = io.StringIO("正在采集\n元数据已保存，开始下载媒体……\n完成\n")

    def wait(self):
        return 0


@contextmanager
def running_server(tmp_path, *, process_factory=FakeProcess, opener=None):
    opened = []
    directory_opener = opener or (lambda path: opened.append(path))
    server = ui.create_server(
        tmp_path / "data" / "ui-jobs",
        token=TOKEN,
        popen_factory=process_factory,
        directory_opener=directory_opener,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, opened
    finally:
        server.shutdown()
        server.server_close()
        server.app.close()
        thread.join(timeout=2)


def request(server, method, path, *, payload=None, token=True, origin=None, host=None):
    connection = http.client.HTTPConnection(
        ui.LOOPBACK_HOST, server.server_address[1], timeout=3
    )
    headers = {"Host": host or server.expected_host}
    if token:
        headers["X-UI-Token"] = TOKEN
    if origin is not None:
        headers["Origin"] = origin
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    content = response.read()
    response_headers = {name.lower(): value for name, value in response.getheaders()}
    result = {
        "status": response.status,
        "headers": response_headers,
        "json": (
            json.loads(content)
            if content and "application/json" in response_headers.get("content-type", "")
            else None
        ),
        "body": content,
    }
    connection.close()
    return result


def wait_for_terminal(server, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = request(server, "GET", f"/api/jobs/{job_id}")
        if response["json"]["status"] in ui.TERMINAL_STATUSES:
            return response
        time.sleep(0.01)
    pytest.fail("fake UI job did not finish")


def test_static_assets_have_strict_security_headers(tmp_path):
    with running_server(tmp_path) as (server, _):
        response = request(server, "GET", "/", token=False)

    assert response["status"] == 200
    assert "frame-ancestors 'none'" in response["headers"]["content-security-policy"]
    assert response["headers"]["referrer-policy"] == "no-referrer"
    assert response["headers"]["x-frame-options"] == "DENY"
    assert response["headers"]["x-content-type-options"] == "nosniff"
    assert response["headers"]["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response["headers"]


def test_api_requires_token_and_rejects_wrong_host(tmp_path):
    with running_server(tmp_path) as (server, _):
        missing_token = request(server, "GET", "/api/health", token=False)
        wrong_host = request(
            server, "GET", "/api/health", host="attacker.invalid", token=True
        )
        healthy = request(server, "GET", "/api/health")

    assert missing_token["status"] == 403
    assert wrong_host["status"] == 421
    assert healthy["status"] == 200
    assert healthy["json"]["single_task"] is True
    assert "firefox" in healthy["json"]["browsers"]
    assert "access-control-allow-origin" not in healthy["headers"]


def test_post_requires_exact_origin(tmp_path):
    with running_server(tmp_path) as (server, _):
        missing = request(server, "POST", "/api/jobs", payload=job_payload())
        wrong = request(
            server,
            "POST",
            "/api/jobs",
            payload=job_payload(),
            origin="http://evil.invalid",
        )

    assert missing["status"] == 403
    assert wrong["status"] == 403


def test_job_uses_safe_cli_argv_and_reads_current_results(tmp_path):
    FakeProcess.calls.clear()
    with running_server(tmp_path) as (server, opened):
        created = request(
            server,
            "POST",
            "/api/jobs",
            payload=job_payload(),
            origin=server.origin,
        )
        assert created["status"] == 202
        job_id = created["json"]["id"]
        finished = wait_for_terminal(server, job_id)
        opened_response = request(
            server,
            "POST",
            f"/api/jobs/{job_id}/open-output",
            origin=server.origin,
        )

    assert finished["json"]["status"] == "succeeded"
    assert finished["json"]["summary"]["matched_this_run"] == 1
    assert finished["json"]["results"]["total"] == 1
    assert finished["json"]["results"]["items"][0]["caption"].startswith("<img")
    assert opened_response["status"] == 200
    assert opened and opened[0].parent == (tmp_path / "data" / "ui-jobs").resolve()

    command, kwargs = FakeProcess.calls[0]
    assert command[:3] == [ui.sys.executable, "-m", "ins_posts"]
    assert kwargs["shell"] is False
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert kwargs["env"]["PYTHONUTF8"] == "1"
    assert "--cookies-from-browser" in command
    assert "--cookies" not in command
    assert command[command.index("--include") + 1] == "posts,reels"
    assert command[command.index("--keywords") + 1] == "launch,上海"
    assert command[command.index("--hashtags") + 1] == "#design,travel"
    assert command[command.index("--media-type") + 1] == "video"
    output = Path(command[command.index("--output") + 1]).resolve()
    assert output.parent == (tmp_path / "data" / "ui-jobs").resolve()
    assert output.name == job_id


def test_random_job_needs_no_target_and_builds_controlled_cli(tmp_path):
    app = ui.UIApp(tmp_path / "jobs", TOKEN, popen_factory=FakeProcess)
    config = ui.validate_job_request(random_job_payload())
    output = app.output_root / ("d" * 32)
    output.mkdir()
    job = ui.Job(id="d" * 32, config=config, output_dir=output)
    try:
        command = app._build_command(job)
    finally:
        app.close()

    assert config["mode"] == "random"
    assert config["target"] is None
    assert config["random_sources"] == 4
    assert config["max_posts"] == 20
    assert config["filters"]["max_results"] == 12
    assert "--random" in command
    assert command[command.index("--random-sources") + 1] == "4"
    assert command[command.index("--max-results") + 1] == "12"
    assert "--include" not in command
    assert "instagram" not in command[3:]


@pytest.mark.parametrize(
    "payload",
    [
        random_job_payload(target="instagram"),
        random_job_payload(discovery={"breadth": "standard", "result_count": 12, "tags": ["custom"]}),
        random_job_payload(filters={"max_results": 5}),
    ],
)
def test_random_job_rejects_target_custom_sources_and_duplicate_limit(payload):
    with pytest.raises(ui.UIRequestError):
        ui.validate_job_request(payload)


def test_web_request_cannot_choose_output_or_cookie_path(tmp_path):
    with running_server(tmp_path) as (server, _):
        payload = job_payload(output="C:\\Users\\Public", cookies="secrets.txt")
        response = request(
            server,
            "POST",
            "/api/jobs",
            payload=payload,
            origin=server.origin,
        )

    assert response["status"] == 400
    assert not (tmp_path / "data" / "ui-jobs").joinpath("secrets.txt").exists()


def test_single_active_job_is_enforced_without_starting_second_process(tmp_path):
    app = ui.UIApp(tmp_path / "data" / "ui-jobs", TOKEN, popen_factory=FakeProcess)
    active_id = "a" * 32
    active_dir = app.output_root / active_id
    active_dir.mkdir()
    app.jobs[active_id] = ui.Job(
        id=active_id,
        config=ui.validate_job_request(job_payload()),
        output_dir=active_dir,
        status="running",
    )
    try:
        with pytest.raises(ui.UIRequestError, match="已有任务正在运行"):
            app.create_job(job_payload())
    finally:
        app.close()


def test_log_is_a_bounded_ring_buffer(tmp_path):
    job = ui.Job(
        id="b" * 32,
        config={},
        output_dir=tmp_path,
    )
    for number in range(ui.MAX_LOG_LINES + 25):
        job.add_log(f"line {number}")

    snapshot = job.snapshot(after_sequence=1)
    assert len(snapshot["logs"]) == ui.MAX_LOG_LINES
    assert snapshot["logs_truncated"] is True
    assert snapshot["logs"][0]["message"] == "line 25"


def test_closing_app_terminates_active_collector_process(tmp_path):
    class RunningProcess:
        terminated = False

        def poll(self):
            return None if not self.terminated else -15

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return -15

    process = RunningProcess()
    app = ui.UIApp(tmp_path / "jobs", TOKEN)
    job_id = "c" * 32
    output = app.output_root / job_id
    output.mkdir()
    job = ui.Job(id=job_id, config={}, output_dir=output, status="running")
    job.process = process  # type: ignore[assignment]
    app.jobs[job_id] = job

    app.close()

    assert process.terminated is True


def test_frontend_uses_text_content_and_no_remote_assets():
    web_dir = Path(ui.__file__).resolve().parent / "static"
    script = (web_dir / "app.js").read_text(encoding="utf-8")
    markup = (web_dir / "index.html").read_text(encoding="utf-8")

    assert "textContent" in script
    assert "innerHTML" not in script
    assert 'status === "failed"' in script
    assert "elements.logBox.open = true" in script
    assert "http://" not in markup
    assert "https://" not in markup
