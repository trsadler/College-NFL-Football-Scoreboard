# NFL/College Scoreboard

Custom NFL/NCAA football scoreboard plugin for LEDMatrix. Standalone plugin,
not a fork of the official `football-scoreboard` registry plugin -- built
from scratch to replace its layout while reusing the core project's ESPN
data-fetching. Includes past scores and upcoming games in addition to live
game tracking.

## What's reused vs. custom

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
