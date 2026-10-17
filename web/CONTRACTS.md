# Browser contracts

The static `index.html` contains all nine views and both dialogs. It loads only
`/js/main.js` as a native module. The server must serve `/`, `/styles.css`,
`/favicon.svg`, and the explicit `/js/*.js` asset whitelist with JavaScript MIME
types. HTML fragment insertion and the old three global scripts are unnecessary.
No build tool, framework, remote font, or CDN is used.

## Requests

- `GET /api/state` initially; subsequent polls send `since=<log_cursor>` and
  `history_revision=<history_revision>`. Visible pages poll once per second;
  hidden pages poll every five seconds. Returning to a visible page fetches now.
- `POST /api/actions/<name>` sends JSON parameters **without an `action` field**,
  `Content-Type: application/json`, and `X-Local-Token: <state.token>`.
- `GET /api/task-log/<encoded-id>` sends that token and expects `{text: string}`.
- Failed requests return a non-2xx status and `{error: string}`. The browser shows
  that message. Failed state polls disable actions and display reconnect status.

| Action | Body |
| --- | --- |
| `login`, `refresh`, `stop`, `test_ai`, `diagnose`, `quit`, `pause_plan`, `test_notification` | `{}` |
| `start` | `{courses: [courseKey, ...]}` |
| `settings` | `{settings: {speed, limit, threshold, remember, budget_minutes}}` |
| `add` | `{course: {id, name, kind, class_id}}` |
| `ai_settings` | `{ai: {enabled, no_exam, use_zhidao_ai, use_stream, max_requests, openai: {api_base, model_name, api_key?}, ppt_processing: {provide_to_ai, moonShot: {base_url, delete_after_convert, api_key?}}}}` |
| `plan` | `{plan: {enabled, time, weekdays, minutes, catchup_minutes, courses}}` |
| `notification_settings` | `{notifications: {enabled, provider, events, bark_server?, pushplus_token?, bark_key?}}` |
| `assessment_scan` | `{courses: [sharedCourseKey]}` |
| `assessment_run` | `{courses: [sharedCourseKey], assessments: [assessmentKey, ...]}` |
| `confirm_verification` | `{task_id, probe_id}` from the current waiting probe |

Omitted secret fields retain saved values; explicit empty strings clear them.
Successful saves clear secret input boxes. Public state contains `has_api_key`,
`has_pushplus_token`, and `has_bark_key`, never the secret values.

## State

State is the existing public JSON object, not a new envelope. See the executable
`demo_state()` fixture in `scripts/browser_smoke.py` for a complete example.

- Core: `token`, `demo`, `version`, `status`, `action`, `message`, `logged_in`,
  `saved_session`, `qr`, `started_at` / `ended_at` (Unix seconds or null),
  `stop_requested`, `missing_dependencies`, `settings`, `defaults`, `courses`.
- Progress: null or `{title, phase, value, remaining?}`. `phase === "waiting"`
  displays remaining seconds instead of the progress bar.
- Logs: `logs` rows `{id, time, level, message}`, `log_cursor`, `logs_reset`.
  Time is an ISO date string. On the same token, deltas append and retain the most
  recent 600 rows. `logs_reset` replaces the list. `history` may be omitted when
  `history_revision` matches; the store preserves the existing history then.
- A new token identifies a new server session: selection, dirty-form flags and
  render signatures reset. A new session must return full logs and history and
  use a new history revision. History rows use `{id, action, status, origin,
  message, started_at, ended_at, ai_requests, has_log}`.
- Feature state: public `ai`, `ai_requests`, `active_ai_max_requests`, `plan`,
  `next_run` (ISO string or null), public `notifications`, `history_revision`.
- Verification: null or `{task_id, course_keys, course_key, course_name, url,
  direct, action?}`. `verify_probe_enabled` plus a running task and
  `verification_probe.awaiting_confirmation` selects the experiment flow.
  Probe IDs, `confirmation_sent`, and `awaiting_confirmation` are authoritative.
- Assessments: `assessments` rows `{course_key, kind, id, exam_id, name, state,
  url, end?, score?, total_score?, score_percent?}` and `assessment_results` keyed by assessment key. Published finite numeric scores (including zero) occur only in list state 4; state 3 means awaiting grading. Missing scores are not zero. Refresh grades uses `assessment_scan` and never opens papers or requests a model. Only states
  1, 2, 5 are selectable; results with `state: "submitted"` disable the row.

Course keys are `kind:id`, or `ai:id:class_id`. Assessment keys are
`course_key:kind:id:exam_id`. Form values and selection are local UI state;
polling must not erase unsaved changes. Settings in the overview and preferences
views mirror each other. Starting a task first saves visible settings edits.

## DOM and module ownership

Existing element IDs, `data-view`, `data-setting`, `data-filter`, `data-key`,
`data-task-log`, `data-assessment` and `data-open-assessment` are preserved. View
hashes are `overview`, `logs`, `settings`, `ai`, `assessments`, `official`, `plan`,
`notifications`, and `guide`; unknown hashes show overview. Smoke checks observe
the DOM and HTTP responses, not global JavaScript variables.

- `api.js`: transport and current token; `store.js`: live imported state,
  selection, pending state, polling, subscriptions (`state`, `controls`,
  `offline`, `restart`); `forms.js`: dirty state and signature helpers;
  `dom.js`: icons, escaping, labels, toast; `actions.js`: common action lifecycle.
- View modules own their event handlers, rendering and local render signatures.
  `main.js` composes them and handles routing, status rendering and timers.
- In demo mode official links display the existing toast/empty embedded region;
  no official URL is loaded. Accepted iframe hosts remain the existing HTTPS
  allowlist. Closing the official region removes its `src`.

## Offline validation

Use the existing environment; neither script installs packages or browsers:

```
.venv/Scripts/python.exe scripts/browser_smoke.py
.venv/Scripts/python.exe scripts/compare_ui.py
.venv/Scripts/python.exe scripts/browser_smoke.py --url http://127.0.0.1:PORT
.venv/Scripts/python.exe scripts/compare_ui.py --url http://127.0.0.1:PORT
```

Smoke defaults to an in-memory fixture HTTP server. `--url` instead exercises the
real new demo backend and rejects a server without `demo: true`. Verification,
probe, restart and connection fault injection run in fixture mode only. Reports
state the mode explicitly. All browser requests outside the allowed local
server origins are blocked and treated as failures.

Comparison reads old assets from the sibling `fuckZHS/web` without executing old
Python code or writing there. It gives both frontends identical deterministic
public state, frozen dates, the same browser/fonts, masked time fields, and
captures full-page PNGs at 1440, 960 and 500 pixels. It compares every pixel,
rejects size differences, writes difference PNGs on failure, and requires zero
changed pixels by default. An optional existing server URL tests its asset
delivery while retaining the deterministic state fixture. This checks rendering
parity, not server/controller behavior. Outputs are under `output/playwright/`.
