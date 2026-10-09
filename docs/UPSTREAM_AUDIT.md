# Upstream pull request audit

Audit of the open pull requests of [mvilanova/intervals-mcp-server](https://github.com/mvilanova/intervals-mcp-server/pulls)
on 2026-10-09, against our base `cb1fbca` (upstream `main`) plus our custom fields/streams
extension (`feat/custom-fields-and-streams`, submitted upstream as #153). Every PR branch was
fetched and diffed locally; adopted PRs were merged with `git merge --no-ff` so authorship is
preserved in the history. Statuses: `ADOPT` (merged as is), `PARTIAL` (idea or one-liner
re-implemented), `ALREADY COVERED` (our code already does it), `DEFER` (worth a separate
decision), `REJECT`.

| PR | Feature | Status | Already present | Decision | Rationale | Tests | Conflict risk |
| --- | --- | --- | --- | --- | --- | --- | --- |
| #118 | update_activity, sport settings, gear, update_wellness (older bundle) | REJECT | gear (upstream #107), rest via #141/#143/#144 or our tools | not merged | superseded bundle, overlaps everything else, touches uv.lock | yes, stale | high |
| #119 | plural `/events/{id}` path in get_event_by_id | ALREADY COVERED | fixed in our P0 work | not merged | same one-line fix is in `tools/events.py`; PR also adds a large audit doc | yes | medium (stacked series) |
| #120 | event sport in `Type:` line | ALREADY COVERED | `event_type_label()` shows category and sport | not merged | our formatter covers category, sport, planned time/load, pairing | yes | medium |
| #121 | correct keys in format_event_details | ALREADY COVERED | our `format_event_details` reads `start_date_local`, `workout_doc` | not merged | re-implemented with workout document rendering | yes | medium |
| #122 | start_date_local for activity time | ALREADY COVERED | `format_start_times()` prints local and UTC with timezone | not merged | ours shows both explicitly (P0 requirement) | yes | medium |
| #123 | created_local for activity messages | PARTIAL | re-implemented in `format_activity_message` | idea adopted | one-liner; PR branch stacked on #119-#122 | yes (ours) | medium |
| #124 | read api_key/athlete_id from env on every access | REJECT | n/a | not merged | changes config semantics; our deployment uses an EnvironmentFile and a restart; not needed | yes | medium |
| #125 | resolve gear for the activity's owner | PARTIAL | re-implemented (athlete from `icu_athlete_id`) | idea adopted | correct fix, isolated as a one-liner from a stacked branch | yes (ours) | medium |
| #126 | log athlete id and API key fingerprint at startup | REJECT | `get_server_status` / `--doctor` report the athlete without any key material | not merged | we never log key fragments (security requirement) | yes | medium |
| #129 | local start time line | ALREADY COVERED | `format_start_times()` | not merged | covered by #122-equivalent work | yes | low |
| #131 | `indoor` flag for add_or_update_event | ADOPT | – | merged | small, verified field name, tests | yes | low |
| #133 | event type handling + endpoint fix | ALREADY COVERED | see #119/#120 | not merged | duplicate of our fix | yes | low |
| #135 | power-law curve fitting tools | REJECT | power/HR/pace curves and eFTP are available | not merged | niche model without validated methodology; adds two tools of little coaching value | yes | low |
| #136 | migrate to mcp SDK v2 | DEFER | – | separate decision | SDK v2 changes transport settings and the server class; needs its own test cycle and a production venv update | yes | high |
| #139 | event 404 fix + all event fields | PARTIAL | endpoint fix ours; extra scalar fields re-implemented (`Other fields:` line) | idea adopted | our formatter already renders category/sport/workout doc; added the generic extra-field line | yes (ours) | medium |
| #140 | honour FASTMCP_HOST/PORT | ADOPT | – | merged | correctness fix with tests; explicit env handling in `mcp_instance.py` | yes | low |
| #141 | get/update sport settings | PARTIAL | `get_sport_settings`, `get_training_zones`, `get_athlete_profile` (ours, richer: absolute zone ranges, pace zones, eFTP, gear names, JSON) | not merged | read side superseded; `update_sport_settings` (write) deferred to a later release | yes | medium |
| #142 | weekly summary + plan compliance | ADOPT | – | merged | uses the athlete-summary endpoint efficiently; clear conventions verified against live data; good tests | yes | low |
| #143 | update_wellness + 1-4 scale labels | ADOPT | – | merged | labels per field from the Intervals.icu dialog; write tool is gated by the `write` class | yes | medium (formatting) |
| #144 | update_activity (RPE, feel, name, description) | ADOPT | – | merged | small write tool, response echoes API values | yes | low |
| #145 | workout library tools | ADOPT | – | merged | list/create/schedule/delete library workouts; writes gated (`write`/`destructive`) | yes | low |
| #146 | add_events_bulk | ADOPT | – | merged | validates all entries before sending; gated as `admin` (mass operation) | yes | medium (events.py) |
| #147 | HR and pace curves | ADOPT | – | merged | mirrors the power curve tool; verified response shapes | yes | low |
| #148 | JSON output for activities/wellness/events | ALREADY COVERED | `output_format="json"` on activities, details, intervals, streams, events and all new tools | not merged | we standardised on `output_format` and explicit local/UTC start fields | yes | medium |
| #149 | tags and sub_type in activity summary | ADOPT | – | merged | two lines, tests | yes | low |
| #150 | "coach athlete": coach report (load, ACWR, monotony/strain, intensity distribution with polarization index, HRV/RHR status, durability, efficiency, eFTP trend, plan projection) plus event fixes | PARTIAL | analysis tools overlap partly | bug fixes re-implemented (partial event/note updates, `category`, absolute pace syntax); coach metrics re-implemented as separate tools (`get_training_load`, `get_intensity_distribution`, `get_durability`, `get_load_projection`, `get_coach_context`) with sample sizes and without flags/verdicts | 28 files incl. lock-file churn and German-language internals; fits better as dedicated tools on our summary/wellness helpers than as one monolithic report | yes | high |
| #151 | subjective wellness `/4 (1 = best)` | ALREADY COVERED | #143 labels each field explicitly | not merged | #143 is the better fix for the same bug | yes | low |
| #152 | OAuth resource server for remote use | PARTIAL | built-in OAuth 2.1 authorization server (`auth.py`, `MCP_AUTH=oauth`) with "Continue with Intervals.icu" sign-in | idea adopted, implemented differently | #152 needs an external JWT issuer; ChatGPT needs client metadata documents or dynamic registration, PKCE and RFC 9207, so the fork ships its own authorization server that delegates the identity check to Intervals.icu OAuth; single athlete per deployment (one API key) | yes (ours) | low |
| #153 | our custom fields / streams PR | – | this fork | – | – | yes | – |

## Summary

- Adopted as merges: #131, #140, #142, #143, #144, #145, #146, #147, #149 (authors
  credited in the git history and in the README).
- Re-implemented ideas: #123 (local message time), #125 (gear of the activity owner),
  #139 (extra event fields), #141 (sport settings, as richer read tools).
- Already covered by our P0 work: #119, #120, #121, #122, #129, #133, #148, #151.
- Deferred: #136 (SDK v2). Re-implemented differently: #150 (coach metrics as separate tools), #152 (OAuth). Rejected: #118, #124, #126, #135.

Every merged PR was based on upstream `cb1fbca`; conflicts were limited to the export lists
in `server.py` / `tools/__init__.py`, the README tool list and the appended tests, which were
resolved by keeping both sides. The full test suite, ruff and mypy pass after integration.
