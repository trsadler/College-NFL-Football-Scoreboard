"""
NFL/College Scoreboard Plugin for LEDMatrix

Reuses the core project's ESPN data-fetching (src.base_classes.football.FootballLive)
for pulling and normalizing live game data, but completely replaces the built-in
_draw_scorebug_layout() with our own custom pixel-perfect layout:
  - Left: stacked team logos, abbreviation + score (side-by-side), timeout row,
    football possession icon
  - Right: quarter/clock + down&distance + field position info row, simulated
    field with end zones/goal posts/yard lines, and a ball-position indicator
    (football icon + direction arrow + yard number) that slides along the field

Recent (final) and Upcoming (scheduled) games instead reuse the exact font
engine and layout design ported from ledmatrix-tidbyt-baseball -- same BDF/TTF
font loading, ink-extent measurement, dynamic font-fitting, box-score-style
grid (adapted to quarters instead of innings), and two-tier upcoming-game
layout. See _render_final_game()/_render_upcoming_game() below.

API Version: 1.0.0
"""

from typing import Dict, Any, Optional, List, Tuple
import logging
import os
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFont

from src.plugin_system.base_plugin import BasePlugin
from src.base_classes.football import Football, FootballLive
from src.base_classes.sports import SportsRecent, SportsUpcoming
from src.logging_config import get_logger

# --- Font engine (ported verbatim from ledmatrix-tidbyt-baseball) ---------
# Rather than hardcoding a guessed filename, this scans a bundled fonts/
# folder for a real font shipped with the plugin, preferring pixel/arcade
# styles. Team abbreviation/score text is fit dynamically to its column
# width so it can never overflow regardless of which font gets picked up.
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_CHOICES = {
    "5by7": os.path.join(PLUGIN_DIR, "fonts", "5by7_regular.ttf"),
    "4x6": os.path.join(PLUGIN_DIR, "fonts", "4x6-font.ttf"),
    "press_start_2p": os.path.join(PLUGIN_DIR, "fonts", "PressStart2P-Regular.ttf"),
    "tom_thumb": os.path.join(PLUGIN_DIR, "fonts", "tom-thumb.bdf"),
    "system": None,
}

# Font choices backed by a real bitmap format (BDF) rather than a scalable
# TrueType outline -- these render every pixel exactly as designed with zero
# anti-aliasing, and are NOT resizable (BDF is a single fixed pixel size), so
# they skip the shrink-to-fit sizing logic used for the TTF options.
BDF_FONT_CHOICES = {"tom_thumb"}

# Preference order for auto-discovering a bundled font from the main
# LEDMatrix install, used only when font_choice is "system" or the selected
# bundled file is missing for some reason.
FONT_NAME_PREFERENCE = ["press", "pixel", "matrix", "arcade", "8x8", "4x6", "retro"]


class BDFFont:
    """Minimal BDF (Glyph Bitmap Distribution Format) parser and renderer.
    Pillow's ImageFont.truetype() can't load .bdf files at all, and BDF
    glyphs are exact per-pixel bitmaps rather than vector outlines -- so
    drawing them is just copying 1-bit pixel data directly, with no
    rasterization/anti-aliasing step to introduce any softness. Intentionally
    tiny: only implements enough of BDF to render basic Latin text."""

    def __init__(self, path: str):
        self.glyphs: Dict[int, Dict[str, Any]] = {}
        self.ascent = 0
        self.descent = 0
        self._parse(path)

    def _parse(self, path: str):
        with open(path, "r", errors="replace") as f:
            lines = f.read().splitlines()
        i, n = 0, len(lines)
        cur: Optional[Dict[str, Any]] = None
        while i < n:
            line = lines[i].strip()
            if line.startswith("FONT_ASCENT"):
                self.ascent = int(line.split()[1])
            elif line.startswith("FONT_DESCENT"):
                self.descent = int(line.split()[1])
            elif line.startswith("STARTCHAR"):
                cur = {}
            elif line.startswith("ENCODING") and cur is not None:
                cur["encoding"] = int(line.split()[1])
            elif line.startswith("DWIDTH") and cur is not None:
                cur["dwidth"] = int(line.split()[1])
            elif line.startswith("BBX") and cur is not None:
                p = line.split()
                cur["bbw"], cur["bbh"] = int(p[1]), int(p[2])
                cur["bbxoff"], cur["bbyoff"] = int(p[3]), int(p[4])
            elif line.startswith("BITMAP") and cur is not None:
                rows = []
                for _ in range(cur.get("bbh", 0)):
                    i += 1
                    hexrow = lines[i].strip()
                    nbits = len(hexrow) * 4
                    val = int(hexrow, 16) if hexrow else 0
                    bits = [(val >> (nbits - 1 - b)) & 1 for b in range(cur["bbw"])]
                    rows.append(bits)
                cur["rows"] = rows
            elif line.startswith("ENDCHAR") and cur is not None:
                if "encoding" in cur:
                    self.glyphs[cur["encoding"]] = cur
                cur = None
            i += 1

    def _glyph(self, ch: str) -> Optional[Dict[str, Any]]:
        return self.glyphs.get(ord(ch))

    def textbbox(self, text: str) -> Tuple[int, int, int, int]:
        """Mimics ImageDraw.textbbox((0,0), text, font=...) closely enough
        for this plugin's centering/width-fit math."""
        cursor_x = 0
        min_top: Optional[int] = None
        max_bottom: Optional[int] = None
        for ch in text:
            g = self._glyph(ch)
            if g is None:
                cursor_x += 4
                continue
            glyph_top = self.ascent - (g["bbyoff"] + g["bbh"])
            glyph_bottom = glyph_top + g["bbh"]
            min_top = glyph_top if min_top is None else min(min_top, glyph_top)
            max_bottom = glyph_bottom if max_bottom is None else max(max_bottom, glyph_bottom)
            cursor_x += g.get("dwidth", 4)
        if min_top is None:
            min_top, max_bottom = 0, 0
        return (0, min_top, cursor_x, max_bottom)

    def draw(self, image: Image.Image, xy: Tuple[int, int], text: str, fill: Tuple[int, int, int]):
        x0, y0 = xy
        cursor_x = x0
        img_w, img_h = image.size
        for ch in text:
            g = self._glyph(ch)
            if g is None:
                cursor_x += 4
                continue
            glyph_top = self.ascent - (g["bbyoff"] + g["bbh"])
            for row_idx, row in enumerate(g.get("rows", [])):
                py = y0 + glyph_top + row_idx
                if py < 0 or py >= img_h:
                    continue
                for col_idx, bit in enumerate(row):
                    if not bit:
                        continue
                    px = cursor_x + g["bbxoff"] + col_idx
                    if 0 <= px < img_w:
                        image.putpixel((px, py), fill)
            cursor_x += g.get("dwidth", 4)


logger = get_logger(__name__)


# --- Shared bitmap font (3 wide x 5 tall) --------------------------------
# Same font used for team abbreviations, the info row, and (in compact form)
# the yard number on the field.
FONT = {
    # Every glyph below is extracted directly from the real tom-thumb.bdf
    # file bundled with the baseball plugin (same file we copied into our
    # own fonts/ folder) -- not hand-drawn approximations. N keeps the
    # widened 4px diagonal fix (also present in the real file); colon keeps
    # our own deliberately-tightened 1px-effective-width version rather
    # than the real font's wider default spacing, since that tightening
    # was an explicit earlier fix, not an oversight.
    'A': ['010', '101', '111', '101', '101'],
    'B': ['110', '101', '110', '101', '110'],
    'C': ['011', '100', '100', '100', '011'],
    'D': ['110', '101', '101', '101', '110'],
    'E': ['111', '100', '111', '100', '111'],
    'F': ['111', '100', '111', '100', '100'],
    'G': ['011', '100', '111', '101', '011'],
    'H': ['101', '101', '111', '101', '101'],
    'I': ['111', '010', '010', '010', '111'],
    'J': ['001', '001', '001', '101', '010'],
    'K': ['101', '101', '110', '101', '101'],
    'L': ['100', '100', '100', '100', '111'],
    'M': ['101', '111', '111', '101', '101'],
    'N': ['1001', '1101', '1011', '1001', '1001'],
    'O': ['010', '101', '101', '101', '010'],
    'P': ['110', '101', '110', '100', '100'],
    'Q': ['010', '101', '101', '111', '011'],
    'R': ['110', '101', '111', '110', '101'],
    'S': ['011', '100', '010', '001', '110'],
    'T': ['111', '010', '010', '010', '010'],
    'U': ['101', '101', '101', '101', '011'],
    'V': ['101', '101', '101', '010', '010'],
    'W': ['101', '101', '111', '111', '101'],
    'X': ['101', '101', '010', '101', '101'],
    'Y': ['101', '101', '010', '010', '010'],
    'Z': ['111', '001', '010', '100', '111'],
    '0': ['011', '101', '101', '101', '110'],
    '1': ['010', '110', '010', '010', '111'],
    '2': ['110', '001', '010', '100', '111'],
    '3': ['110', '001', '010', '001', '110'],
    '4': ['101', '101', '111', '001', '001'],
    '5': ['111', '100', '110', '001', '110'],
    '6': ['011', '100', '111', '101', '111'],
    '7': ['111', '001', '010', '100', '100'],
    '8': ['111', '101', '111', '101', '111'],
    '9': ['111', '101', '111', '001', '110'],
    '&': ['110', '110', '111', '101', '011'],
    ':': ['000', '010', '000', '010', '000'],
}

