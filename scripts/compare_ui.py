"""Compare legacy and ES-module UI pixels against identical offline state.

Reads sibling assets only; never imports or starts legacy application code.
Optionally use --url / --legacy-url for already-running loopback demo servers;
their state responses are replaced by the same deterministic fixture. All other
non-loopback requests are blocked. No Pillow or image-processing dependency.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from browser_smoke import ROOT, VIEWS, Fixture, guard_network, local_url, navigate, offline_site, ready

PIXEL_DIFF = """async ([first, second]) => {
  const decode = async value => {
    const bytes = Uint8Array.from(atob(value), ch => ch.charCodeAt(0));
    return createImageBitmap(new Blob([bytes], {type: 'image/png'}));
  };
  const [a, b] = await Promise.all([decode(first), decode(second)]);
  const width = Math.max(a.width, b.width), height = Math.max(a.height, b.height);
  const pixels = bitmap => {
    const canvas = new OffscreenCanvas(width, height), ctx = canvas.getContext('2d');
    ctx.drawImage(bitmap, 0, 0);
    return ctx.getImageData(0, 0, width, height);
  };
  const left = pixels(a), right = pixels(b);
  const canvas = new OffscreenCanvas(width, height), ctx = canvas.getContext('2d');
  const diff = ctx.createImageData(width, height);
  let changed = 0;
  for (let offset = 0; offset < left.data.length; offset += 4) {
    let different = false;
    for (let channel = 0; channel < 4; channel++)
      different ||= left.data[offset + channel] !== right.data[offset + channel];
    if (different) changed++;
    diff.data.set(different ? [230, 25, 70, 255] : [255, 255, 255, 255], offset);
  }
  ctx.putImageData(diff, 0, 0);
  const bytes = new Uint8Array(await (await canvas.convertToBlob({type:'image/png'})).arrayBuffer());
  let binary = '';
  for (const value of bytes) binary += String.fromCharCode(value);
  const same_dimensions = a.width === b.width && a.height === b.height;
  a.close(); b.close();
  return {width, height, changed_pixels: changed, total_pixels: width * height,
          ratio: changed / (width * height), same_dimensions,
          diff_png: btoa(binary)};
}"""


def install_state(page, fixture):
    def respond(route):
        parsed = urlsplit(route.request.url)
        from urllib.parse import parse_qs

        route.fulfill(json=fixture.public_state(parse_qs(parsed.query)))

    page.route("**/api/state*", respond)


def stable_screenshot(page, path):
    """Let layout paint, then require consecutive identical captures per page."""
    page.clock.run_for(50)
    options = {
        "full_page": True,
        "animations": "disabled",
        "mask": [
            page.locator("#today"),
            page.locator("#stat-time"),
            page.locator(".log-line time"),
            page.locator(".history-row > div:first-child > span"),
            page.locator("#next-run"),
        ],
    }
    previous = page.screenshot(**options)
    for _ in range(4):
        current = page.screenshot(**options)
        if current == previous:
            path.write_bytes(current)
            return current
        previous = current
    path.write_bytes(previous)
    raise AssertionError(f"Screenshot did not stabilize: {path.name}")


def compare(new_url, old_url, output, max_ratio=0.0):
    from playwright.sync_api import sync_playwright

    cases = []
    failures = []
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai", reduced_motion="reduce")
        blocked = guard_network(context, {new_url, old_url})
        pages = [context.new_page(), context.new_page()]
        for label, page in zip(("legacy", "modules"), pages, strict=True):
            page.on("pageerror", lambda error, label=label: errors.append(f"{label}: {error}"))
            # Fixed dates make overview, logs and task history reproducible.
            page.clock.install(time=1792209600000)
            page.clock.pause_at(1792209600000)
        fixtures = [Fixture(), Fixture()]
        for page, fixture in zip(pages, fixtures, strict=True):
            install_state(page, fixture)
        try:
            for scenario in ("idle", "running", "verification", "probe"):
                baseline = Fixture()
                if scenario == "running":
                    baseline.state.update(
                        status="running",
                        action="start",
                        started_at=1792209570,
                        progress={"title": "演示视频", "value": 35, "phase": "playing"},
                        active_ai_max_requests=10,
                        ai_requests=2,
                    )
                elif scenario in ("verification", "probe"):
                    baseline.verification(probe=scenario == "probe")
                # A populated history exercises the same date and badge layout in logs.
                baseline.state["history"] = [
                    {
                        "id": "offline-task",
                        "action": "start",
                        "status": "done",
                        "origin": "manual",
                        "message": "演示操作已模拟。",
                        "started_at": 1792209540,
                        "ended_at": 1792209600,
                        "ai_requests": 0,
                        "has_log": True,
                    }
                ]
                for fixture in fixtures:
                    fixture.state = copy.deepcopy(baseline.state)
                views = VIEWS if scenario == "idle" else ("overview", "logs", "ai", "assessments")
                for width in (1440, 960, 500):
                    for page, url in zip(pages, (old_url, new_url), strict=True):
                        page.set_viewport_size({"width": width, "height": 1000})
                        ready(page, url)
                    for view in views:
                        images = []
                        name = f"{scenario}-{view}-{width}"
                        for label, page in zip(("legacy", "modules"), pages, strict=True):
                            navigate(page, view)
                            page.mouse.move(width - 1, 999)
                            page.evaluate("() => document.fonts.ready")
                            images.append(stable_screenshot(page, output / f"{name}-{label}.png"))
                        result = pages[1].evaluate(
                            PIXEL_DIFF, [base64.b64encode(value).decode("ascii") for value in images]
                        )
                        diff = base64.b64decode(result.pop("diff_png"))
                        case = {"case": name, **result}
                        cases.append(case)
                        if not result["same_dimensions"] or result["ratio"] > max_ratio:
                            failures.append(name)
                            (output / f"{name}-diff.png").write_bytes(diff)
                        else:
                            (output / f"{name}-diff.png").unlink(missing_ok=True)
            assert not errors, errors
            assert not blocked, blocked
        finally:
            context.close()
            browser.close()
    report = {
        "comparison": "passed" if not failures else "failed",
        "mode": "identical-offline-state",
        "cases": cases,
        "failures": failures,
        "page_errors": errors,
        "blocked_external_requests": blocked,
        "max_diff_ratio": max_ratio,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", type=local_url, help="New backend loopback demo URL (optional)")
    parser.add_argument("--legacy-url", type=local_url, help="Existing legacy loopback demo URL (optional)")
    parser.add_argument("--legacy-root", type=Path, default=ROOT.parent / "fuckZHS")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "playwright" / "compare")
    parser.add_argument("--max-diff-ratio", type=float, default=0.0)
    args = parser.parse_args()
    if not 0 <= args.max_diff_ratio <= 1:
        parser.error("--max-diff-ratio must be between 0 and 1")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(json.dumps({"comparison": "running"}), encoding="utf-8")
    with ExitStack() as stack:
        new_url = args.url or stack.enter_context(offline_site())[0]
        old_url = args.legacy_url or stack.enter_context(offline_site(legacy=args.legacy_root))[0]
        report = compare(new_url, old_url, args.output, args.max_diff_ratio)
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False))
    raise SystemExit(bool(report["failures"]))


if __name__ == "__main__":
    main()
