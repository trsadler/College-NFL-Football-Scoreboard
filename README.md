# NFL/College Scoreboard

Custom NFL/NCAA football scoreboard plugin for LEDMatrix. Standalone plugin,
not a fork of the official `football-scoreboard` registry plugin -- built
from scratch with its own layout AND (as of a later architecture rewrite,
see the numbered item near the end of "Known gaps" below) its own complete
ESPN fetch/extraction. Includes past scores and upcoming games in addition
to live game tracking.

## Architecture

Fully standalone -- one shared `requests.Session()`, its own complete ESPN
fetch and extraction logic (`_fetch_league_scoreboard`/`_parse_game_event`/
`_resolve_logo` in `manager.py`), no dependency on any core sports base
class. This mirrors the `ledmatrix-tidbyt-baseball` plugin's own current
architecture -- confirmed directly from its real, currently-working source,
after this plugin's original design (inheriting from
`src.base_classes.football.Football`/`FootballLive` and
`src.base_classes.sports.SportsRecent`/`SportsUpcoming`) broke entirely when
LEDMatrix deprecated those shared "built-in manager" base classes in favor
of fully standalone plugins. The section below ("What's reused vs. custom")
and most of the numbered items in "Known gaps" predate that rewrite and
describe the OLD, now-removed architecture -- kept as a historical record of
what was tried and why, not as current fact. See the item titled "Complete
architecture rewrite" near the end of "Known gaps" for what actually
changed and what's been verified since.

## What's reused vs. custom (describes the ORIGINAL architecture -- see
## "Architecture" above for what's actually true now)

**Reused from `src.base_classes.football`:**
- `FootballLive` -- ESPN scoreboard polling, live/upcoming/final state
  handling, favorite-team prioritization, display rotation timing
- `Football._extract_game_details()` -- normalizes ESPN's response into
  abbreviations, scores, logos, records, period/clock, down & distance,
  possession, timeouts

**Fully custom:**
- `_draw_scorebug_layout()` -- completely overridden. This is the only
  method `FootballLive` uses to put pixels on the display, so overriding
  it swaps the look without touching any data plumbing.
- Team stack (logo/abbreviation/score/timeouts/possession icon), field
  strip (end zones/goal posts/yard lines), and ball-position indicator
  (football icon + arrow + yard number) match the pixel layout mocked up
  during design.


## Known gaps / TODOs before this runs correctly on real games

1. ~~Yard-line math is unverified.~~ **FIXED & VERIFIED.** Checked against
   a real ESPN play object (`{"distance": 13, "yardLine": 43,
   "possessionText": "BUF 43", "yardsToEndzone": 57}` — 43 + 57 = 100),
   confirming `yardLine` runs 0-100 from the *possessing* team's own goal
   line. Since our display always puts the away team's end zone on the
   left and home's on the right, the pixel math now branches on
   `possession_indicator` (away → maps left-to-right; home → maps
   right-to-left). Also switched the field-position label (both the info
   row and the number above the ball) to parse ESPN's own
   `possessionText` string directly instead of re-deriving "team + yard"
   ourselves — that field already correctly identifies which side of the
   field the ball is on, including after crossing midfield.