FONT_SMALL = {  # compact 3x4, used only for the yard number on the field
    '0': ['111', '101', '101', '111'],
    '1': ['010', '110', '010', '111'],
    '2': ['111', '001', '110', '111'],
    '3': ['111', '011', '001', '111'],
    '4': ['101', '101', '111', '001'],
    '5': ['111', '100', '011', '111'],
    '6': ['111', '100', '111', '111'],
    '7': ['111', '001', '010', '010'],
    '8': ['111', '111', '101', '111'],
    '9': ['111', '111', '001', '111'],
}

FOOTBALL_ICON = ['0011100', '0111110', '1111111', '0111110', '0011100']  # 7x5, pointed tips
ARROW_RIGHT = ['10000', '11000', '11100', '11000', '10000']
ARROW_LEFT = ['00001', '00011', '00111', '00011', '00001']
GOALPOST = ['10001', '10001', '11111', '00100', '00100', '00100']

WHITE = (244, 244, 240)
AMBER = (255, 176, 32)
BROWN = (193, 102, 47)
GOALPOST_YELLOW = (255, 205, 40)
FIELD_GREEN = (30, 110, 60)


class _ExtractionMixin:
    """
    Shared _extract_game_details() override -- adds yard line, possession
    text, and real team colors on top of whatever Football's own extraction
    provides. Used by all three worker types (Live/Recent/Upcoming) so the
    extra fields are available regardless of game state.
    """

    def _extract_game_details(self, game_event: Dict) -> Optional[Dict]:
        details = super()._extract_game_details(game_event)
        if details is None:
            return None
        try:
            situation = game_event["competitions"][0].get("situation") or {}
            # ESPN gives yardLine as 0-100 from the *possessing* team's own goal
            # line, plus a human string like "TEX 35". Grab both -- we compute
            # our own absolute field position from yardLine + possession side.
            details["yard_line"] = situation.get("yardLine")
            details["possession_text"] = situation.get("possessionText", "")
            details["is_redzone"] = situation.get("isRedZone", False)
        except Exception:
            details["yard_line"] = None
            details["possession_text"] = ""

        try:
            competitors = game_event["competitions"][0]["competitors"]
            home_team = next(c for c in competitors if c.get("homeAway") == "home")
            away_team = next(c for c in competitors if c.get("homeAway") == "away")
            # ESPN's team object carries real hex team colors (verified against
            # a live boxscore response, e.g. "color": "061642", "alternateColor":
            # "bc945c") -- use `color` as primary, falling back to
            # `alternateColor` if the primary is missing or pure white/black
            # (some teams' primary color is white, which reads poorly as a
            # solid end zone fill on a black-background LED display).
            details["home_color"] = _hex_to_rgb(
                home_team["team"].get("color"), home_team["team"].get("alternateColor")
            )
            details["away_color"] = _hex_to_rgb(
                away_team["team"].get("color"), away_team["team"].get("alternateColor")
            )

            # Quarter-by-quarter scores -- football's equivalent of baseball's
            # per-inning linescores, used by the ported box-score grid on the
            # final-game layout. ESPN's `linescores` is a documented field
            # (same convention across sports: a list of {"value": N} per
            # period), unconfirmed specifically for football the way it was
            # for baseball, so treat a missing/malformed array as "no data"
            # (blank cells) rather than guessing zeros.
            def parse_linescores(team):
                raw = team.get("linescores")
                if not isinstance(raw, list):
                    return []
                out = []
                for entry in raw:
                    try:
                        out.append(int(entry.get("value")))
                    except (AttributeError, TypeError, ValueError):
                        out.append(None)
                return out

            details["home_linescores"] = parse_linescores(home_team)
            details["away_linescores"] = parse_linescores(away_team)
        except Exception:
            details["home_color"] = (60, 60, 60)
            details["away_color"] = (60, 60, 60)
            details["home_linescores"] = []
            details["away_linescores"] = []

        try:
            # ESPN's scoreboard-level `leaders` (confirmed to exist per
            # competition, with passing/rushing/receiving categories each
            # naming one athlete + a team reference) -- combined across
            # BOTH teams (one top passer/rusher/receiver for the whole game,
            # not per team), so which team each one belongs to has to be
            # checked via the `team` reference rather than assumed.
            #
            # NOT YET VERIFIED: the exact contents of `displayValue` (e.g.
            # whether it includes completions/attempts and TDs, or just
            # yards) -- extracting it verbatim rather than assuming a
            # specific format, so double check this against a real
            # finished game before trusting the on-screen wording.
            raw_leaders = game_event["competitions"][0].get("leaders", [])
            leaders = []
            for category in raw_leaders:
                for leader in category.get("leaders", []):
                    athlete = leader.get("athlete", {})
                    team_ref = leader.get("team", {})
                    leaders.append({
                        "category": category.get("name", ""),
                        "team_id": team_ref.get("id") if isinstance(team_ref, dict) else None,
                        "name": athlete.get("shortName") or athlete.get("displayName", ""),
                        "display_value": leader.get("displayValue", ""),
                    })
            details["leaders"] = leaders
        except Exception:
            details["leaders"] = []

        # Tag which league this game came from, so the plugin can tell games
        # from different workers apart once they're merged into one list.
        details["league"] = self.league
        return details


class _LiveDataWorker(_ExtractionMixin, FootballLive):
    """
    One instance per configured league, for currently-in-progress games.

    IMPORTANT FINDING: _fetch_data() is *never* implemented anywhere in this
    core repo -- not for football, not for baseball, hockey, or basketball
    either (checked all of them). SportsLive.update() calls self._fetch_data()
    expecting a dict with an "events" key, but the only definition anywhere
    is SportsCore's abstract `pass`. So without this override, live_games
    would silently stay empty forever, fetch failures and all -- there'd be
    no error, just nothing ever showing up. This isn't football-specific;
    it'd need adding for any sport built on this core.
    """

    def _fetch_data(self) -> Optional[Dict]:
        # _fetch_todays_games() already exists on SportsCore and builds the
        # correct ESPN URL from self.sport/self.league -- it just needed
        # something to actually call it.
        return self._fetch_todays_games()


class _RecentDataWorker(_ExtractionMixin, Football, SportsRecent):
    """
    One instance per configured league, for completed (final) games.

    Composed the same way baseball.py composes BaseballRecent(Baseball,
    SportsRecent) -- football.py just never got the equivalent class, so we
    build it here instead of in core.
    """

    def _fetch_data(self) -> Optional[Dict]:
        # SportsRecent.update() itself filters to a 21-day-back cutoff, so
        # the fetch window needs to be at least that wide or there'd be
        # nothing for that filter to find.
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=21)
        events = self.data_source.fetch_schedule(self.sport, self.league, (start, now))
        return {"events": events}


