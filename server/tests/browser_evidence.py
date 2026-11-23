"""Playwright evidence for failed tests using the disposable browser fixture."""

import os
import re
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError


class BrowserEvidence:
    def __init__(self, root: Path, nodeid: str, attempt: int) -> None:
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        name = re.sub(r"[^A-Za-z0-9_.-]+", "-", nodeid).strip("-")[-150:]
        self.relative = Path(worker) / f"{name}-attempt-{attempt}-{uuid.uuid4().hex}"
        self.destination = root / self.relative
        self.staging = TemporaryDirectory(prefix="iki-browser-traces-")
        self.active: dict[BrowserContext, Path] = {}
        self.errors: list[str] = []
        self.count = 0
        self.failed = False

    def record(self, context: BrowserContext) -> None:
        self.count += 1
        try:
            # A short-lived WebKit page can close before its first screencast frame.
            browser = context.browser
            context.tracing.start(
                screenshots=True,
                snapshots=True,
                screen_snapshots=browser is not None and browser.browser_type.name == "webkit",
                sources=False,
            )
        except PlaywrightError as error:
            self.errors.append(f"Could not start trace: {error}")
            return
        self.active[context] = Path(self.staging.name) / f"context-{self.count}-trace.zip"

    def stop(self, context: BrowserContext) -> None:
        path = self.active.pop(context, None)
        if path is not None:
            try:
                context.tracing.stop(path=path)
            except PlaywrightError as error:
                self.errors.append(f"Could not finish {path.name}: {error}")

    def finish(self) -> None:
        try:
            if self.failed:
                for trace in Path(self.staging.name).glob("*.zip"):
                    self.destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.move(trace, self.destination / trace.name)
        except OSError as error:
            self.errors.append(f"Could not retain browser evidence: {error}")
        finally:
            self.staging.cleanup()


DISPOSABLE_BROWSER = pytest.StashKey[Browser]()
EVIDENCE = pytest.StashKey[BrowserEvidence]()
ATTEMPTS = pytest.StashKey[dict[str, int]]()


@pytest.fixture(autouse=True)
def _browser_evidence(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    root = os.environ.get("IKI_BROWSER_ARTIFACTS")
    module: pytest.Module | None = request.node.getparent(pytest.Module)
    browser = module.stash.get(DISPOSABLE_BROWSER, None) if module else None
    if not root or browser is None or "browser" not in request.fixturenames:
        yield
        return

    attempts = request.config.stash.setdefault(ATTEMPTS, {})
    nodeid = request.node.nodeid
    attempts[nodeid] = attempts.get(nodeid, 0) + 1
    evidence = BrowserEvidence(Path(root).resolve(), nodeid, attempts[nodeid])
    request.node.stash[EVIDENCE] = evidence
    new_context = browser.new_context

    def stop_before_close(owner: BrowserContext | Page, context: BrowserContext) -> None:
        close = owner.close

        def close_with_trace(**options: Any) -> None:
            # The test may close the context before failing in a later assertion.
            evidence.stop(context)
            close(**options)

        monkeypatch.setattr(owner, "close", close_with_trace)

    def traced_context(**options: Any) -> BrowserContext:
        context = new_context(**options)
        evidence.record(context)
        stop_before_close(context, context)
        return context

    monkeypatch.setattr(browser, "new_context", traced_context)
    new_page = browser.new_page

    def traced_page(**options: Any) -> Page:
        # Browser.new_page owns an implicit context and closes it with the page.
        page = new_page(**options)
        evidence.record(page.context)
        stop_before_close(page.context, page.context)
        stop_before_close(page, page.context)
        return page

    monkeypatch.setattr(browser, "new_page", traced_page)
    try:
        yield
    finally:
        for context in list(evidence.active):
            evidence.stop(context)


def report_browser_evidence(item: pytest.Item, report: pytest.TestReport) -> None:
    evidence = item.stash.get(EVIDENCE, None)
    if evidence is None:
        return
    if report.failed:
        evidence.failed = True
        if evidence.count:
            report.sections.append(("Browser evidence (path in archive)", str(evidence.relative)))
    if report.when == "teardown":
        evidence.finish()
        del item.stash[EVIDENCE]
        if evidence.errors:
            report.sections.append(("Browser evidence diagnostics", "\n".join(evidence.errors)))