2. ~~Team colors are a gray placeholder.~~ **FIXED.** ESPN's team object
   carries real hex colors (verified against a live boxscore response:
   `"color": "061642", "alternateColor": "bc945c"`). `_extract_game_details()`
   now pulls both and picks whichever one isn't too close to pure white/black
   to read as a solid end zone fill (some teams' primary color *is* white,
   which disappears against the display's black background) -- falls back
   to gray only if both are missing/unusable.
3. ~~Logos aren't drawn yet.~~ **FIXED.** `_draw_team_stack()` now calls the
   inherited `SportsCore._load_and_resize_logo()` (same caching/auto-download
   pipeline the core project already uses elsewhere) and fits the result
   into the 25x16 logo box, centered on a swatch of the team's color so
   mismatched aspect ratios don't leave a black gap. Falls back to a flat
   color swatch only if the logo genuinely can't be loaded.
4. Still worth confirming against an actual **live** game once the season
   starts — the yard-line fix is checked against a real play-by-play object,
   but not yet against a live top-level `situation` block during an
   in-progress game. Also haven't visually confirmed the logo-fit/color-pick
   logic against real team art yet (light-colored logos on a light end zone
   swatch, for instance, might need a contrast check we haven't added).
5. **Fixed a real wiring bug**: `Football.__init__` sets `self.sport =
   "football"` but never sets `self.league`, and `SportsCore` builds the
   actual ESPN fetch URL as `.../sports/{self.sport}/{self.league}/...` --
   so live data fetching (and logo downloads, which use the same
   `sport_key`) would have been broken regardless of network access.
   Separately, ESPN's URL slug for a league ("nfl"/"college-football") is
   NOT the same string the core project uses internally for logo
   directories and config keys ("nfl"/"ncaa_fb", per
   `src/logo_downloader.py`'s `LOGO_DIRECTORIES`) -- added
   `LEAGUE_TO_SPORT_KEY` to map between them and set `self.league`
   explicitly in `__init__`.
6. ~~Only wires up one league per plugin instance.~~ **FIXED.** Refactored
   into a composition model: the plugin no longer *is* a `FootballLive`
   itself. Instead it owns one `_LeagueDataWorker(FootballLive)` per
   configured league (each with the correct `sport_key`/`league` pair),
   calls `.update()` on all of them, merges their `live_games`, and applies
   favorite-team `live_priority` ordering across the merged list. Verified
   against a hand-built scenario with two workers (NFL + college) each
   returning games -- correctly picked the favorite-team game across
   league boundaries.
7. **Config schema rewritten** to match what was actually requested:
   `favorite_teams`, `show_favorite_teams_only`, and independent
   `live.enabled` / `upcoming.enabled` / `recent.enabled` toggles, plus
   `live_priority`, per-state durations/update intervals, and
   `show_records`/`show_ranking`/`show_odds`. `_build_worker_config()`
   translates this into the `{sport_key}_scoreboard` namespace
   `SportsCore`/`FootballLive` actually read from -- verified the
   translation produces correctly-shaped `nfl_scoreboard` /
   `ncaa_fb_scoreboard` sub-configs.
8. ~~`upcoming.enabled`/`recent.enabled` don't do anything yet.~~ **FIXED**
   in a later pass -- `_RecentDataWorker`/`_UpcomingDataWorker` exist now
   and `update()` checks live -> recent -> upcoming in order, respecting
   each one's `enabled` flag.
9. **Rotation across multiple simultaneous live games is a stub.**
   `update()` picks the single top-priority game and shows only that one;
   `live.game_duration_seconds` is accepted by the schema but nothing
   currently cycles through multiple live games using it.
10. **Fixed a manifest.json bug that blocked installation entirely**:
    the core plugin loader requires `display_modes` (checked by
    `store_manager.py`) plus `compatible_versions`, `requires`, and a few
    other fields (checked against `schema/manifest_schema.json`) that our
    original manifest never had -- it was missing `display_modes` and
    `compatible_versions` outright, which surfaced as "Manifest missing
    required fields: display_modes" on install. Rebuilt the manifest
    against a real example (the NHL plugin manifest documented in
    `PLUGIN_ARCHITECTURE_SPEC.md`) and verified it against both the
    `store_manager.py` required-field check and the formal
    `manifest_schema.json` (including the `compatible_versions` semver
    regex) manually, since `jsonschema` itself isn't installed in this
    sandbox.
11. **Fixed a real bug in the test-mode logo paths**: `logo_path` was
    passed as a plain string (via `os.path.join`), but the actual
    `SportsCore._load_and_resize_logo()` calls `logo_path.parent` --
    which only exists on a `pathlib.Path`, not a string. This worked fine
    against this sandbox's own simplified test stub (which defensively
    wrapped everything in `Path(...)` before checking it), masking the
    bug -- it only surfaced once run against the real method on actual
    hardware, where it raised `AttributeError`, got silently caught by
    the existing try/except around logo loading, and fell back to flat
    color swatches with no visible error. Fixed by switching test-mode
    logo paths to real `Path` objects, and **also tightened the sandbox's
    own test stub** to stop defensively wrapping its input, so this class
    of bug gets caught locally next time instead of only showing up on
    real hardware.
12. **Applied the same User-Agent fix as the baseball plugin**: ESPN
    started rejecting some User-Agent strings (per real-world testing
    on the baseball plugin). Traced this plugin's two SEPARATE header
    paths -- `_fetch_todays_games()` (used by the live worker) sends
    `self.headers['User-Agent']`, which `SportsCore.__init__` sets to a
    literal unfilled placeholder,
    `'LEDMatrix/1.0 (https://github.com/yourusername/LEDMatrix;
    contact@example.com)'`, identical across every LEDMatrix install;
    `fetch_schedule()` (used by the recent/upcoming workers) instead goes
    through `ESPNDataSource.get_headers()`, a different but equally
    generic `'LEDMatrix/1.0'`. Both get overridden now, via
    `_apply_user_agent_fix()` called on every worker right after
    construction, with a distinct per-plugin User-Agent
    (`LEDMatrix-NFLCollegeScoreboard/1.0`), matching the fix pattern
    baseball already validated. **Not verified from this sandbox** --
    there's no raw network access here (bash networking is disabled, and
    `web_fetch` doesn't expose custom headers), so this couldn't be
    tested against ESPN's actual rejection behavior directly; needs
    confirming on real hardware.
13. **Pulled real live ESPN data for the first time** (previously only
    possible against historical play-by-play records, since it was the
    offseason) -- the 2026 Hall of Fame Game (CAR @ ARI) was pulled
    directly from the real scoreboard endpoint, but was still
    `STATUS_SCHEDULED`/state `"pre"` (8:00 PM ET kickoff hadn't happened
    yet) at fetch time, so this didn't yet exercise the live `situation`
    object. The yard-line/possession logic is still only verified
    against a historical play-by-play record (see the earlier fix), not
    a genuinely live top-level `situation` block -- worth re-fetching
    after kickoff.
14. **Added baseball-style diagnostic logging** to `update()` and
    `__init__()`, for the same reason baseball's README describes: "only
    test mode works" is ambiguous on its own -- it could mean `__init__`
    is failing (though that would break test mode too, since worker
    construction is unconditional), `update()` never getting called, the
    fetch itself failing, or the fetch succeeding but genuinely finding
    zero games. `update()` now logs `Fetched {state}/{league} OK: N
    game(s)` on success or `FETCH FAILED: {exception}` on failure for
    each state/league, plus a summary when nothing is found at all;
    `__init__` logs a `WORKER CONSTRUCTION FAILED` line if a specific
    worker type fails to build, and an overall summary of which
    live/recent/upcoming workers actually got created. Verified both the
    "success but empty" and "real exception" cases produce distinctly
    different log output.
15. **Real 403 Forbidden confirmed on real hardware, during the actual live
    Hall of Fame Game (2026-08-06)** -- ESPN rejected the recent/upcoming
    fetch (`fetch_schedule`'s `dates=` range request) with a genuine 403,
    even with the User-Agent fix from item 12 already deployed. That
    disproves the theory that a distinct app-identifying User-Agent alone
    is sufficient -- confirmed not sufficient, not just unconfirmed.
    Switched `_apply_user_agent_fix()` to mimic a real browser's full
    header set (User-Agent, Accept, Accept-Language, Referer, Origin)
    instead of a custom app name, on the theory that a distinctive
    app-identifying string may be MORE conspicuous to bot detection, not
    less. **This replacement is also not yet confirmed working** -- same
    limitation as before, no way to test ESPN's actual rejection behavior
    from this sandbox.
16. **Found and fixed a real visibility gap this 403 exposed**: the core
    `ESPNDataSource.fetch_schedule()` catches ALL exceptions internally
    (including HTTP errors) and returns an empty list rather than
    re-raising -- so the 403 in item 15 was completely invisible to this
    plugin's own error handling; `update()` only ever saw "0 games found,"
    indistinguishable from a genuinely empty schedule, and the only reason
    the 403 was visible at all was the core method's own separate log
    line. Added `_fetch_schedule_direct()`, which duplicates just enough
    of `fetch_schedule`'s request logic to let real HTTP errors propagate
    as exceptions, so they now surface as `FETCH FAILED: HTTPError: 403
    ...` through the item-14 diagnostic logging instead of silently
    presenting as an empty result. Verified against a simulated 403 using
    the exact URL/params from the real error log -- confirmed it now
    surfaces correctly instead of being swallowed.
17. **Found and fixed the exact same visibility gap in the LIVE path.**
    After item 16's fix, "plugin is not initializing" turned out to still
    be happening (confirmed via a direct question: test mode still worked,
    which rules out `__init__`/construction -- the failure had to be in
    the real-data fetch path specifically). Checked whether
    `_fetch_todays_games()` (used by the live worker, separate code path
    from `fetch_schedule()`) had the same problem: it does -- catches
    `requests.exceptions.RequestException` internally and returns `None`
    instead of re-raising, exactly like item 16's bug, just in a different
    method. This one had gone unnoticed because all prior testing/analysis
    focused on the recent/upcoming 403 specifically. `_LiveDataWorker`
    now bypasses `_fetch_todays_games()` the same way `_RecentDataWorker`/
    `_UpcomingDataWorker` bypass `fetch_schedule()` -- replicates its exact
    request logic (including the `pytz` America/New_York timezone handling
    it uses, now added as an explicit dependency) but lets real HTTP
    errors propagate. Verified against a simulated 403 on this specific
    path -- confirmed it now surfaces as `FETCH FAILED` instead of
    silently returning nothing, matching the fix already verified for
    recent/upcoming.
18. **Corrected a claim from item 17**: `_RecentDataWorker`/`_UpcomingDataWorker`
    only override `_fetch_data()`, not `update()` itself -- and the
    *inherited* `SportsRecent.update()`/`SportsUpcoming.update()` (core
    code, called directly as `worker.update()`) has its OWN try/except
    wrapped around the call to `_fetch_data()`, which catches our
    now-propagating exception and logs it as `"Error updating recent
    games: {e}"` -- **before** it ever reaches this plugin's own
    diagnostic logging. That handler does not re-raise. So the `FETCH
    FAILED` messages from items 16/17 likely never actually fire for
    this path in practice; the real 403 was already visible via the
    core method's own logging the whole time, just under a different
    message format than this plugin's own. The underlying propagation
    fix (letting real HTTP errors raise instead of silently returning
    empty) is still correct and still needed -- the visibility claim
    specifically was overstated.
19. **The browser-header fix (item 15) was tested on real hardware and
    did NOT resolve the 403** -- confirmed via a real log line during
    the actual live game, after confirming the deployed code did include
    that fix. That rules out "generic User-Agent string" as a
    sufficient explanation on its own (both a custom app name AND a
    full browser-mimicking header set produced the same 403).
    Reconsidered what's actually unusual about these requests
    independent of headers: `limit=1000` combined with a 21-day (recent)
    or 14-day (upcoming) date range is not a shape a real browser would
    ever request -- browsing espn.com never asks for three weeks of
    scoreboard data in one call. Narrowed `limit` to 100 and both date
    ranges to 7 days, on the theory that the *parameter shape* itself
    may be triggering rejection independent of headers. **Not yet
    confirmed working** -- same sandbox limitation as every other
    attempt at this specific problem. Also a real trade-off to flag:
    narrowing the recent-games window from 21 to 7 days means games
    older than a week won't be found even though `SportsRecent.update()`'s
    own internal filter would otherwise allow up to 21 -- prioritized
    getting any data through at all over the full window, but this
    should be revisited once something actually works.
20. **The narrowed request-shape fix (item 19) was ALSO tested on real
    hardware and did NOT resolve the 403** -- confirmed via another real
    log line, same message, now showing the narrowed `dates=20260731-
    20260807&limit=100`. Three different theories (custom User-Agent,
    full browser headers, narrowed request shape) have now all failed
    identically. Rather than guess a fourth header/parameter variation,
    asked for a real diagnostic instead: whether the baseball plugin
    (already running on this same Pi) was fetching live ESPN data
    successfully at the same moment football was getting 403'd.
    **Confirmed baseball was working fine** -- same IP, same Pi, same
    moment. That rules out IP-level blocking, a general ESPN outage, and
    local network issues entirely; the problem is specific to something
    this plugin's requests do differently from baseball's.
21. **Found a real, confirmed architectural difference from baseball**:
    baseball uses a single `requests.Session()` for everything. This
    plugin's per-league workers (live/recent/upcoming) were each
    independently constructing their own session -- in fact *two* each
    (`SportsCore.__init__`'s `self.session`, and a separate one inside
    `Football.__init__`'s `self.data_source`) -- meaning up to 6 separate
    sessions per league, all potentially firing requests within the same
    `update()` cycle. Consolidated to one shared `requests.Session()` per
    league, assigned to every worker's `.session` and
    `.data_source.session`. Verified via direct object-identity checks
    (not just "it compiles") that all three workers for a league now
    share the exact same session object on both attributes. **Not yet
    confirmed this resolves the 403** -- this is the first fix attempt
    grounded in a real, confirmed difference from a working plugin rather
    than a guess at header/parameter values, but it still needs testing
    against the real ESPN rejection before trusting it.
22. **Found and fixed a severe regression from item 17's own fix**: after
    a real gap in testing (season start), the plugin stopped initializing
    ENTIRELY on real hardware -- not "test mode works, real modes don't"
    like before, but no log output at all, not even an attempt to start.
    Root cause: item 17 added `import pytz` at module level to replicate
    `_fetch_todays_games()`'s timezone handling, and `pytz` was never
    confirmed to actually be installed in the real plugin environment --
    only assumed safe because the CORE project's `requirements.txt` lists
    it. That assumption was wrong to rely on: a plugin's own
    `requirements.txt` doesn't necessarily get (re-)installed on an
    update to an already-installed plugin, only possibly on a fresh
    install. A failing top-level import crashes loading the entire
    module before any of this plugin's own logging can run at all --
    which explains total silence far better than any error message
    would. Replaced `pytz.timezone("America/New_York")` with the
    standard library's `zoneinfo.ZoneInfo("America/New_York")` (built
    into Python 3.9+, no pip install ever needed) and removed `pytz`
    from `requirements.txt` entirely. Verified `zoneinfo` resolves the
    same timezone correctly (EDT, -04:00) as a direct sanity check, not
    just that the file compiles. Audited every other module-level import
    and top-level statement in the file for the same risk -- everything
    else is either standard library, a dependency (`requests`/`Pillow`)
    the core project itself requires for anything to run at all (so its
    absence would break baseball too, which is known-working), or a
    plain string/dict/list literal with no I/O. This was the only
    self-introduced risk of this kind in the file.

## Test mode

`config.test_mode` lets you preview any view directly on real hardware
without needing an actual live/recent/upcoming game to exist:

```json
"test_mode": {
  "enabled": true,
  "view": "live"
}
```

`view` is one of `"live"`, `"recent"`, `"upcoming"`, or `"all"` (cycles
through all three, switching every `display_duration` seconds). When
enabled, `update()` short-circuits before any ESPN calls and serves one of
three hardcoded sample games (`_TEST_LIVE_GAME`/`_TEST_RECENT_GAME`/
`_TEST_UPCOMING_GAME` in `manager.py`) through the exact same drawing code
real games use -- so this checks the actual render path, not a separate
mock. Verified the dispatch logic selects the correct game/state for all
four `view` values, including that `"all"` cycles.

Sample games use six real NFL logos (BUF/KC/DAL/DET/GB/CHI, one pair per
view) bundled in this plugin's own `test_logos/` folder -- pulled directly
from the actual LEDMatrix core repo's `assets/sports/nfl_logos/`, not
downloaded or generated. These are for test-mode previewing only; real
games never touch this folder. Once this runs with real ESPN data, logos
come from the existing `_load_and_resize_logo()`/`download_missing_logo()`
pipeline instead (same one the core project and other sports plugins use),
which fetches straight from ESPN's CDN and caches locally -- that path
needs real network access to verify, which isn't available in the sandbox
this was built in, but the code doesn't distinguish test-mode logos from
real ones; it's the same `logo_path` mechanism either way.

## Complete architecture rewrite (standalone, no core sports base classes)

Everything above this section (except "Architecture" near the top) predates
this and describes the ORIGINAL design. This section documents what
actually changed, why, and what's been verified since.

**What broke, and why.** After a real gap in testing (season start), the
plugin failed to load at all on real hardware: `Unexpected error loading
plugin nfl-college-scoreboard: No module named 'src.base_classes.football'`.
Investigation (pulling the current LEDMatrix core repo directly from GitHub)
confirmed this wasn't a renamed/moved module -- the entire shared "built-in
manager" architecture (`src.base_classes.football`/`sports`, which
`FootballLive`/`Football`/`SportsRecent`/`SportsUpcoming` all came from) was
deprecated and removed from the core project entirely, in favor of every
sport plugin being fully standalone. Confirmed via the current LEDMatrix
README itself ("Built-in Managers Deprecated... moved to the plugin
system") and via a newly-released **official** `football-scoreboard` plugin
in the `ledmatrix-plugins` registry (version 2.10+, actively maintained,
far more feature-rich than this plugin -- score/win celebrations, odds
integration, shared scroll orchestration). Chose to keep building this
plugin's own custom design rather than switch to the official one, per
explicit direction, accepting that meant a full rewrite of the data layer
rather than a patch.

**What `BasePlugin` status turned out to be.** Re-uploading and inspecting
baseball's CURRENT source (still confirmed working on real hardware)
showed `from src.plugin_system.base_plugin import BasePlugin` wrapped in a
`try/except ImportError`, with a local fallback class used "ONLY for
sandbox testing when the real LEDMatrix framework isn't installed."
`BasePlugin` itself was never deprecated -- only the sport-specific shared
base classes were. This plugin now guards that import the same way.

**The rewrite, concretely.** Removed `_ExtractionMixin` and the three
core-inherited worker classes (`_LiveDataWorker`/`_RecentDataWorker`/
`_UpcomingDataWorker`, one per league, each delegating to a removed core
class). Replaced with a single standalone class matching baseball's own
proven architecture:
- One shared `requests.Session()` for everything (not up to 6 separate
  sessions across leagues/states, which the old per-worker design created)
- `_fetch_league_scoreboard()`: ONE fetch per league per `update()` cycle,
  covering live/recent/upcoming all at once (ESPN's scoreboard endpoint
  naturally returns all three states together) -- not a separate fetch per
  state per league like the old design. Raises on a real HTTP error rather
  than swallowing it (the old core-provided fetch methods were found to
  swallow exactly this class of error, making a real 403 invisible to this
  plugin's own diagnostics).
- `_parse_game_event()`: builds the complete game dict from raw ESPN JSON
  from scratch -- team abbreviations, scores, records, colors, logos,
  quarter/clock, down/distance, possession, yard line, timeouts,
  linescores, leaders -- mirroring baseball's own `_parse_game` for the
  general shape and field-confidence caveats, adapted for football-specific
  fields in place of baseball's (balls/strikes/outs, bases, inning).
- `_resolve_logo()`: local bundled asset first, then download-and-cache-
  to-disk from ESPN, mirroring baseball's own `_resolve_logos`/
  `_get_team_logo`/`_load_local_logo` pattern. Best-guess local-asset
  folder per league (`assets/sports/nfl_logos` for NFL, confirmed present
  on real hardware via this project's earlier test-mode work;
  `assets/sports/ncaa_logos` for college football, NOT confirmed -- a
  wrong guess here just means a slower first load via the download
  fallback, not a broken one).
- The three rendering-code call sites that used to delegate logo loading
  through a per-league "worker" instance now call
  `self._load_and_resize_logo()` directly -- logo resolution already
  happened once during fetch, so rendering just opens the resolved path.
- Everything from `display()` onward -- every drawing method built across
  this entire project (the field/yard-line visualization, the recent/
  upcoming layout redesigns, the ported font engine) -- is completely
  untouched. None of it ever depended on the removed core classes directly;
  it only ever consumed a plain game dict.
- Cleaned up now-dead imports (`timedelta`, `timezone`, `BytesIO`,
  `zoneinfo` -- the new single-fetch-per-league design doesn't need
  date-range arithmetic at all, unlike the old per-state-fetch design) and
  the now-unused `_build_worker_config`/`LEAGUE_TO_SPORT_KEY`/
  `_apply_user_agent_fix` methods.

**The header set was also upgraded using real evidence, not another
guess.** Baseball's current source shows it hit the *identical* 403 issue
around the same date as this plugin's own investigation, and its
proven-working fix (confirmed: baseball fetches successfully on the same
Pi/IP where this plugin was getting 403'd) goes further than anything
tried here previously -- a full realistic browser header set including
`Accept`/`Accept-Language`/`Accept-Encoding`/`Referer`/`Origin`/
`Connection` AND the three `Sec-Fetch-*` headers, which had never been
attempted in this plugin's own prior header-fix attempts. Adopted
verbatim rather than partially. **Still not verified against ESPN's real
rejection behavior** -- no outbound network access in the sandbox this
was built in -- but this is the strongest evidence available for any
header configuration tried across this whole investigation, since it's
drawn from a plugin CONFIRMED working right now, not a guess.

**What's been verified, and how (not just "it compiles"):**
- The plugin imports and constructs successfully with `sys.modules`
  containing NO stub for `src.base_classes` at all (only `BasePlugin` is
  stubbed) -- direct proof the removed-class dependency is gone, not an
  inference from reading the diff.
- Fed a realistic mocked ESPN scoreboard event (shaped like a real live
  NFL game JSON) through the actual `update()` method end to end. Every
  extracted field came out correct: abbreviations, scores, colors
  (hex-to-RGB), record, down/distance text, possession side, yard line,
  period/clock, and leaders (filtered to the right team).
- Fed that same real extracted-shape game dict into `_draw_scorebug_layout`
  directly -- rendered with no exception.
- Simulated a 403 through the real `update()` call path -- confirmed it
  produces both the specific "likely blocked/rate-limited" diagnostic log
  AND the generic `FETCH FAILED` log, with no unhandled exception, and
  `current_game`/`current_state` correctly reset to `None`.
- Re-ran all three test-mode views (live/recent/upcoming) through the
  actual `update()` + `display()` call path after every change in this
  rewrite, including after the final import cleanup -- all three still
  render successfully throughout.
- Verified `_resolve_logo()` degrades gracefully (no exception, leaves
  `logo_path` as `None`) when there's no local asset folder and no real
  network to download from, which is exactly this sandbox's situation --
  confirms the fallback path doesn't crash even in the worst case.

**Still needs verification on real hardware, which this sandbox cannot
provide:** whether the expanded header set actually clears ESPN's 403,
whether `assets/sports/ncaa_logos` is really the correct folder name for
college football on the real Pi (falls back to downloading if wrong,
which itself needs real network to verify), and general behavior against
a real, currently-live or recently-completed game now that the season has
started.

**UPDATE, confirmed on real hardware:** the rewrite works and the 403 is
resolved. Real logs from the Pi show `Fetched nfl OK: 0 live, 0 recent,
16 upcoming` with no errors -- the fetch, the new header set, and the
extraction logic are all genuinely working against live ESPN data now
that the season has started.

## Real bug found on real hardware: display() never received which mode
## the core wanted shown

Real symptom (from the same log confirming the fetch works): the core's
display controller correctly cycled through all three declared modes
every ~15s (`Switching to mode: nfl_college_recent`, `..._upcoming`,
`..._live`, repeating) -- but this plugin's own diagnostic logging showed
`Showing UPCOMING: ATL@GB` every single time, regardless of which mode
the core had just switched to. The display looked "stuck," but the real
cause had nothing to do with fetching (which was working the whole
time) -- `display()` had no way to know which mode was active.

**Root cause, confirmed from real LEDMatrix core source/PRs (not
inferred):** `display_controller.py` inspects a plugin's `display()`
signature and only passes a `display_mode` keyword (the specific mode
string, e.g. `"nfl_college_recent"`) if that method actually accepts it;
otherwise it silently falls back to calling `display(force_clear=True)`
with no mode information at all. Our previous signature
(`display(self, force_clear=False)`) didn't accept it, so every mode
switch fell through to that same no-mode call, and we always rendered
whatever our own global live>recent>upcoming priority logic had picked
-- completely ignoring which of the three modes the core was actually
asking for. Also confirmed (from a real bug report against other official
plugins): `display()` should return `True`/`False`, not `None` -- the
controller skips a mode immediately when `False` is returned, so a mode
with no current content (e.g. "live" when nothing's live) doesn't show a
blank or stale panel.

**Fix:** `display()` now accepts `display_mode: Optional[str] = None`.
When provided, it's mapped (via `_MODE_TO_STATE`) to the matching game
list (`live_games`/`recent_games`/`upcoming_games`) and draw method,
favorite-team-sorted, and rendered -- returning `True` if that mode had a
game to show, `False` if not (letting the core skip straight to the next
mode). When `display_mode` isn't passed at all (test mode's own call
path, which has no notion of "which mode is active" since it's always
previewing one fixed sample), the old global-priority behavior
(`current_game`/`current_state`) is used unchanged.

**Verified, not just written:**
- `inspect.signature(...).parameters` confirms `display_mode` is now a
  real parameter the core's own introspection check will detect.
- Reproduced the exact real-world scenario from the log (only upcoming
  games populated, nothing live/recent) and simulated the core cycling
  through all three modes: `nfl_college_live` and `nfl_college_recent`
  now correctly return `False`, `nfl_college_upcoming` returns `True` --
  the core would now correctly skip straight to showing the upcoming
  game instead of displaying it under all three mode labels.
- Verified all three modes independently when each has its own real data
  populated (not just the single-mode-populated case above).
- Verified an unknown/unrecognized mode string returns `False` gracefully
  (logged, no exception) rather than crashing.
- Re-ran the full test-mode regression (all three views via the legacy
  no-`display_mode` path) after this change -- still renders correctly.

## Real gap: "recent" could never show a game from last week

Confirmed via real hardware (and explicit user confirmation of the
expected behavior): with the `display_mode` fix live, the plugin
correctly cycled to the upcoming game's mode -- but `recent` stayed
empty even though a full week's worth of games had already been played
and finished. Root cause: `_fetch_league_scoreboard()`'s single ESPN
call only returns the CURRENT NFL week's games (Thursday through
Monday), not a rolling window -- so a game that finished last week
never appears in it at all, regardless of how long ago it finished.

**Fix:** added `_fetch_recent_lookback()`, a separate fetch covering the
last 10 days via ESPN's `dates=` range parameter, mirroring baseball's
own `_fetch_past_games_lookback` (which exists for this exact reason,
confirmed from its current real-hardware-working source). Runs on its
own slower timer (`recent.update_interval_seconds`, default 1 hour) --
not every `update()` cycle -- since a completed game's result never
changes once final, unlike live data. Results are merged into
`recent_games`, deduplicated by `event_id` against whatever the main
per-week call already found.

**Verified, not just written:**
- Simulated the main call returning only an upcoming game and the
  lookback call returning a finished game from a different week --
  confirmed the finished game correctly appears in `recent_games` and
  gets selected as `current_state` (recent outranks upcoming in the
  priority order).
- Verified the throttle: called `update()` three times in a row and
  confirmed the main fetch ran all three times (as it should, live data
  changes) while the lookback fetch ran exactly once (cached for the
  configured interval).
- Re-ran the full test-mode regression after this change -- still
  renders correctly.

## Real gap: only ever showed games[0], never rotated

Confirmed via explicit user report: even with 16 upcoming games fetched
(the full Thu/Sun/Mon week's slate), only ever showed the same single
game, and it never advanced. `game_duration_seconds` and `games_to_show`
had been in `config_schema.json` since the beginning (20s/15s defaults,
10/5 game caps) but were never actually implemented -- `display()`
always just rendered `games[0]` after favorite-sorting.

**Fix:** added `_pick_rotated_game()`, using the same deterministic
wall-clock approach already used by `_update_test_mode`'s "all" cycling
(`int(time.time() // duration) % len(games)`) rather than mutable
per-mode index state -- whichever game "should" be showing at a given
moment is computed fresh every call, so it doesn't matter how often or
irregularly `display()` gets invoked. Also wired up the previously-inert
`games_to_show` cap (truncates the list before rotating).

**Verified:** simulated `display()` calls at increasing time offsets
with 3 games and a 15s duration -- confirmed it advances to the next
game exactly at each 15s boundary and wraps back to the first after
cycling through all three. Verified `games_to_show=2` against a 16-game
list stays confined to only the first two, never rotating into the rest.
Re-ran the full test-mode regression -- still passes.

## Real bug: recent-lookback fetch got a 400, not a 403

Confirmed via real hardware logs, and distinct from every other issue in
this project: `_fetch_recent_lookback()` (added last session to find
last week's finished games) failed with `400 Client Error: Bad Request`
on the `dates=20260914-20260924` (10-day) range -- a different error
class than the 401/403s investigated everywhere else here. A 400 means
ESPN considers the request itself malformed, not just unwelcome.

**Fix:** reduced the lookback window from 10 days to 7. Best guess: the
`dates=` range parameter has a maximum span ESPN accepts, and 10 days
exceeded it -- this plugin's own pre-rewrite code used this exact
request shape with a 7-day window and only ever got 403s (a header/
blocking issue, since fixed), never a 400, so 7 days is a value already
confirmed not to trigger this specific error class. **Not independently
verified that 7 is the actual limit** -- same sandbox limitation as
everywhere else in this project (no outbound network access here).

Also added response-body logging to both scoreboard fetch methods (main
and lookback) for any non-OK status, not just 401/403 -- ESPN's error
responses often explain the actual rejection reason, and this project
has repeatedly needed a full redeploy-and-check-logs round-trip just to
see status codes with no body. If this happens again, the actual reason
should be visible in the log line directly.

## The 400 was NOT a range-length issue -- it was the wrong query shape entirely

The previous fix (reducing the lookback window from 10 to 7 days) was
tested on real hardware and **confirmed wrong**: the exact same `400
Client Error: Bad Request`, with ESPN's own error body
(`{"code":400,"message":"Failed to get events endpoint."}`), came back
for the 7-day range too. Narrowing the range wasn't the fix.

**Real root cause, found by re-diffing against baseball's actual current
source:** this plugin's `_fetch_recent_lookback()` used a single
hyphenated date-RANGE query (`dates=20260917-20260924`). Baseball's own
`_fetch_past_games_lookback` -- confirmed working on the same real
Pi/IP right now -- **never does this**. It loops over each individual
day and queries ESPN with a single date each time (`dates=20260917`,
then `dates=20260918`, etc., one request per day, no hyphen at all).
The range-query format is apparently just not valid input for this
endpoint.

This also retroactively explains something that looked like evidence
for the wrong theory earlier: this plugin's very first, pre-full-rewrite
version used this same range-query shape and got a 403, not a 400 --
which looked like proof the format was valid syntax, just rejected for
an unrelated (header/bot-detection) reason. In hindsight, that 403 was
most likely a blocking layer rejecting the request based on headers
alone, before it ever reached whatever backend logic validates the date
parameter -- so the range format was never actually confirmed valid,
just rejected earlier in the pipeline for a completely different
reason. Only after the header fix got requests past that layer did the
real validation error underneath become visible for the first time.

**Fix:** switched to baseball's exact proven shape -- loop over each of
the last 7 days, one single-date query per day, collecting and merging
results. More requests than the single range-query attempt, but
baseball does exactly this against the same real ESPN endpoint from the
same Pi/IP and is confirmed working, so this isn't a re-introduction of
the request-volume concern investigated earlier in this project -- it's
simply the correct request shape for this kind of query, distinct from
the main per-league scoreboard call (which takes no date parameter at
all and was never affected by this).

**Verified:** confirmed the new implementation issues exactly 7 separate
single-date requests with no hyphens, correctly finds a game on the one
simulated day that has one, and skips the rest without error. Ran the
full `update()` pipeline end to end with this fix in place -- a
last-week finished game correctly appears in `recent_games` and gets
selected over an available upcoming game, matching the intended
priority order. Re-ran the full test-mode regression -- still passes.

## Real bug: yellow win-highlight box looked incomplete only when the winner was on the left

Confirmed via a real screenshot: the away-side (left) yellow box looked
like it wasn't filling its space, while the home-side (right) one looked
fine, despite both being coded as the exact same 13px width. Root cause:
both boxes were trimmed from their own right edge, but the away zone's
adjacent stroke (toward FINAL) is on ITS right, while the home zone's
adjacent stroke is on ITS left. The same "trim from the right" rule
therefore put the away box's gap right next to the FINAL stroke -- the
most visually prominent spot, dead center of the display -- while the
home box's gap landed on the far side near the logo block, barely
noticeable, purely as a coincidence of which side of the screen each
zone happens to sit on.

**Fix:** trim the away box from its LEFT edge instead (now `x34-46`
rather than `x32-44`), so it stays flush against its own adjacent stroke
exactly like the home side already does against its. Same width, same
concept, just mirrored to match which side each zone's stroke is
actually on.

**Verified:** rendered both a winner-on-left and a winner-on-right game
and confirmed via direct pixel inspection that both yellow boxes now
extend all the way to the edge nearest FINAL (x46 and x81 respectively).
Re-ran the full test-mode regression -- still passes.

## Follow-up: away score centering was still off after the box-trim fix

Confirmed via a real screenshot: after fixing the yellow box's trim
direction (previous entry), the score NUMBER's centering math was never
updated to match -- it was still centering within the OLD zone bounds
(`32`, width 15, with an extra -1 shift) from before the box moved to
`x34-46`. Result: 5px of padding on the right vs 1px on the left,
exactly matching the user's own estimate ("looks like 3px" off).

**Fix:** center directly within the box's actual current bounds (`34`,
width 13) with no extra shift needed. Verified by measuring true ink
extent across the full glyph height (not just one row -- an earlier
measurement attempt scanning a single row gave a misleading result,
since a digit like "2"'s glyph shape has different lit columns at
different row heights) -- confirmed exactly 3px/3px, matching the home
side. Re-ran the full test-mode regression -- still passes.

## Real bug (partially reverted below): "FINAL/OT" rendered as "FINAL 0T" -- missing slash glyph

Confirmed via a real screenshot of an actual overtime game (GB 20,
NYJ 17): the title read "FINAL 0T" instead of "FINAL/OT". Root cause:
our own bitmap `FONT` dict never had a `/` glyph at all. `_draw_char`
silently does nothing for an unrecognized character, but the caller
still advances the cursor by the default width regardless -- so the `/`
became an invisible blank gap, and "FINAL" + gap + "OT" read as
"FINAL 0T" at this pixel size (capital O reads as 0 that small).

Added a real 3-wide diagonal slash glyph to `FONT` -- this part stands;
the glyph is still used elsewhere for date strings (`M/D` format). The
first estimate of the resulting overflow (31px ink vs a 30px zone) was
wrong -- rechecked with the real `_text_ink_width()` function instead of
hand math and got 32px, not 31px -- and per explicit follow-up request,
rather than fix that overflow, "FINAL/OT" was dropped entirely in favor
of always showing plain "FINAL" (see below).

## "FINAL/OT" dropped entirely -- OT omitted instead of fixing the overflow

Checked using the real `_text_ink_width()` function rather than hand
math: "FINAL/OT" and "FINAL OT" (space instead of slash) both come out
to exactly 32px, 2px wider than the 30px zone -- a space costs the same
advance width as any other character in this font, so dropping the
slash doesn't help fit it. Per explicit request, this now just always
shows "FINAL" (20px, comfortably fits), even in overtime. The slash
glyph added for this stays in `FONT` regardless -- it's used elsewhere
for date strings (`M/D` format).

## Live priority overhaul: show ANY live game, not just favorites

Previous behavior: the live mode rotated through whatever was in
`live_games` with favorites merely sorted to the front of that same
list -- so a favorite team playing live would still share rotation time
with every other live game, and with no favorite configured or playing,
there was no cross-mode signal telling the core to stay on live mode at
all instead of cycling to recent/upcoming on its normal schedule
regardless of whether something was actually live.

**Explicitly requested behavior:**
- A favorite team playing live -> show ONLY that game, no rotation to
  others.
- No favorite configured, or favorites configured but none currently
  live -> show every live game, rotating between them.
- Whenever ANY game is live at all (favorite or not) -> the display
  should stay on live mode rather than cycling away to recent/upcoming
  on schedule regardless of content (e.g. Thursday/Monday Night
  Football, usually the only live game, should hold there instead of
  briefly showing recent/upcoming every ~15s in between).

**Fix, two parts:**
1. Inside `display()`'s live-mode game selection: if any favorite team is
   currently live, `games` narrows to ONLY the favorite's live game(s)
   before the existing rotation logic runs -- with no favorite live,
   `games` stays as the full live list, rotating normally (unchanged
   from before).
2. Added `has_live_content()` -- a real `BasePlugin` framework hook,
   confirmed from baseball's own current source: the display controller
   checks this (alongside `has_live_priority()`, already implemented by
   BasePlugin itself from the existing `live_priority` config toggle) to
   decide whether to stay on a plugin's live mode instead of rotating
   away on schedule. Deliberately differs from baseball's own version
   here: baseball only returns `True` for a favorite specifically live;
   this plugin returns `True` whenever ANY game is live at all, matching
   the explicitly requested "any time there is a live game going on"
   behavior rather than baseball's favorite-only one.

**Verified** with five scenarios covering every case described: a single
live game with no favorite (shown, `has_live_content()` True), multiple
live games with no favorite (rotates through more than one over
simulated time), multiple live games with a favorite playing (shows
ONLY the favorite's game across every sampled time offset), a favorite
configured but not currently playing (falls back to showing/rotating all
live games normally), and no live games at all (`has_live_content()`
False, `display()` returns False). Re-ran the full test-mode regression
-- still passes.

## Live priority follow-up: has_live_content() alone wasn't enough

Confirmed via explicit report: even with `has_live_content()` in place,
the display still showed the live game and then cycled into recent
results anyway. Real cause: that hook apparently only affects
cross-PLUGIN priority (whether the core stays on this plugin instead of
switching to a different one) -- it doesn't stop the core from still
cycling through THIS plugin's own three declared modes on their normal
schedule. Recent/upcoming honestly returning `True` (since they had
real content) was enough for the core to show them on their turn,
regardless of live content existing elsewhere in the same plugin.

**Fix:** recent/upcoming now explicitly return `False` -- refusing to
show anything at all -- whenever `self.live_games` is non-empty,
regardless of favorites, per explicit request ("I want the live game to
always be prioritized even if it is not my favorite team"). Once
`live_games` empties out (the live game(s) end), recent/upcoming
immediately resume normally.

**Verified:** reproduced the exact reported scenario (live game active,
recent AND upcoming both also populated, no favorite configured) --
confirmed live returns `True` while recent and upcoming both correctly
return `False`. Confirmed recent/upcoming resume returning `True` again
once `live_games` is emptied, so this doesn't permanently break them,
only suppresses them while something is actually live. Re-ran the full
test-mode regression -- still passes (that path doesn't go through
`display_mode` at all, so it's unaffected by this change).

## Real bug: logo cache broke plugin updates with a Permission denied error

Confirmed via real hardware: updating the plugin failed with `Failed to
remove logo_cache/college-football_ARS.png: Permission denied`. Root
cause: downloaded (non-bundled) team logos were being cached to disk
inside the plugin's OWN install folder
(`os.path.join(PLUGIN_DIR, "logo_cache")`). Files this plugin's own
runtime process wrote there ended up with permissions/ownership the
update process (running as a different user, or at a different point in
the permission chain) couldn't clean up during its own removal step.

Re-checked baseball's current source for comparison: it never writes
downloaded logos to disk at all -- only ever caches them in memory. This
plugin's disk-caching was a deviation from that proven pattern, and it's
what created this real deployment problem.

**Fix:** moved the logo disk cache to the system temp directory
(`tempfile.gettempdir()/nfl-college-scoreboard-logos`) instead of inside
the plugin's own folder -- completely outside anywhere a plugin
update/reinstall would ever need to touch, so it can't conflict again.
Also wrapped the cache directory creation in its own try/except (it
wasn't before), so any future issue with this cache degrades gracefully
(falls back to no cached logo, not a crash) rather than ever taking down
the broader fetch pipeline with it.

**Verified:** confirmed the resulting path is `/tmp/
nfl-college-scoreboard-logos`, genuinely outside the plugin's own
install directory. Re-ran the full test-mode regression -- still passes.

**Separately noted, not yet fixed:** `show_all_live` exists in
`config_schema.json` but isn't referenced anywhere in the current
`manager.py` at all -- a leftover from before the full architecture
rewrite. Not currently causing any problem (favorite-exclusive live
priority is already applied unconditionally, which happens to match
what this setting implies it should do), but it's a dead, misleading
config option that should either be wired up or removed for clarity.

## Real root cause, finally found (in two rounds): MSU's live game was never in the fetch at all

After the git-update deadlock was cleared and this project's own
diagnostic logging (`Live games in {league}: ...`, added earlier)
actually reached real hardware, it gave a definitive answer: on a real
Saturday with MSU vs Nebraska kicked off on schedule and confirmed
in-progress by the clock, the live college-football list was `OU@UGA,
MIS@FLA, UTA@ISU, IOW@MIC, HOU@GAS, WIS@PSU` -- six real live games,
correctly extracted, and MSU's game simply wasn't among them. Not a
matching bug, not an abbreviation mismatch -- ESPN's response to this
plugin's own request genuinely didn't include it.

**Round 1 (confirmed WRONG on real hardware, not just incomplete):**
theorized the cause was an unset `limit` letting ESPN's default result
count silently truncate a large slate, and added `limit=300`. This was
tested and confirmed deployed successfully -- and the exact same 6-game
list persisted, unchanged, with MSU's game still completely absent.
`limit` alone did nothing.

**Round 2 (the actual fix):** confirmed via multiple independent sources
that ESPN's college-football scoreboard endpoint specifically defaults
to a much smaller subset of games regardless of `limit` -- one source
states outright that it "only outputs top 25 events by default." The
real fix is a separate `groups` parameter -- `groups=80` is ESPN's ID
for the entire FBS division. `limit` only controls how many results
come back *within* whatever default grouping is already applied; it
does nothing to expand which games are eligible for inclusion in the
first place. NFL has no such concept (one league, no conference/division
grouping to filter by) and was never affected by this -- college
football's 130+ FBS teams across many conferences is exactly the case
ESPN's undocumented default group excludes much of. Added `groups=80`
(scoped to `college-football` only) to both `_fetch_league_scoreboard()`
and `_fetch_recent_lookback()`, since both hit the same endpoint and the
lookback's per-day college-football queries could suffer the identical
truncation for past results.

**Verified:** confirmed via direct inspection of the actual request
parameters sent -- `college-football` now carries `groups=80` on both
fetch methods, `nfl` correctly does not (meaningless for a single
league, scoped out to avoid confusion). Re-ran the full test-mode
regression -- still passes.

**Still needs verification on a real live game with a favorite
playing** -- this addresses the specific, confirmed gap the diagnostic
logging exposed (a real game genuinely missing from ESPN's response),
but hasn't yet been tested against another real live game to confirm
MSU's (or any other non-Top-25 favorite's) game now actually appears.

## Real bug: timeouts always showed 3 regardless of actual remaining count

Confirmed via explicit user report during the same live MSU game: Nebraska
had 1 timeout left, but the display showed all 3 bars lit. Direct proof
that `away.get("timeouts", 3)`/`home.get("timeouts", 3)` never actually
found the field in ESPN's real response -- the `3` default was silently
firing on every single game, which matched this project's own earlier
"NOT CONFIRMED" caveat on this exact field.

Researched the more likely real location after this report: multiple
independent sources point to `situation.homeTimeouts`/
`situation.awayTimeouts` (not per-competitor), and one confirms this
exact field is known to be unreliable for college football specifically
-- "timeouts remaining were only fixed... for NFL games in-progress;
college games still don't work."

**Fix:** switched extraction to `situation.get("awayTimeouts")`/
`situation.get("homeTimeouts")`, defaulting to `None` (not `3`, and not
the old per-competitor path) when genuinely absent. Rendering now skips
the timeout indicator entirely when the value is `None`, rather than
crash on `idx < None` or display a value that's likely wrong either way.

**Verified:** confirmed `dict.get()` correctly returns `None` (not the
old default) when a key exists with a `None` value, so this propagates
cleanly from extraction through to rendering with no other code changes
needed. Rendered a game with `away_timeouts=None`/`home_timeouts=1`
directly -- no crash, and the real value renders correctly.

**Not yet confirmed:** whether `situation.homeTimeouts`/`awayTimeouts`
is itself reliably populated by ESPN, especially for college football
given the source above -- this is the best-available fix given the
evidence, but the underlying field may simply be unreliable at the
API level regardless of what this plugin does.

## Real bug: team logos invisible against their own background color

Confirmed via explicit user report, specifically named for Michigan
State, Iowa, and Utah: the logo was nearly impossible to see against its
background. Root cause: the background behind each logo is filled with
that team's own color (for team identity/branding), and a logo's
dominant color frequently matches that team's own color BY DESIGN --
MSU's green helmet on a green background, Iowa's black-and-gold on
black, Utah's red on red. No tuning of which background color gets
picked fixes this in general, since the match is intentional; the
background and the logo are supposed to share that color for brand
identity, which is exactly what defeats simple contrast at this pixel
size.

**Fix:** added a neutral ellipse backdrop behind the logo specifically
(not the whole box, so the team's color still shows around the edges) --
white by default, switching to black when the team's own color is
already light (average brightness > 175), so a light/whitish team logo
doesn't hit the identical problem in reverse. Sized to the logo's own
fitted dimensions plus a small margin.

**Verified:** rendered two synthetic test cases with an exact
color-for-color match between a fake logo and its team background (dark
green, matching MSU's actual reported problem, and a light/whitish
color to test the reverse case) -- confirmed via direct visual
inspection that the backdrop makes the previously-invisible logo clearly
visible in both directions. Re-ran the full test-mode regression --
still passes.

## Feature: "END Q1"/"END Q2" display at end of quarter

Per explicit request. Confirmed real ESPN value (not guessed):
`status.type.name` is `"STATUS_END_PERIOD"` at the end of a quarter,
alongside other known real values for the same field like
`"STATUS_IN_PROGRESS"`/`"STATUS_HALFTIME"`/`"STATUS_FINAL"`.

**Implementation:** extraction now sets `is_end_of_period` from this
field. The live info row's rendering checks it and, when true, replaces
the normal period+clock display (`"Q2" "2:14"`) with `"END Q2"` --
clock is dropped since it would just read "0:00" at this point, which
is redundant once already saying "END".

**Verified:** fed a realistic mocked ESPN event with
`status.type.name = "STATUS_END_PERIOD"` and `period = 2` through the
real extraction -- confirmed `is_end_of_period` comes out `True`.
Rendered the resulting game dict directly -- "END Q2" displays correctly
in place of the normal period/clock text. Re-ran the full test-mode
regression -- still passes.

## Follow-up: suppress trailing field-position text at end of period

Per explicit request: the down/distance and field-position (team
abbreviation + yard line) text next to "END Q1"/"END Q2" wasn't wanted --
neither is meaningful once the quarter has ended and there's no active
play happening. Both are now suppressed whenever `is_end_of_period` is
true, showing just "END Q2" alone. Verified via direct render.

## Corrected: no 3-vs-4-letter abbreviation issue exists

Initially reported as some teams (MSU, NEB) having 3-letter
abbreviations while others (MICH, IOWA) have 4, colliding with the
score. Checked this project's own extraction code first: `team_abbr()`
has always sliced to `[:3]` -- every abbreviation is already capped at
3 characters, confirmed directly from this project's own earlier
diagnostic logs (`IOW@MIC`, not `IOWA`/`MICH`) and from the user
double-checking the real display (UTA/MIS/IOW/MIC, all 3 letters). There
was no 3-vs-4-letter discrepancy to fix.

There IS a real, much smaller inconsistency: this plugin's bitmap font
renders "N" 4px wide instead of the normal 3px (needed for its diagonal
stroke to read correctly), so a 3-letter abbreviation containing an N
(NEB, MIN, IND, etc.) is about 1px wider than one without (UTA, MIS,
IOW, MIC). The score position was hardcoded to a fixed `x=40` regardless
-- harmless for non-N abbreviations, but 1px too tight for N-containing
ones.

**Fix:** score position now derives from wherever the abbreviation loop
actually finished (plus a small gap), instead of a fixed x. This
self-corrects for any width variance regardless of its cause, not just
the N-glyph case specifically.

**Verified:** rendered UTA/MIC/IOW (no N) against NEB (has N) and
confirmed via direct pixel inspection that NEB's content correctly
spans 1px further right than the others, eliminating the inconsistency.
Re-ran the full test-mode regression -- still passes.

## Corrected again: the truncation to 3 characters was itself the bug

Follow-up to the previous entry: clarified that the actual request was
to STOP forcing every abbreviation down to 3 characters at all -- teams
that are natively 4 characters in ESPN's own data (IOWA, MICH, NAVY,
ARMY, etc.) should show all 4, not get cut down to IOW/MIC/NAV/ARM.

Found this truncation baked into 5 separate places in `manager.py`, all
slicing to `[:3]` -- the main extraction (`team_abbr()`), both
`_draw_scorebug_layout` team-dict constructions, both
`_draw_recent_layout`/`_draw_upcoming_layout` local abbreviation
variables, and the field-position fallback in `_parse_possession_text`.
Changed all 5 to `[:4]`, matching ESPN's own convention for football
(never longer than 4).

**Verified:** confirmed via direct extraction test that "IOWA" and
"MICH" now come through as full 4-character strings, not truncated.
Tested the worst realistic case directly -- a 4-character abbreviation
that ALSO contains the wide N glyph (NAVY) plus a 2-digit score --
and confirmed via direct pixel inspection it still fits with a 2px
margin before the team-stack divider (the dynamic score-position fix
from the previous entry is what makes this safe: it was always
computing from the abbreviation's actual rendered width, so removing
the length cap didn't require any additional layout change). Also
re-ran the full test-mode regression -- still passes.

## Real bug: possession icon collided with the score for wide abbreviation/score combinations

Confirmed via explicit user report: a 4-letter abbreviation combined
with a double-digit score broke the possession icon. Root cause: the
icon was still hardcoded at a fixed `x=49` -- left over from when
abbreviations were forcibly truncated to 3 characters and the score
always started at a fixed position, so `x=49` was always safely past
both. Once abbreviations were allowed their real 4-character length
(the previous fix) and the score's own position became dynamic (the
fix before that), a wide combination -- e.g. NAVY, which is both 4
characters and contains the extra-wide N glyph -- plus a 2-digit score
could push the score text's actual end past x=49, so the icon would
draw directly on top of the score's last digit.

**Fix:** the icon's position now derives from wherever the score
actually finished (plus a small gap), the same self-correcting approach
already used for the score's own position relative to the abbreviation.

**Verified:** rendered the worst realistic combination directly --
NAVY, score 21, possession true -- and confirmed via direct pixel
inspection that the score text ends at x=52 and the icon now starts
cleanly at x=56, no overlap. Visual render confirms it reads correctly.
Re-ran the full test-mode regression -- still passes.

## Logo contrast fix, revised: darkened team color instead of a white/black backdrop

The white/black ellipse backdrop (previous entry) was functionally
correct but disliked stylistically -- an unrelated neutral shape
breaking the team's own color identity. Replaced per explicit request:
back to a solid team-color box exactly like the original design, but
darkened (`color * 0.55`, clamped at 0) rather than left at full
brightness. The logo itself stays at its original brightness, so
darkening only the background creates a real gap between them without
introducing a foreign color -- same hue, just visibly darker, preserving
team identity instead of overriding it.

**Verified, including an honest limitation:** re-rendered the same
synthetic MSU case as before (logo color exactly matching its team
background) -- confirmed the logo is now visibly distinguishable against
the darkened background, subtler than the white backdrop but present,
matching the requested look. Also tested the genuine worst case directly
-- a near-black logo on an already-black team color -- and confirmed
contrast is weak there, since darkening an already-dark color doesn't
create much separation. Real team logos generally have lighter accent
elements even on dark primary colors (Iowa's real logo includes gold,
not solid black), so this is more a synthetic worst case than a likely
real one, but it's a genuine limit of this approach worth knowing rather
than glossing over. Re-ran the full test-mode regression -- still
passes.

## Logo contrast: white fallback for near-black team colors

Per explicit follow-up request, addressing the honest limitation flagged
in the previous entry: teams whose own color is already very dark (true
black, very dark navy) fall back to a plain white background instead of
a darkened version of their own color, since darkening an already-dark
color doesn't create meaningful separation. Most teams still get the
darkened-own-color treatment -- this fallback only applies below a
brightness floor.

**Real bug caught before shipping, not after:** an initial threshold of
60 was tested against MSU's own color brightness (~50.7) and would have
incorrectly caught it too, even though MSU's darkened treatment was
already confirmed working visually in the previous fix. Lowered to 25
so only genuinely near-black colors trigger the white fallback, not
MSU's darker green.

**Verified:** re-rendered MSU (brightness ~50.7) and confirmed it still
correctly uses the darkened-green treatment, not white. Re-rendered a
near-black team color (0,0,0) and confirmed it now correctly falls back
to a clean white background with the logo clearly visible. Re-ran the
full test-mode regression -- still passes.

## Real bug: absolute worst-case info row overflowed the display

Found by direct request to stress-test the right side: period "Q1" + a
full "15:00" clock + a long down/distance like "4TH&25" + a 4-letter
team abbreviation ("IOWA") in the field position. Measured (not
estimated) via the real `_text_ink_width()` function: this combination
needs 79px but only 70px is available, a confirmed 9px overflow --
visually, "IOWA" got clipped right at the display's edge.

**First fix attempt (superseded below):** tightened two of the fixed
gaps and always capped the field-position team at 3 characters. This
closed 6 of the 9px, but a second direct measurement showed 3px still
overflowing (confirmed visually too -- the yard number's last digit was
still clipped). Rather than tighten spacing further (which would affect
every game, not just this rare combination), asked which trade-off was
preferred.

**Actual fix, per explicit direction:** reverted the universal gap
tightening and the always-3-char cap. Instead, the exact width the
field-position team + yard number would need is computed BEFORE drawing
(via `_text_ink_width`, not guessed), and the team prefix is dropped
entirely (showing just the yard number, e.g. "25" instead of "IOWA 25")
only when that specific game's combination would actually push past the
display's right edge. Every other game keeps the full team name exactly
as before, unaffected.

**Verified:** re-rendered the exact worst case (Q1, 15:00, 4th & 25,
IOWA) and confirmed via direct pixel measurement it now fits with 7px to
spare, team prefix correctly dropped, no clipping. Re-rendered a normal
case (Q4, 2:14, 3rd & 7, BUF) and confirmed it still shows the full
"BUF 43" with room to spare, unaffected by the fix. Re-ran the full
test-mode regression -- still passes.

## Info row redesigned to two lines, replacing the conditional-drop fix

Per explicit follow-up: rather than ever conditionally dropping the
field-position team name (the previous fix), down/distance now moves to
its own line below quarter+time, freeing enough width that field
position never needs special-casing again -- it always shows in full.

**New layout:** row 1 (y=6) is period/clock + field position (team +
yard, always full length). Row 2 (y=14, left-aligned under quarter+time,
not centered under the whole row) is down/distance alone. Confirmed
~16px of vertical space was available between the old single-line info
row and the field strip (which starts at y=27), comfortably enough for
a second text line.

**Verified via the same measurement approach as the fix this replaces:**
re-measured the identical absolute worst case (Q1, 15:00, 4th & 25,
IOWA) -- row 1 (period+clock+full "IOWA 25") now needs at most ~53px
against 70px available (17px to spare), row 2 (just "4TH&25") uses far
less than the full width on its own line. No conditional logic left --
every game always shows the same two-row structure with nothing ever
dropped. Re-rendered a normal case (BUF, 3rd & 7) and the end-of-period
case ("END Q2") to confirm both still look correct with the new row
structure -- end-of-period correctly shows nothing on row 2, since
down/distance is already suppressed entirely in that state. Re-ran the
full test-mode regression -- still passes.

## Real bug: info row could collide with the ball-position yard number

Per explicit request, checked before it caused a visible problem: the
field's ball-position yard number (`_draw_field`) is drawn at y=18. Row
2 (down/distance, from the two-row redesign above) sat at y=14, ending
around y=18-19 -- directly in the yard number's own space. As the ball
approaches either end zone, that number can land horizontally under
this row too, so the two would visibly overlap.

**Fix:** shifted both info rows up 3px (row 1: y=6 -> y=3, row 2:
y=14 -> y=11). Row 2 now ends around y=16, leaving real clearance before
the yard number starts at y=18 instead of running into it.

**Verified:** rendered the specific collision-prone scenario directly --
1st & goal from the 5, ball near the end zone -- and confirmed via
direct pixel inspection there's no actual content clipping at the
display's top edge (an initial check flagged pixels above y=3, but
those turned out to be the team-stack divider line, which spans the
full height by design, not text -- rechecked excluding that column and
confirmed clean). Visual render confirms real separation between
"1ST&GOAL" and the yard number above the ball. Re-ran the full
test-mode regression -- still passes.

## Possession icon redesigned: fixed position in the logo block, white instead of brown

Confirmed via explicit user report: the team-stack possession icon,
positioned dynamically after wherever the score ended (a fix from an
earlier round), could reach x=54 -- the divider between the team stack
and info row -- in the worst-case combination (a 4-character
abbreviation with the wide N glyph, plus a 2-digit score), visibly
running into it.

**Fix, per explicit direction:** the icon no longer depends on
abbreviation/score width at all. It now lives at a fixed position
(x=20-23) on the right side of the logo block itself, with the logo
shifted slightly left within that same block (thumbnailed to a 21px-wide
area instead of the full 25px) to make room. Also changed from brown to
white, for reliable contrast against any team's background color --
brown risked the same problem the earlier logo-contrast fixes addressed,
disappearing against a similarly warm/dark team color.

**Verified:** re-rendered the exact worst case (NAVY, 2-digit score,
possession true) and confirmed via direct pixel inspection the icon sits
at x=20-23, nowhere near the x=54 divider, completely unaffected by the
score's own width. Confirmed the no-possession case correctly shows no
icon, and the no-logo fallback case (flat color swatch) still renders
correctly. Re-rendered a full scorebug layout to confirm everything
looks right together. Re-ran the full test-mode regression -- still
passes.

## Timeouts removed for college football entirely -- confirmed genuine data gap

After the `situation.homeTimeouts`/`awayTimeouts` fix (previous entry)
was deployed, college football timeouts were still confirmed not
working. Asked directly whether this is a real, unfixable data
limitation: yes -- an independent third-party source found during the
original research states this plainly, not just as a guess: "timeouts
remaining were only fixed... for NFL games in-progress; college games
still don't work." That's someone else hitting the identical limitation
at the ESPN API level, independent of this plugin's own field-path
choice -- strong evidence this isn't something fixable by trying yet
another field.

**Fix, per explicit request:** timeouts are now suppressed entirely for
college football -- forced to `None` regardless of what
`situation.awayTimeouts`/`homeTimeouts` happens to return for a given
game -- while NFL keeps the real extraction, since the source only
reported college as broken. The existing rendering fallback (skip the
indicator entirely on `None`, confirmed in an earlier fix) means this
required no rendering changes at all, only the extraction change.

**Verified:** fed the identical mocked event through both leagues --
confirmed NFL still returns the real extracted values (2 and 1 in the
test) while college football is forced to `None` for both, even though
the same mock data included non-null values for both team's timeouts.
Re-ran the full test-mode regression -- still passes.

## Real bug: cross-plugin priority never worked -- a required config key was simply missing

Explicit question asked: does this plugin prevent other plugins from
cycling in while a favorite is live, the way baseball does? Checked
directly rather than assume -- confirmed baseball's own
`config_schema.json` documents that `has_live_content()` (added to this
plugin in an earlier round) is only HALF of what's needed:
`BasePlugin.has_live_priority()` -- already implemented by the framework
itself, not something a plugin overrides -- also has to return true, and
it does so by reading a **top-level** `live_priority` config key.

This plugin's config schema never had one. It only has a
similarly-named `live.live_priority` key nested under `live`, which
controls something entirely different -- favorite-team ordering within
this plugin's OWN live-game selection, not whether the broader system
stays on this plugin at all. Since the top-level key genuinely didn't
exist, `has_live_priority()` had nothing real to read, so cross-plugin
priority could never actually trigger regardless of `has_live_content()`
already working correctly.

**Fix:** added the missing top-level `live_priority` boolean (default
`false`, matching baseball's own opt-in-deliberately default, since it
changes cross-plugin behavior). Also clarified the existing nested
`live.live_priority` description to explicitly distinguish it from the
new top-level one, since the two now have easily-confused similar names.

**Verified:** confirmed the new key sits at the correct top level (not
nested) via direct schema inspection, confirmed the existing nested-key
code path (`live_cfg.get("live_priority", True)`, reading from
`self.config.get("live", {})`) is entirely separate and unaffected,
confirmed the JSON schema is still valid, and re-ran the full test-mode
regression -- still passes. **Not independently verified beyond this**:
whether the real `BasePlugin.has_live_priority()` implementation reads
this exact key name the way baseball's own documentation describes --
that part relies entirely on baseball's own comment being accurate,
since this plugin has no way to inspect the framework's real source
directly.

## Logos enlarged, allowed to extend past their box boundaries

Confirmed via explicit user report: at the previous (21, 16) target
size, several logos were barely legible. Root cause of why they ended
up smaller than either limit: most real logos aren't a perfect 21:16
aspect ratio, so `thumbnail()` shrinks to whichever dimension is the
tighter constraint to preserve proportions -- often landing well under
both limits rather than filling either one.

**Fix, per explicit direction:** increased the target size to (28, 24)
and accepted that logos will now often extend past this block's own
boundaries and get clipped -- legibility was explicitly prioritized over
staying perfectly inside the box. `paste_x`/`paste_y` can go negative
now on purpose (centering an image larger than its nominal box); PIL
simply clips anything outside the canvas, which is the intended look.

**Verified:** rendered both a synthetic non-square logo and two real
bundled logos (BUF, KC) at the new size -- confirmed via visual
inspection both are noticeably larger and more detailed/legible than
before, while the abbreviation, score, and possession icon (drawn after
the logo, so they render on top of any overlap) all still display
correctly and readably alongside the now-larger logos. Re-ran the full
test-mode regression -- still passes.

## Real bug: the logo enlargement fix (previous entry) overflowed into the adjacent row

Confirmed via explicit user report: after enlarging logos to (28, 24),
logos were spilling into the OTHER team's row, worse for the bottom
team's logo bleeding upward into the top row. Computed the exact cause
directly rather than guess: each team's row is only 16px tall, and a
24px-tall target centers at `(16-24)//2 = -4` -- a confirmed 4px
overflow into EACH adjacent row, not a subtle rounding issue but a
real, sizable intrusion.

**Fix:** reduced the vertical target to 18px -- `(16-18)//2 = -1`, only
1px of overflow per side now, much less disruptive -- while keeping the
wider 28px horizontal target from the previous fix, since that dimension
wasn't reported as a problem (confirmed: even a 28px-wide logo's right
edge lands around x=24-25, still short of the abbreviation text starting
at x=27).

**Verified:** re-rendered the same two-team stack (BUF/KC) used to
confirm the original enlargement, and confirmed via direct visual
inspection that there's no more visible spillover between the two
rows, while both logos remain noticeably larger than the original
too-small (21, 16) size that started this whole round of fixes. Re-ran
the full test-mode regression -- still passes.

## Suggested next steps

1. ~~Verify yard-line math~~ done above.
2. ~~Wire in logos and team colors~~ done above -- worth a visual check
   once we can render against real team data, though.
3. ~~Build the Recent and Upcoming views.~~ **BUILT.** Composed
   `_RecentDataWorker(Football, SportsRecent)` and
   `_UpcomingDataWorker(Football, SportsUpcoming)` ourselves, mirroring how
   baseball.py composes `BaseballRecent(Baseball, SportsRecent)` -- core's
   football.py just never got the equivalent. Added `_draw_recent_layout()`
   (team stack + FINAL/FINAL-OT + game date) and `_draw_upcoming_layout()`
   (team stack, no score yet + date/time), both reusing `_draw_team_stack()`
   with a new `show_extras=False` mode that skips timeouts/possession
   (neither applies to a game that hasn't started or has already ended).
   **These are first-pass layouts, not yet visually iterated on** the way
   the live layout was -- treat spacing/centering as a starting point.
10. **Found and fixed a bigger core gap than previously known**:
    `_fetch_data()` -- the method every single update loop (`SportsLive`,
    `SportsRecent`, `SportsUpcoming`) calls to get ESPN data -- is *never*
    implemented anywhere in this core repo. Checked baseball, hockey, and
    basketball too; same story everywhere, not football-specific. Without
    an override, every state's games list would have silently stayed empty
    forever -- no error, just nothing ever showing up. Implemented it for
    all three: `_LiveDataWorker` wires it to the existing
    `_fetch_todays_games()`; `_RecentDataWorker`/`_UpcomingDataWorker` wire
    it to `ESPNDataSource.fetch_schedule()` with a 21-day-back window (to
    match `SportsRecent.update()`'s own internal 21-day cutoff) and a
    14-day-forward window, respectively.
11. **Selection priority across states**: `update()` now checks
    live → recent → upcoming in that order and shows the first one with
    any games, applying favorite-team-first ordering within whichever
    state is chosen (across all configured leagues). Verified against a
    hand-built two-league, three-worker-type scenario.
13. **Recent/Upcoming rebuilt to match the baseball plugin exactly.**
    Ported the real font engine from `ledmatrix-tidbyt-baseball` --
    `BDFFont` (bitmap font parser/renderer), bundled `.ttf`/`.bdf` files
    (now in this plugin's own `fonts/` folder), `_load_font`, `_measure`,
    `_render_text`, `_ink_extent`, `_fit_font_for_pair`, `_darken_color`,
    `_text_color_for`, and `_draw_team_column` -- rather than reusing our
    simple fixed bitmap FONT (which the live layout still uses; it wasn't
    touched). `_draw_recent_layout()`/`_draw_upcoming_layout()` are now
    close ports of baseball's `_render_final_game()`/
    `_render_upcoming_game()`: same two-column-plus-grid structure for
    final games (winning team's bar highlighted yellow, box-score grid
    on the right) and same two-tier logo/title/record-bar structure for
    upcoming games. Adapted for football: quarters instead of innings,
    a single "F" (final score) column instead of baseball's R/H/E (no
    hits/errors equivalent in football) -- added quarter-by-quarter
    `home_linescores`/`away_linescores` extraction to `_ExtractionMixin`
    for this, same unconfirmed-but-standard-ESPN-convention caveat as
    baseball's innings had.
14. **Found and fixed a real regression while wiring the port in**: the
    multi-league refactor (composition instead of inheritance, done a few
    steps after logos were first confirmed working) meant our top-level
    plugin no longer inherits `_load_and_resize_logo()` from anywhere --
    logos had been silently falling back to flat color swatches ever
    since that refactor, caught by the existing try/except around every
    call site (no error, just a silent fallback -- confirmed by
    re-rendering and seeing the placeholder circle logos had disappeared
    from the live layout despite no code change to that layout itself).
    Fixed with `_logo_loader_for_league()`, which delegates to whichever
    league worker actually has the method.
4. Wire up rotation across multiple simultaneous live games (currently
   only the top-priority one displays).
5. Get this loaded on the Pi (or in the plugin test harness) against a
   hardcoded sample game JSON to verify the drawing code, then check
   against a real live game once one is available, the same way the
   baseball plugin's edge cases were caught.