class _UpcomingDataWorker(_ExtractionMixin, Football, SportsUpcoming):
    """
    One instance per configured league, for scheduled-but-not-started games.
    Same story as _RecentDataWorker -- composed here since football.py has
    no equivalent to baseball's pattern.
    """

    def _fetch_data(self) -> Optional[Dict]:
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=14)
        events = self.data_source.fetch_schedule(self.sport, self.league, (now, end))
        return {"events": events}


def _hex_to_rgb(primary: Optional[str], fallback: Optional[str]) -> tuple:
    """
    Convert an ESPN hex color string ("061642") to an RGB tuple, preferring
    `primary` unless it's missing or too close to white/black to read as a
    fill color on our display, in which case fall back to `alternateColor`.
    """
    def parse(hex_str):
        if not hex_str:
            return None
        hex_str = hex_str.lstrip('#')
        if len(hex_str) != 6:
            return None
        try:
            return tuple(int(hex_str[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return None

    for candidate in (primary, fallback):
        rgb = parse(candidate)
        if rgb is None:
            continue
        brightness = sum(rgb) / 3
        if 20 < brightness < 235:  # not near-black, not near-white
            return rgb
    return (60, 60, 60)  # gray fallback if both colors are unusable/missing


class NFLCollegeScoreboardPlugin(BasePlugin):
    """
    Standalone football scoreboard plugin with a fully custom layout.

    Owns one _LeagueDataWorker per configured league (see config's
    `leagues` list) for data/fetch state, and implements its own
    _draw_scorebug_layout() -- the exact pixel design worked out earlier --
    for whichever game gets selected across all of them.
    """

    # ESPN's URL slug for a league (used to build the scoreboard/summary
    # fetch URL) is NOT the same string the core project uses internally for
    # logo directories and per-sport config keys (verified against
    # src/logo_downloader.py's LOGO_DIRECTORIES dict and
    # src/base_classes/sports.py's `mode_config = config.get(f"{sport_key}_scoreboard")`).
    # This maps the former to the latter.
    LEAGUE_TO_SPORT_KEY = {
        "nfl": "nfl",
        "college-football": "ncaa_fb",
    }

    def __init__(self, plugin_id: str, config: Dict[str, Any],
                 display_manager: Any, cache_manager: Any, plugin_manager: Any):
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)

        leagues = config.get("leagues") or ["nfl"]
        self.live_workers: Dict[str, _LiveDataWorker] = {}
        self.recent_workers: Dict[str, _RecentDataWorker] = {}
        self.upcoming_workers: Dict[str, _UpcomingDataWorker] = {}

        for league in leagues:
            sport_key = self.LEAGUE_TO_SPORT_KEY.get(league)
            if sport_key is None:
                self.logger.warning("Unknown league '%s' in config, skipping", league)
                continue

            worker_config = self._build_worker_config(sport_key)
            for worker_dict, worker_cls in (
                (self.live_workers, _LiveDataWorker),
                (self.recent_workers, _RecentDataWorker),
                (self.upcoming_workers, _UpcomingDataWorker),
            ):
                worker = worker_cls(
                    worker_config, display_manager, cache_manager, self.logger, sport_key=sport_key
                )
                # Football.__init__ sets self.sport = "football" but never sets
                # self.league -- and SportsCore's fetch methods build the actual
                # ESPN URL as f".../sports/{self.sport}/{self.league}/...", so
                # without this the URL would be missing its league segment
                # entirely (".../sports/football//scoreboard").
                worker.league = league
                worker_dict[league] = worker

        self.current_game: Optional[Dict] = None
        self.current_state: Optional[str] = None  # "live" | "recent" | "upcoming"

        # --- Font engine state (ported from baseball plugin) ---
        # Used only by _render_final_game()/_render_upcoming_game() -- the
        # live layout keeps its own hand-rolled bitmap FONT/_draw_char.
        self._font_cache: Dict[Tuple[str, int], Any] = {}
        self._fit_font_cache: Dict[Any, Any] = {}
        self.font_choice = "tom_thumb"
        self._repo_font_path = self._discover_repo_font()
        self.font_small = self._load_font(9)
        self.font_tiny = self._load_font(7)

    def _discover_repo_font(self) -> Optional[str]:
        """Scans assets/fonts/ (relative to the LEDMatrix install root) for
        a real bundled font instead of guessing a filename. Prefers anything
        that looks like a pixel/arcade font so team text matches the
        aesthetic the rest of the project's plugins use."""
        fonts_dir = "assets/fonts"
        if not os.path.isdir(fonts_dir):
            return None
        try:
            candidates = [f for f in os.listdir(fonts_dir) if f.lower().endswith((".ttf", ".otf"))]
        except Exception:
            return None
        if not candidates:
            return None
        for pref in FONT_NAME_PREFERENCE:
            for f in candidates:
                if pref in f.lower():
                    return os.path.join(fonts_dir, f)
        return os.path.join(fonts_dir, candidates[0])

    def _load_font(self, size: int, bold: bool = False) -> Any:
        cache_key = (self.font_choice, size)
        if cache_key in self._font_cache:
            return self._font_cache[cache_key]

        if self.font_choice in BDF_FONT_CHOICES:
            bdf_key = (self.font_choice, "bdf")
            if bdf_key in self._font_cache:
                font = self._font_cache[bdf_key]
            else:
                bdf_path = FONT_CHOICES[self.font_choice]
                try:
                    font = BDFFont(bdf_path)
                except Exception as e:
                    self.logger.error(f"Failed to parse BDF font at {bdf_path}: {e}", exc_info=True)
                    font = None
                self._font_cache[bdf_key] = font
            if font is not None:
                self._font_cache[cache_key] = font
                return font

        candidates = []
        bundled_path = FONT_CHOICES.get(self.font_choice)
        if bundled_path and os.path.isfile(bundled_path) and self.font_choice not in BDF_FONT_CHOICES:
            candidates.append(bundled_path)

        if self._repo_font_path:
            candidates.append(self._repo_font_path)

        for choice, path in FONT_CHOICES.items():
            if choice in BDF_FONT_CHOICES or choice == self.font_choice or path is None:
                continue
            if os.path.isfile(path):
                candidates.append(path)

        if bold:
            candidates += [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
            ]
        else:
            candidates.append("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")

        font = None
        for path in candidates:
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
        if font is None:
            self.logger.error(
                f"ALL font candidates failed to load for size={size}, bold={bold}: "
                f"{candidates}. Falling back to PIL's built-in default bitmap font."
            )
            font = ImageFont.load_default()

        self._font_cache[cache_key] = font
        return font

    def _measure(self, font: Any, text: str) -> Tuple[int, int, int, int]:
        """Unified text bounding-box measurement for either a BDFFont or a
        normal PIL font, so the rest of the code doesn't need to care which
        one is active."""
        if isinstance(font, BDFFont):
            return font.textbbox(text)
        tmp_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        return tmp_draw.textbbox((0, 0), text, font=font)

    def _render_text(self, image: Image.Image, xy: Tuple[int, int], text: str, font: Any, fill: Tuple[int, int, int]):
        """Unified text drawing for either a BDFFont (direct pixel writes,
        no anti-aliasing) or a normal PIL font (draw.text)."""
        if isinstance(font, BDFFont):
            font.draw(image, xy, text, fill)
        else:
            ImageDraw.Draw(image).text(xy, text, font=font, fill=fill)

    def _ink_extent(self, font: Any, text: str) -> Tuple[int, int]:
        """Renders `text` to a small scratch image and returns the actual
        leftmost/rightmost columns containing ink, as opposed to the font's
        nominal advance width (which for punctuation like ':' often includes
        several columns of blank design space)."""
        bbox = self._measure(font, text)
        w = max(bbox[2] - bbox[0], 1) + 6
        h = max(bbox[3] - bbox[1], 1) + 6
        scratch = Image.new("RGB", (w, h), (0, 0, 0))
        self._render_text(scratch, (3, 3), text, font, (255, 255, 255))
        cols = [x for x in range(w) for y in range(h) if scratch.getpixel((x, y)) != (0, 0, 0)]
        if not cols:
            return (3, 3)
        return (min(cols), max(cols))

    def _draw_line_tightened(self, image: Image.Image, xy: Tuple[int, int], font: Any,
                              fill: Tuple[int, int, int], text: str, ink_gap: int = 2) -> int:
        """
        Draws `text` word-by-word with only `ink_gap` real pixels between
        each word's actual rendered ink, instead of the font's normal space
        character advance -- same concept as baseball's _draw_tight_join,
        generalized to any number of words rather than just two. This is
        what actually fixes "J.  Goff" or "YDS   3" reading as too spaced
        out: the gap isn't extra space added between words, it's blank
        design space a tiny font's space glyph (and narrow glyphs like
        commas) leaves for normal-width spacing, which looks disproportionate
        at this pixel scale. Returns the total pixel width used.
        """
        x, y = xy
        cursor_x = x
        tokens = [t for t in text.split(" ") if t]
        for i, token in enumerate(tokens):
            left, right = self._ink_extent(font, token)
            draw_x = cursor_x if i == 0 else cursor_x - (left - 3)
            self._render_text(image, (draw_x, y), token, font, fill)
            cursor_x = draw_x + (right - 3) + 1 + ink_gap
        return cursor_x - x - ink_gap if tokens else 0

    def _fit_font_for_pair(self, draw, text_a: str, text_b: str, max_width: int, start_size: int, min_size: int = 4) -> Any:
        """Sizes for whichever of the two strings is wider, so both team
        columns render at the SAME font size rather than each shrinking
        independently based on its own text length."""
        candidate = self._load_font(start_size, bold=True)
        if isinstance(candidate, BDFFont):
            return candidate

        cache_key = (self.font_choice, text_a, text_b, max_width)
        if cache_key in self._fit_font_cache:
            return self._fit_font_cache[cache_key]

        size = start_size
        chosen = None
        while size >= min_size:
            font = self._load_font(size, bold=True)
            bbox_a = self._measure(font, text_a)
            bbox_b = self._measure(font, text_b)
            widest = max(bbox_a[2] - bbox_a[0], bbox_b[2] - bbox_b[0])
            if widest <= max_width:
                chosen = font
                break
            size -= 1
        if chosen is None:
            chosen = self._load_font(min_size, bold=True)

        self._fit_font_cache[cache_key] = chosen
        return chosen

    @staticmethod
    def _darken_color(color: Tuple[int, int, int], min_channel: int = 15) -> Tuple[int, int, int]:
        return tuple(max(c // 2, min_channel) for c in color)

    @staticmethod
    def _text_color_for(bg: Tuple[int, int, int]) -> Tuple[int, int, int]:
        luminance = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
        return (0, 0, 0) if luminance > 150 else (255, 255, 255)

    def _draw_team_column(self, image, draw, x0, y0, w, h, abbr, score, logo, text_color, bg_color, font,
                          bar_color_override=None, show_score=True):
        """Logo fills nearly the whole column; a darkened bar across the
        bottom holds the bold 'ABBR SCORE' text so it stays legible over the
        logo. `font` is computed once by the caller from BOTH columns' text,
        so the two teams always render at the same size.

        `bar_color_override`: used for final (completed) games to highlight
        the winning team's bar in yellow instead of its normal team color.
        `show_score`: set False for upcoming games, which don't have a score
        yet -- shows just the abbreviation."""
        text_line = f"{abbr} {score}" if show_score else abbr
        line_bbox = self._measure(font, text_line)
        line_h = line_bbox[3] - line_bbox[1]
        line_w = line_bbox[2] - line_bbox[0]
        bar_h = line_h + 4

        if logo is not None:
            logo_x = x0 + (w - logo.width) // 2
            logo_y = y0 + (h - logo.height) // 2
            image.paste(logo, (logo_x, logo_y), logo)

        bar_y0 = y0 + h - bar_h
        bar_color = bar_color_override if bar_color_override is not None else bg_color
        draw.rectangle([x0, bar_y0, x0 + w - 1, y0 + h - 1], fill=bar_color)

        tx = x0 + max((w - line_w) // 2, 0)
        tx = min(tx, x0 + w - line_w) if line_w < w else x0
        ty = bar_y0 + max((bar_h - line_h) // 2, 0) - line_bbox[1]
        self._render_text(image, (tx, ty), text_line, font, text_color)

    def _build_worker_config(self, sport_key: str) -> Dict[str, Any]:
        """
        Translate our config schema (leagues/favorite_teams/live.*/upcoming.*/
        recent.*/show_records/etc.) into the shape SportsCore/FootballLive
        expects: everything namespaced under f"{sport_key}_scoreboard", with
        its own specific key names (live_update_interval, live_game_duration,
        recent_update_interval, upcoming_update_interval, ...).
        """
        cfg = self.config
        live_cfg = cfg.get("live", {})
        upcoming_cfg = cfg.get("upcoming", {})
        recent_cfg = cfg.get("recent", {})

        mode_config = {
            "enabled": cfg.get("enabled", True),
            "favorite_teams": cfg.get("favorite_teams", []),
            "show_favorite_teams_only": cfg.get("show_favorite_teams_only", False),
            "show_all_live": live_cfg.get("show_all_live", True),
            "live_update_interval": live_cfg.get("update_interval_seconds", 15),
            "live_game_duration": live_cfg.get("game_duration_seconds", 20),
            "upcoming_update_interval": upcoming_cfg.get("update_interval_seconds", 3600),
            "upcoming_games_to_show": upcoming_cfg.get("games_to_show", 10),
            "recent_update_interval": recent_cfg.get("update_interval_seconds", 3600),
            "recent_games_to_show": recent_cfg.get("games_to_show", 5),
            "show_records": cfg.get("show_records", False),
            "show_ranking": cfg.get("show_ranking", False),
            "show_odds": cfg.get("show_odds", False),
            "test_mode": cfg.get("test_mode", False),
        }
        return {f"{sport_key}_scoreboard": mode_config}

    @staticmethod
    def _favorite_first(games: List[Dict], favorite_teams: List[str]) -> List[Dict]:
        if not favorite_teams:
            return games
        def is_favorite(g):
            return g.get("home_abbr") in favorite_teams or g.get("away_abbr") in favorite_teams
        return sorted(games, key=lambda g: not is_favorite(g))  # favorites first, stable otherwise

    # -------------------------------------------------------------------
    # BasePlugin interface
    # -------------------------------------------------------------------
    # -------------------------------------------------------------------
    # Test mode: preview sample data for any view directly on real
    # hardware, without needing an actual live/recent/upcoming game.
    # -------------------------------------------------------------------
    # Real NFL logos (pulled from the actual LEDMatrix core repo's
    # assets/sports/nfl_logos/, bundled here in test_logos/ just for these
    # test-mode previews) -- not used for real games, which pull live from
    # ESPN via the existing _load_and_resize_logo()/download_missing_logo()
    # pipeline once real network access is available on the Pi.
    _TEST_LOGO_DIR = os.path.join(PLUGIN_DIR, "test_logos")

    _TEST_LIVE_GAME = {
        "league": "nfl",
        "away_abbr": "BUF", "away_id": "2", "away_score": "17",
        "away_color": (0, 51, 141),
        "away_logo_path": os.path.join(_TEST_LOGO_DIR, "BUF.png"), "away_logo_url": None,
        "away_timeouts": 2,
        "home_abbr": "KC", "home_id": "12", "home_score": "20",
        "home_color": (227, 24, 55),
        "home_logo_path": os.path.join(_TEST_LOGO_DIR, "KC.png"), "home_logo_url": None,
        "home_timeouts": 3,
        "period_text": "Q4", "clock": "2:14", "down_distance_text": "3rd & 7",
        "possession_indicator": "away", "possession_text": "BUF 43",
        "yard_line": 43, "is_redzone": False,
    }
    _TEST_RECENT_GAME = {
        "league": "nfl",
        "away_abbr": "DAL", "away_id": "6", "away_score": "24",
        "away_color": (0, 34, 68),
        "away_logo_path": os.path.join(_TEST_LOGO_DIR, "DAL.png"), "away_logo_url": None,
        "home_abbr": "DET", "home_id": "8", "home_score": "34",
        "home_color": (0, 118, 182),
        "home_logo_path": os.path.join(_TEST_LOGO_DIR, "DET.png"), "home_logo_url": None,
        "period": 4, "is_final": True, "game_date": "11/30",
        "leaders": [
            {"category": "passingYards", "team_id": "8", "name": "J. Goff", "display_value": "19/23, 258 YDS, 3 TD"},
            {"category": "rushingYards", "team_id": "8", "name": "J. Gibbs", "display_value": "12 CAR, 54 YDS, 2 TD"},
        ],
    }
    _TEST_UPCOMING_GAME = {
        "league": "nfl",
        "away_abbr": "GB", "away_id": "9", "away_score": "0",
        "away_color": (24, 48, 40),
        "away_logo_path": os.path.join(_TEST_LOGO_DIR, "GB.png"), "away_logo_url": None,
        "away_record": "5-3",
        "home_abbr": "CHI", "home_id": "3", "home_score": "0",
        "home_color": (11, 22, 42),
        "home_logo_path": os.path.join(_TEST_LOGO_DIR, "CHI.png"), "home_logo_url": None,
        "home_record": "4-4",
        "game_date": "SUN 9/14", "game_time": "1:00PM",
    }


    def _update_test_mode(self, view: str) -> None:
        """
        Serve hardcoded sample data instead of touching ESPN at all. `view`
        is one of "live"/"recent"/"upcoming" (always shows that one) or
        "all" (cycles through all three, switching every display_duration
        seconds using a simple wall-clock modulo -- no extra state needed).
        """
        import time as _time

        if view == "all":
            cycle_len = max(self.config.get("display_duration", 15), 1)
            index = int(_time.time() // cycle_len) % 3
            view = ["live", "recent", "upcoming"][index]

        if view == "recent":
            self.current_game, self.current_state = self._TEST_RECENT_GAME, "recent"
        elif view == "upcoming":
            self.current_game, self.current_state = self._TEST_UPCOMING_GAME, "upcoming"
        else:
            self.current_game, self.current_state = self._TEST_LIVE_GAME, "live"

    def update(self) -> None:
        test_cfg = self.config.get("test_mode", {})
        if test_cfg.get("enabled", False):
            self._update_test_mode(test_cfg.get("view", "live"))
            return

        favorite_teams = self.config.get("favorite_teams", [])
        live_cfg = self.config.get("live", {})
        recent_cfg = self.config.get("recent", {})
        upcoming_cfg = self.config.get("upcoming", {})

        # Priority: live > recent > upcoming -- a live game is always more
        # interesting than a completed or not-yet-started one. Within each
        # state, favorite teams are moved to the front.
        #
        # TODO: this shows only the single top-priority game in each state;
        # rotating through *all* live games (or all recent/upcoming ones)
        # over their configured game_duration_seconds is still a stub.

        if live_cfg.get("enabled", True):
            all_live: List[Dict] = []
            for worker in self.live_workers.values():
                try:
                    worker.update()
                except Exception as e:
                    self.logger.error(f"Error updating live worker for '{worker.league}': {e}", exc_info=True)
                    continue
                all_live.extend(getattr(worker, "live_games", None) or [])
            if all_live:
                if live_cfg.get("live_priority", True):
                    all_live = self._favorite_first(all_live, favorite_teams)
                self.current_game = all_live[0]
                self.current_state = "live"
                return

        if recent_cfg.get("enabled", True):
            all_recent: List[Dict] = []
            for worker in self.recent_workers.values():
                try:
                    worker.update()
                except Exception as e:
                    self.logger.error(f"Error updating recent worker for '{worker.league}': {e}", exc_info=True)
                    continue
                all_recent.extend(getattr(worker, "games_list", None) or [])
            if all_recent:
                all_recent = self._favorite_first(all_recent, favorite_teams)
                self.current_game = all_recent[0]
                self.current_state = "recent"
                return

        if upcoming_cfg.get("enabled", True):
            all_upcoming: List[Dict] = []
            for worker in self.upcoming_workers.values():
                try:
                    worker.update()
                except Exception as e:
                    self.logger.error(f"Error updating upcoming worker for '{worker.league}': {e}", exc_info=True)
                    continue
                all_upcoming.extend(getattr(worker, "games_list", None) or [])
            if all_upcoming:
                all_upcoming = self._favorite_first(all_upcoming, favorite_teams)
                self.current_game = all_upcoming[0]
                self.current_state = "upcoming"
                return

        self.current_game = None
        self.current_state = None

    def display(self, force_clear: bool = False) -> None:
        if self.current_game is None:
            return
        if self.current_state == "live":
            self._draw_scorebug_layout(self.current_game, force_clear=force_clear)
        elif self.current_state == "recent":
            self._draw_recent_layout(self.current_game, force_clear=force_clear)
        elif self.current_state == "upcoming":
            self._draw_upcoming_layout(self.current_game, force_clear=force_clear)

    # -------------------------------------------------------------------
    # Rendering: our own layout, not inherited from anywhere.
    # -------------------------------------------------------------------
    def _draw_scorebug_layout(self, game: Dict, force_clear: bool = False) -> None:
        try:
            width = self.display_manager.width
            height = self.display_manager.height
            img = Image.new('RGB', (width, height), (0, 0, 0))
            draw = ImageDraw.Draw(img)

            teams = self._teams_from_game(game)
            self._draw_team_stack(img, draw, teams)
            self._draw_divider(draw)
            self._draw_field(draw, teams, game)
            self._draw_info_row(draw, game)

            self.display_manager.image.paste(img, (0, 0))
            self.display_manager.update_display()
        except Exception as e:
            self.logger.error(f"Error drawing custom football layout: {e}", exc_info=True)

    # -- helpers -----------------------------------------------------------

    def _teams_from_game(self, game: Dict) -> List[Dict]:
        """Map the normalized ESPN `game` dict onto our two-row team model."""
        possession = game.get("possession_indicator")  # "home" | "away" | None
        league = game.get("league")
        return [
            {
                "abbr": game.get("away_abbr", "")[:3].upper(),
                "score": str(game.get("away_score", 0)),
                "color": game.get("away_color", (60, 60, 60)),
                "possession": possession == "away",
                "timeouts": game.get("away_timeouts", 3),
                "side": "away",
                "team_id": game.get("away_id"),
                "logo_path": game.get("away_logo_path"),
                "logo_url": game.get("away_logo_url"),
                "league": league,
            },
            {
                "abbr": game.get("home_abbr", "")[:3].upper(),
                "score": str(game.get("home_score", 0)),
                "color": game.get("home_color", (60, 60, 60)),
                "possession": possession == "home",
                "timeouts": game.get("home_timeouts", 3),
                "side": "home",
                "team_id": game.get("home_id"),
                "logo_path": game.get("home_logo_path"),
                "logo_url": game.get("home_logo_url"),
                "league": league,
            },
        ]

    def _logo_loader_for_league(self, league: Optional[str]):
        """
        Returns whichever worker instance for this league actually has
        _load_and_resize_logo() (inherited from SportsCore) -- our top-level
        plugin doesn't inherit it directly since the multi-league refactor
        made the plugin OWN workers rather than BE one. Without this, logo
        loading would silently fail (caught by the try/except around every
        call site) and fall back to flat color swatches -- which is exactly
        what happened until this was added; confirmed by re-rendering
        before/after and seeing the placeholder circle logos disappear/
        reappear.
        """
        for workers in (self.live_workers, self.recent_workers, self.upcoming_workers):
            worker = workers.get(league)
            if worker is not None:
                return worker
        return None

    # -- Recent (final score) layout ----------------------------------------

    def _draw_recent_layout(self, game: Dict, force_clear: bool = False) -> None:
        """
        Top row (y0-14, 15px tall -- one row shorter than the live layout so
        the horizontal stroke below it doesn't touch the stats), each
        team's half filled CONTINUOUSLY with its color from edge to stroke
        -- logo+abbreviation+score all sit on one unbroken color field:
          x0-31    away logo + abbreviation, on away_team color
          x32-45   away score, still on away_team color (or YELLOW if away
                    won -- yellow replaces the team color for just this
                    sub-region, nothing else changes)
          x46-47   2px white stroke
          x48-79   "FINAL" (or "FINAL/OT"), centered
          x80-81   2px white stroke
          x82-95   home score, on home_team color (or YELLOW if home won)
          x96-127  home abbreviation + logo, on home_team color

        The score zones are intentionally narrower (14px) than the middle
        FINAL zone (32px, up from an earlier 24px) -- FINAL's actual ink is
        19px wide regardless of zone size, so there's an unavoidable 1px
        left/right imbalance (19 is odd, can't split evenly), but widening
        its zone makes that 1px proportionally minor instead of noticeable.

        Both halves always touch their adjacent stroke directly regardless
        of who won -- that symmetry is what actually fixes the "blocks look
        different sizes" problem. (An earlier attempt inserted black gaps
        instead, which was wrong: it made the LOSING side stop short of its
        stroke while the WINNING side's yellow still touched it, so the two
        sides still read as different sizes just for a different reason.)

        NOT YET VERIFIED: the exact wording/format of each stat line
        depends on ESPN's `leaders[].leaders[].displayValue`, which hasn't
        been checked against a real finished game -- see the extraction
        comment in _ExtractionMixin.
        """
        try:
            width = self.display_manager.width
            height = self.display_manager.height
            image = Image.new('RGB', (width, height), (0, 0, 0))
            draw = ImageDraw.Draw(image)

            TOP_H = 15  # rows 0-14

            away_score = int(game.get("away_score", 0) or 0)
            home_score = int(game.get("home_score", 0) or 0)
            away_won = away_score > home_score
            home_won = home_score > away_score
            YELLOW = (255, 220, 0)
            BLACK = (0, 0, 0)
            STROKE = (255, 255, 255)

            teams = self._teams_from_game(game)
            away_team = next(t for t in teams if t["side"] == "away")
            home_team = next(t for t in teams if t["side"] == "home")

            def load_logo(team):
                if team.get("logo_path") is None:
                    return None
                loader = self._logo_loader_for_league(team.get("league"))
                if loader is None:
                    return None
                try:
                    return loader._load_and_resize_logo(
                        team["team_id"], team["abbr"], team["logo_path"], team.get("logo_url")
                    )
                except Exception as e:
                    self.logger.debug(f"Logo load failed for {team['abbr']}: {e}")
                    return None

            # --- Left half (x0-49): continuous away_team color ---
            draw.rectangle([0, 0, 46, TOP_H - 1], fill=away_team["color"])
            logo = load_logo(away_team)
            if logo is not None:
                fitted = logo.copy()
                fitted.thumbnail((16, TOP_H), Image.Resampling.LANCZOS)
                image.paste(fitted, (1 + (16 - fitted.width) // 2, (TOP_H - fitted.height) // 2), fitted)
            x = 19
            for ch in away_team["abbr"]:
                self._draw_char(draw, FONT, ch, x, 5, WHITE)
                x += self._char_adv(ch)

            def centered_x(text: str, zone_x0: int, zone_w: int) -> int:
                """
                Starting x that centers `text` (drawn with our 3-wide-glyph,
                4px-per-char bitmap FONT) within [zone_x0, zone_x0+zone_w).
                When the leftover space is odd, the extra pixel goes to the
                LEFT margin -- this was previously going right (plain floor
                division), which is what made FINAL and both score numbers
                all read as having more space on their right than their left.
                """
                n = len(text)
                ink_w = self._text_ink_width(text)
                leftover = zone_w - ink_w
                left_margin = (leftover + 1) // 2  # ceil, instead of floor
                return zone_x0 + max(left_margin, 0)

            # Yellow replaces the team color for just the score sub-region
            # if this team won -- everything else stays continuous.
            # Zone widened from 14 to 15px -- the stroke moved 1px inward
            # (toward FINAL) below, and this zone absorbs the freed column.
            score_x0 = centered_x(away_team["score"], 32, 15) - 1  # 1px left, per request
            if away_won:
                # Yellow box trimmed 1px off its right edge -- doesn't span
                # the full zone width anymore, per request.
                draw.rectangle([32, 0, 44, TOP_H - 1], fill=YELLOW)
                score_color = BLACK
            else:
                score_color = WHITE
            x = score_x0
            for ch in away_team["score"]:
                self._draw_char(draw, FONT, ch, x, 5, score_color)
                x += self._char_adv(ch)

            # --- Stroke, FINAL, stroke ---
            # Both strokes moved 1px inward (toward FINAL), per request --
            # FINAL's zone shrinks by 2px total (1 each side); the away/home
            # score zones each grow by 1px to absorb their freed column.
            draw.rectangle([47, 0, 48, TOP_H - 1], fill=STROKE)

            period = game.get("period", 0)
            title = "FINAL/OT" if (period and period > 4) else "FINAL"
            x = centered_x(title, 49, 30)  # middle zone is now x49-78
            for ch in title:
                self._draw_char(draw, FONT, ch, x, 5, YELLOW)
                x += self._char_adv(ch)

            draw.rectangle([79, 0, 80, TOP_H - 1], fill=STROKE)

            # --- Right half (x81-127): continuous home_team color, mirrored ---
            draw.rectangle([81, 0, 127, TOP_H - 1], fill=home_team["color"])

            score_x0 = centered_x(home_team["score"], 81, 15) - 1  # 1px left
            if home_won:
                draw.rectangle([81, 0, 93, TOP_H - 1], fill=YELLOW)  # trimmed 2px off right edge total
                score_color = BLACK
            else:
                score_color = WHITE
            x = score_x0
            for ch in home_team["score"]:
                self._draw_char(draw, FONT, ch, x, 5, score_color)
                x += self._char_adv(ch)

            home_abbr_w = self._text_ink_width(home_team["abbr"])
            x = 109 - home_abbr_w
            for ch in home_team["abbr"]:
                self._draw_char(draw, FONT, ch, x, 5, WHITE)
                x += self._char_adv(ch)
            logo = load_logo(home_team)
            if logo is not None:
                fitted = logo.copy()
                fitted.thumbnail((16, TOP_H), Image.Resampling.LANCZOS)
                image.paste(fitted, (111 + (16 - fitted.width) // 2, (TOP_H - fitted.height) // 2), fitted)

            # --- Horizontal stroke, with a clear gap before the stats below ---
            draw.rectangle([0, TOP_H, width - 1, TOP_H + 1], fill=STROKE)  # y15-16

            # --- Bottom: top-performer stat lines, full width ---
            winner_id = game.get("home_id") if home_won else game.get("away_id") if away_won else None
            leaders = [l for l in (game.get("leaders") or []) if l.get("team_id") == winner_id]
            y = 18  # TOP_H(15) + stroke(2) + 1px gap = 18
            NAME_TO_STAT_GAP = 5  # more than the 2px used within each part
            for leader in leaders[:2]:
                name = leader.get("name", "")
                stat = leader.get("display_value", "")
                name_w = self._draw_line_tightened(image, (2, y), self.font_tiny, WHITE, name, ink_gap=2)
                self._draw_line_tightened(image, (2 + name_w + NAME_TO_STAT_GAP, y), self.font_tiny, WHITE, stat, ink_gap=2)
                y += 7

            self.display_manager.image.paste(image, (0, 0))
            self.display_manager.update_display()
        except Exception as e:
            self.logger.error(f"Error drawing recent-game layout: {e}", exc_info=True)

    # -- Upcoming (scheduled game) layout ------------------------------------

    def _draw_upcoming_layout(self, game: Dict, force_clear: bool = False) -> None:
        """
        Ported from ledmatrix-tidbyt-baseball's _render_upcoming_game(),
        same two-tier structure: top tier has away logo (left) / "UPCOMING"
        + date/time (center) / home logo (right); bottom tier is a
        full-width color bar split at the center, abbreviation + record
        side by side under each logo.
        """
        try:
            width = self.display_manager.width
            height = self.display_manager.height
            image = Image.new("RGB", (width, height), (0, 0, 0))
            draw = ImageDraw.Draw(image)

            away_color = game.get("away_color", (60, 60, 60))
            home_color = game.get("home_color", (60, 60, 60))
            away_abbr = game.get("away_abbr", "")[:3].upper()
            home_abbr = game.get("home_abbr", "")[:3].upper()
            away_record = game.get("away_record", "")
            home_record = game.get("home_record", "")

            bar_line_h = self._measure(self.font_tiny, "0")[3]
            bar_h = bar_line_h + 4
            top_h = height - bar_h
            mid_x = width // 2
            side_w = 41

            away_txt_color = self._text_color_for(away_color)
            home_txt_color = self._text_color_for(home_color)

            bar_y0 = height - bar_h
            draw.rectangle([0, bar_y0, mid_x - 1, height - 1], fill=away_color)
            draw.rectangle([mid_x, bar_y0, width - 1, height - 1], fill=home_color)

            def draw_bar_text(is_away, abbr, record, text_color):
                abbr_bbox = self._measure(self.font_tiny, abbr)
                abbr_w = abbr_bbox[2] - abbr_bbox[0]
                ty = bar_y0 + max((bar_h - bar_line_h) // 2, 0) - abbr_bbox[1] + 1

                if is_away:
                    abbr_x = max((side_w - abbr_w) // 2, 0)
                else:
                    abbr_x = (width - side_w) + max((side_w - abbr_w) // 2, 0)
                self._render_text(image, (abbr_x, ty), abbr, self.font_tiny, text_color)

                if record:
                    rec_bbox = self._measure(self.font_tiny, record)
                    rec_w = rec_bbox[2] - rec_bbox[0]
                    gap = 4
                    if is_away:
                        rec_x = abbr_x + abbr_w + gap
                        rec_x = min(rec_x, mid_x - 2 - rec_w)
                    else:
                        rec_x = abbr_x - gap - rec_w
                        rec_x = max(rec_x, mid_x + 3)
                    self._render_text(image, (rec_x, ty), record, self.font_tiny, text_color)

            draw_bar_text(True, away_abbr, away_record, away_txt_color)
            draw_bar_text(False, home_abbr, home_record, home_txt_color)

            middle_w = width - side_w * 2
            middle_x0 = side_w

            draw.rectangle([0, 0, side_w - 1, top_h - 1], fill=self._darken_color(away_color))
            draw.rectangle([width - side_w, 0, width - 1, top_h - 1], fill=self._darken_color(home_color))

            away_logo = self._safe_load_logo(game, "away")
            if away_logo is not None:
                fitted = away_logo.copy()
                fitted.thumbnail((side_w, top_h), Image.Resampling.LANCZOS)
                logo_x = (side_w - fitted.width) // 2
                logo_y = max((top_h - fitted.height) // 2, 0)
                image.paste(fitted, (logo_x, logo_y), fitted)

            home_logo = self._safe_load_logo(game, "home")
            if home_logo is not None:
                fitted = home_logo.copy()
                fitted.thumbnail((side_w, top_h), Image.Resampling.LANCZOS)
                logo_x = (width - side_w) + (side_w - fitted.width) // 2
                logo_y = max((top_h - fitted.height) // 2, 0)
                image.paste(fitted, (logo_x, logo_y), fitted)

            def ink_centered_x(font, text, x0, w):
                left, right = self._ink_extent(font, text)
                ink_w = right - left + 1
                target_ink_x0 = x0 + max((w - ink_w) // 2, 0)
                return target_ink_x0 - left + 3

            title_font = self._load_font(8, bold=True)
            title = "UPCOMING"
            tbbox = self._measure(title_font, title)
            tx = ink_centered_x(title_font, title, middle_x0, middle_w)
            ty = 2 - tbbox[1]
            self._render_text(image, (tx, ty), title, title_font, (255, 255, 255))

            info_font = self._load_font(7, bold=False)
            date_str = game.get("game_date")
            time_str = game.get("game_time")
            cursor_y = ty + (tbbox[3] - tbbox[1]) + 3

            for line in filter(None, [date_str, time_str]):
                lbbox = self._measure(info_font, line)
                lx = ink_centered_x(info_font, line, middle_x0, middle_w)
                ly = cursor_y - lbbox[1]
                self._render_text(image, (lx, ly), line, info_font, (200, 200, 200))
                cursor_y += (lbbox[3] - lbbox[1]) + 1

            STROKE = (255, 255, 255)
            stroke_w = 2
            draw.rectangle([side_w, 0, side_w + stroke_w - 1, top_h - 1], fill=STROKE)
            draw.rectangle([width - side_w - stroke_w, 0, width - side_w - 1, top_h - 1], fill=STROKE)
            draw.rectangle([0, top_h, width - 1, top_h + stroke_w - 1], fill=STROKE)
            draw.rectangle([mid_x, top_h, mid_x + stroke_w - 1, height - 1], fill=STROKE)

            self.display_manager.image.paste(image, (0, 0))
            self.display_manager.update_display()
        except Exception as e:
            self.logger.error(f"Error drawing upcoming-game layout: {e}", exc_info=True)

    def _safe_load_logo(self, game: Dict, side: str):
        """Best-effort logo load for the ported layouts, using the same
        SportsCore._load_and_resize_logo() pipeline the team stack uses.
        Delegates to whichever league worker actually has that method --
        see _logo_loader_for_league()."""
        team_id = game.get(f"{side}_id")
        abbr = game.get(f"{side}_abbr", "")
        logo_path = game.get(f"{side}_logo_path")
        logo_url = game.get(f"{side}_logo_url")
        if logo_path is None:
            return None
        loader = self._logo_loader_for_league(game.get("league"))
        if loader is None:
            return None
        try:
            return loader._load_and_resize_logo(team_id, abbr, logo_path, logo_url)
        except Exception as e:
            self.logger.debug(f"Logo load failed for {abbr}: {e}")
            return None

    def _draw_char(self, draw, font, ch, x, y, color, width=None):
        bits = font.get(ch)
        if not bits:
            return
        for row, bitrow in enumerate(bits):
            for col, bit in enumerate(bitrow):
                if width is not None and col >= width:
                    break
                if bit == '1':
                    draw.point((x + col, y + row), fill=color)

    @staticmethod
    def _char_adv(ch: str) -> int:
        """
        Horizontal advance for one character of our bitmap FONT: glyph width
        + 1px gap. Almost everything is 3px wide (-> advance 4), but N is a
        real diagonal at 4px wide (-> advance 5) -- same fix baseball
        applied to its BDF font, needed here too since our own bitmap FONT
        had the identical "N looks like M" bug independently (they differed
        by only one row). Reads the glyph's own width rather than assuming
        3, so this doesn't need updating again if another glyph ever needs
        to widen the same way.
        """
        bits = FONT.get(ch)
        glyph_w = len(bits[0]) if bits else 3
        return glyph_w + 1

    def _text_ink_width(self, text: str) -> int:
        """
        True rendered width of `text` in our bitmap FONT: sum of each
        character's advance, minus the final trailing 1px gap (nothing
        follows the last character). Any width calculation that instead
        assumes a fixed 4px/char -- which was true before N was widened to
        4px+5 advance -- undercounts by 1px for every N in the string, and
        was throwing off both the centering math (centered_x) and the
        right-aligned home abbreviation. Use this instead of a hardcoded
        `len(text) * 4 - 1` anywhere text might contain an N.
        """
        if not text:
            return 0
        return sum(self._char_adv(ch) for ch in text) - 1

    def _draw_team_stack(self, img, draw, teams, show_extras: bool = True):
        for i, team in enumerate(teams):
            y_base = i * 16

            logo = None
            if team.get("logo_path") is not None:
                loader = self._logo_loader_for_league(team.get("league"))
                if loader is not None:
                    try:
                        logo = loader._load_and_resize_logo(
                            team["team_id"], team["abbr"], team["logo_path"], team.get("logo_url")
                        )
                    except Exception as e:
                        self.logger.debug(f"Logo load failed for {team['abbr']}: {e}")

            if logo is not None:
                # _load_and_resize_logo thumbnails to up to 1.5x display size,
                # not our specific 25x16 box -- fit it down to that box,
                # preserving aspect ratio, and center it on a plain background
                # of the team's color so mismatched logo aspect ratios don't
                # leave an awkward black gap.
                draw.rectangle([0, y_base, 24, y_base + 15], fill=team["color"])
                fitted = logo.copy()
                fitted.thumbnail((25, 16), Image.Resampling.LANCZOS)
                paste_x = (25 - fitted.width) // 2
                paste_y = y_base + (16 - fitted.height) // 2
                img.paste(fitted, (paste_x, paste_y), fitted)
            else:
                # Fallback: flat color swatch if the logo couldn't be loaded
                # (missing/failed download -- SportsCore's loader already
                # tries to create a placeholder logo file in that case, so
                # this is a last-resort, not the expected common path)
                draw.rectangle([0, y_base, 24, y_base + 15], fill=team["color"])

            # Abbreviation + score, side by side
            x = 27
            for ch in team["abbr"]:
                self._draw_char(draw, FONT, ch, x, y_base + 5, WHITE)
                x += self._char_adv(ch)
            x = 40
            score_color = team.get("score_color", AMBER)
            for ch in team["score"]:
                self._draw_char(draw, FONT, ch, x, y_base + 5, score_color)
                x += self._char_adv(ch)

            if not show_extras:
                continue  # Recent/Upcoming games have no possession or timeouts

            # Football possession icon (small version, 4x3, reused from the
            # earlier mockup's possession indicator)
            if team["possession"]:
                small_football = ['0110', '1111', '0110']
                for row, bitrow in enumerate(small_football):
                    for col, bit in enumerate(bitrow):
                        if bit == '1':
                            draw.point((49 + col, y_base + 5 + row), fill=BROWN)

            # Timeout row: horizontal, left-aligned with abbreviation
            segments = [(27, 29), (31, 33), (35, 37)]
            for idx, (sx, ex) in enumerate(segments):
                color = WHITE if idx < team["timeouts"] else None
                if color:
                    draw.line([(sx, y_base + 12), (ex, y_base + 12)], fill=color)

    def _draw_divider(self, draw):
        draw.line([(54, 0), (54, 31)], fill=(200, 205, 200))

    def _draw_field(self, draw, teams, game):
        # End zones + goal lines + yard lines
        draw.rectangle([55, 27, 61, 31], fill=teams[0]["color"])   # left end zone
        draw.line([(62, 27), (62, 31)], fill=WHITE)                # left goal line
        draw.rectangle([122, 27, 127, 31], fill=teams[1]["color"])  # right end zone
        draw.line([(121, 27), (121, 31)], fill=WHITE)              # right goal line
        for i in range(9):
            x = 67 + i * 6
            draw.line([(x, 27), (x, 31)], fill=WHITE)
        for x in range(63, 121):
            for y in range(27, 32):
                if draw.im.getpixel((x, y)) == (0, 0, 0):
                    draw.point((x, y), fill=FIELD_GREEN)

        # Goal posts
        for gx0, color in ((56, GOALPOST_YELLOW), (122, GOALPOST_YELLOW)):
            for row, bitrow in enumerate(GOALPOST):
                for col, bit in enumerate(bitrow):
                    if bit == '1':
                        draw.point((gx0 + col, 21 + row), fill=color)

        # Ball position indicator
        #
        # VERIFIED against a real ESPN play object: {"distance": 13, "yardLine": 43,
        # "possessionText": "BUF 43", "yardsToEndzone": 57}. Since 43 + 57 = 100,
        # `yardLine` is confirmed to run 0-100 measured from the POSSESSING team's
        # own goal line (not a fixed left/right scale). Our display has a fixed
        # layout though: teams[0] (away) always owns the LEFT end zone, teams[1]
        # (home) always owns the RIGHT end zone (see _teams_from_game/_draw_field).
        # So the pixel math has to branch on which team currently has the ball:
        #   - away has it: their own goal line IS the left edge, so yard_line
        #     maps directly left-to-right, and they're driving right.
        #   - home has it: their own goal line is the RIGHT edge, so yard_line
        #     counts down from the right edge, and they're driving left.
        yard_line = game.get("yard_line")
        possession_side = game.get("possession_indicator")
        if yard_line is None or possession_side is None:
            return  # nothing to show pre-snap/no possession data

        if possession_side == 'away':
            ball_x = round(61 + yard_line * 0.6)
            direction = 'right'
        else:
            ball_x = round(121 - yard_line * 0.6)
            direction = 'left'

        icon_h = 5
        icon_y0 = 22
        if direction == 'right':
            icon_x0 = ball_x - 3
            arrow_x0 = icon_x0 + 7 + 1
            arrow = ARROW_RIGHT
        else:
            arrow_x0 = ball_x - 3 - 1 - 5
            icon_x0 = arrow_x0 + 5 + 1
            arrow = ARROW_LEFT

        for row, bitrow in enumerate(FOOTBALL_ICON):
            for col, bit in enumerate(bitrow):
                if bit == '1':
                    color = WHITE if (row == 2 and col in (2, 3, 4)) else BROWN
                    draw.point((icon_x0 + col, icon_y0 + row), fill=color)
        for row, bitrow in enumerate(arrow):
            for col, bit in enumerate(bitrow):
                if bit == '1':
                    draw.point((arrow_x0 + col, icon_y0 + row), fill=WHITE)

        # The number shown above the ball is the yard line *as ESPN's
        # possessionText already formats it* (e.g. "BUF 43" -> "43") -- this
        # identifies which side of the field the ball is on, which is not
        # necessarily the possessing team's own side once they've crossed
        # midfield, so don't just reprint the raw yard_line here.
        _, _, yard_str = self._parse_possession_text(game)
        digit_w = 4
        visual_w = digit_w * len(yard_str) - 1
        ball_center = icon_x0 + 3
        start_x = ball_center - visual_w // 2
        for i, ch in enumerate(yard_str):
            bits = FONT_SMALL.get(ch)
            if not bits:
                continue
            for row, bitrow in enumerate(bits):
                for col, bit in enumerate(bitrow):
                    if bit == '1':
                        draw.point((start_x + i * digit_w + col, 18 + row), fill=WHITE)

    def _parse_possession_text(self, game) -> tuple:
        """
        Split ESPN's `possessionText` (e.g. "BUF 43") into (team, yard_str).
        This is the field-side label, which is NOT always the possessing
        team once they've crossed midfield -- ESPN already resolves that,
        so we just parse their string instead of re-deriving it ourselves.
        Falls back to (possession-side abbr, str(yard_line)) if the API
        didn't give us possessionText for some reason.
        """
        text = (game.get("possession_text") or "").strip()
        if text and " " in text:
            team, _, yard = text.rpartition(" ")
            if yard.isdigit():
                return team, text, yard

        # Fallback: not ideal (this re-derives rather than trusting ESPN's
        # own resolved label), but keeps the display from going blank.
        possession_side = game.get("possession_indicator")
        fallback_team = game.get("away_abbr" if possession_side == "away" else "home_abbr", "")[:3].upper()
        yard_line = game.get("yard_line")
        fallback_yard = str(int(yard_line)) if yard_line is not None else ""
        return fallback_team, f"{fallback_team} {fallback_yard}".strip(), fallback_yard

    def _draw_info_row(self, draw, game):
        cursor = [57]  # mutable so the nested helper can advance it

        def draw_char(ch, color, last=False):
            bits = FONT.get(ch)
            char_width = 1 if ch == ':' else (len(bits[0]) if bits else 3)
            if bits:
                for row, bitrow in enumerate(bits):
                    if ch == ':':
                        if bitrow[1] == '1':
                            draw.point((cursor[0], 6 + row), fill=color)
                    else:
                        for col in range(char_width):
                            if bitrow[col] == '1':
                                draw.point((cursor[0] + col, 6 + row), fill=color)
            cursor[0] += char_width + (0 if last else 1)

        period_text = game.get("period_text", "")
        clock = game.get("clock", "")
        # ESPN's shortDownDistanceText comes back like "3rd & 7" -- our font is
        # uppercase-only and we render tight (no spaces) like "4TH&3", so
        # normalize both before drawing.
        down_distance = (game.get("down_distance_text", "") or "").upper().replace(" ", "")
        _, field_pos_str, _ = self._parse_possession_text(game)

        for ch in period_text:
            draw_char(ch, WHITE)
        cursor[0] += 1
        for ch in clock:
            draw_char(ch, WHITE)

        cursor[0] += 3
        for ch in down_distance[:-1]:
            draw_char(ch, GOALPOST_YELLOW)
        if down_distance:
            draw_char(down_distance[-1], GOALPOST_YELLOW)

        cursor[0] += 3
        parts = field_pos_str.split(" ", 1)
        for ch in parts[0]:
            draw_char(ch, WHITE)
        if len(parts) > 1:
            cursor[0] += 1
            for ch in parts[1][:-1]:
                draw_char(ch, WHITE)
            draw_char(parts[1][-1], WHITE, last=True)
