"""
NFL/College Scoreboard Plugin for LEDMatrix

Fully standalone (own requests.Session(), own complete ESPN fetch and
extraction logic) -- mirrors baseball's own current, real-hardware-
confirmed-working architecture. The core's shared sports base classes
this plugin originally depended on (src.base_classes.football/sports)
were removed entirely when LEDMatrix deprecated built-in managers in
favor of standalone plugins; see the imports section below for how that
was confirmed. Implements its own _draw_scorebug_layout() with a custom
pixel-perfect layout:
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
import time
import tempfile
import os
from pathlib import Path
from datetime import datetime, timedelta

import requests
from PIL import Image, ImageDraw, ImageFont

# The core's shared sports base classes (src.base_classes.football/sports)
# were removed entirely when LEDMatrix deprecated built-in managers in
# favor of fully standalone plugins -- confirmed via the real
# "No module named 'src.base_classes.football'" error on real hardware,
# and via the current LEDMatrix README ("Built-in Managers Deprecated...
# moved to the plugin system"). This plugin no longer depends on them at
# all; every sport plugin (including the official football-scoreboard and
# baseball, confirmed via baseball's own current source) now implements
# its own complete ESPN fetch/extraction, mirroring baseball's approach.
#
# BasePlugin itself is NOT deprecated -- it's the one still-valid core
# interface every plugin implements, confirmed directly from baseball's
# own current source ("on real deployments the import above succeeds").
# Guarded the same way baseball does: the fallback below exists ONLY for
# sandbox testing without the real LEDMatrix framework installed.
try:
    from src.plugin_system.base_plugin import BasePlugin
except ImportError:
    class BasePlugin:  # type: ignore
        """Local fallback ONLY for sandbox testing when the real
        LEDMatrix framework isn't installed -- on real deployments the
        import above succeeds and this class is never used."""
        def __init__(self, plugin_id, config, display_manager, cache_manager, plugin_manager):
            self.plugin_id = plugin_id
            self.config = config
            self.display_manager = display_manager
            self.cache_manager = cache_manager
            self.plugin_manager = plugin_manager

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
    '/': ['001', '001', '010', '100', '100'],
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


# NOTE: the old core-inherited "_LiveDataWorker"/"_RecentDataWorker"/
# "_UpcomingDataWorker" classes (one per league, each delegating to a core
# Football/FootballLive/SportsRecent/SportsUpcoming base class) are gone.
# Those core classes no longer exist -- confirmed via the real
# "No module named 'src.base_classes.football'" error on real hardware,
# and via LEDMatrix's own current README ("Built-in Managers Deprecated").
# This plugin is now a single, fully standalone class (mirroring baseball's
# own current, real-hardware-confirmed-working architecture): one shared
# requests.Session(), its own complete ESPN fetch + extraction logic below
# (as methods on NFLCollegeScoreboardPlugin itself), looping over whichever
# leagues are configured rather than owning a separate worker instance per
# league. All of the actual DRAWING code below this point (display(),
# _draw_scorebug_layout(), etc.) is untouched -- it only ever consumed a
# plain game dict, never anything from the removed core classes directly.


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

    Fully self-contained (mirroring baseball's own current, real-hardware-
    confirmed-working architecture) -- one shared requests.Session(), its
    own complete ESPN fetch + extraction logic, looping over whichever
    leagues are configured. No dependency on any core sports base class;
    those were removed from LEDMatrix entirely (see the imports comment
    at the top of this file for how that was confirmed).

    Implements its own _draw_scorebug_layout() -- the exact pixel design
    worked out earlier in this project -- for whichever game gets
    selected across all configured leagues/states.
    """

    # A realistic, full browser header set -- NOT just a User-Agent string.
    # Real history behind this: a custom app-identifying UA
    # ("LEDMatrix-NFLCollegeScoreboard/1.0") was deployed and confirmed
    # STILL got a real 403 Forbidden on real hardware during a genuinely
    # live game (2026-08-06, Hall of Fame Game). Re-checking baseball's own
    # CURRENT source (it hit the identical 403 around the same date) shows
    # its proven fix goes further than anything tried here: a full
    # browser-like header set including Accept/Accept-Language/
    # Accept-Encoding/Referer/Origin/Connection AND the three Sec-Fetch-*
    # headers, which hadn't been tried in this plugin before. Baseball is
    # confirmed fetching successfully on the same Pi/IP right now with
    # this exact set, which is the strongest evidence available for any
    # header configuration tried so far -- adopted verbatim rather than
    # partially, since baseball's own comments note even a complete
    # standard-browser UA ALONE was insufficient (a common bot-detection
    # pattern checks for the other headers a real browser always sends
    # alongside it, not just User-Agent in isolation).
    #
    # Still not verifiable from this sandbox (no outbound network access
    # here) -- if this ALSO doesn't resolve it on real hardware, that
    # would point toward something header-independent (IP-based rate
    # limiting, TLS fingerprinting, or a more fundamental block), which
    # no header change could fix.
    _BROWSER_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.espn.com/nfl/scoreboard",
        "Origin": "https://www.espn.com",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }

    def __init__(self, plugin_id: str, config: Dict[str, Any],
                 display_manager: Any, cache_manager: Any, plugin_manager: Any):
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)
        self.logger = logging.getLogger(f"plugin.{plugin_id}")

        self.leagues: List[str] = config.get("leagues") or ["nfl"]
        # ESPN's own URL slug for a league IS the config value already
        # ("nfl", "college-football") -- no separate mapping needed now
        # that we build the fetch URL ourselves instead of going through
        # a core class that expected a different internal "sport_key".

        # ONE shared session for everything (both leagues, all three
        # states) -- mirrors baseball's own architecture exactly. Real
        # finding from this project: the previous per-league-per-state
        # worker design created up to 6 separate requests.Session()
        # objects firing requests within the same update() cycle, a real
        # architectural difference from baseball (single session) that
        # was never ruled out as a contributing factor to the 403s hit
        # here. A single shared session is both simpler and closer to the
        # one plugin confirmed still working against ESPN right now.
        self.session = requests.Session()
        self.session.headers.update(self._BROWSER_HEADERS)

        self.live_games: List[Dict[str, Any]] = []
        self.recent_games: List[Dict[str, Any]] = []
        self.upcoming_games: List[Dict[str, Any]] = []
        self.current_game: Optional[Dict] = None
        self.current_state: Optional[str] = None  # "live" | "recent" | "upcoming"

        # In-memory logo cache, keyed by "{league}_{abbr}_{size}" -- avoids
        # re-opening/re-thumbnailing the same file every display() call.
        # Local-file resolution + ESPN download-and-cache-to-disk happens
        # once per game in _resolve_logo() during fetch, not here; this
        # cache is just for the decoded/resized PIL.Image itself.
        self._logo_cache: Dict[str, Any] = {}
        # Where downloaded (non-bundled) logos get cached to disk between
        # polls, keyed the same way -- avoids re-downloading from ESPN
        # every single update() cycle for teams with no local asset.
        #
        # REAL BUG FOUND AND FIXED HERE: this used to be
        # os.path.join(PLUGIN_DIR, "logo_cache") -- inside the plugin's
        # own install folder. Confirmed on real hardware: updating the
        # plugin failed with "Failed to remove logo_cache/
        # college-football_ARS.png: Permission denied", because files
        # this plugin's own runtime process downloaded and wrote ended up
        # with permissions/ownership the UPDATE process (running as a
        # different user, or at a different point in the permission
        # chain) couldn't delete during its own cleanup step. Baseball's
        # own current source never writes downloaded logos to disk at all
        # -- only ever caches them in memory (self._logo_cache) -- so this
        # was a deviation from its proven pattern that created a real
        # deployment problem. Using the system temp directory instead:
        # completely outside the plugin's own folder structure, so it can
        # never conflict with a future plugin update/reinstall again.
        self._logo_disk_cache_dir = os.path.join(tempfile.gettempdir(), "nfl-college-scoreboard-logos")

        # Cache for _fetch_recent_lookback()'s results, keyed by league --
        # refreshed on its own slower timer (recent.update_interval_seconds,
        # default 1hr) rather than every update() cycle, since a completed
        # game's result doesn't change once final.
        self._recent_lookback_cache: Dict[str, List[Dict]] = {}
        self._recent_lookback_last_fetch: Dict[str, float] = {}

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
        "away_logo_path": Path(_TEST_LOGO_DIR) / "BUF.png", "away_logo_url": None,
        "away_timeouts": 2,
        "home_abbr": "KC", "home_id": "12", "home_score": "20",
        "home_color": (227, 24, 55),
        "home_logo_path": Path(_TEST_LOGO_DIR) / "KC.png", "home_logo_url": None,
        "home_timeouts": 3,
        "period_text": "Q4", "clock": "2:14", "down_distance_text": "3rd & 7",
        "possession_indicator": "away", "possession_text": "BUF 43",
        "yard_line": 43, "is_redzone": False,
    }
    _TEST_RECENT_GAME = {
        "league": "nfl",
        "away_abbr": "DAL", "away_id": "6", "away_score": "24",
        "away_color": (0, 34, 68),
        "away_logo_path": Path(_TEST_LOGO_DIR) / "DAL.png", "away_logo_url": None,
        "home_abbr": "DET", "home_id": "8", "home_score": "34",
        "home_color": (0, 118, 182),
        "home_logo_path": Path(_TEST_LOGO_DIR) / "DET.png", "home_logo_url": None,
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
        "away_logo_path": Path(_TEST_LOGO_DIR) / "GB.png", "away_logo_url": None,
        "away_record": "5-3",
        "home_abbr": "CHI", "home_id": "3", "home_score": "0",
        "home_color": (11, 22, 42),
        "home_logo_path": Path(_TEST_LOGO_DIR) / "CHI.png", "home_logo_url": None,
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
        if view == "all":
            cycle_len = max(self.config.get("display_duration", 15), 1)
            index = int(time.time() // cycle_len) % 3
            view = ["live", "recent", "upcoming"][index]

        if view == "recent":
            self.current_game, self.current_state = self._TEST_RECENT_GAME, "recent"
        elif view == "upcoming":
            self.current_game, self.current_state = self._TEST_UPCOMING_GAME, "upcoming"
        else:
            self.current_game, self.current_state = self._TEST_LIVE_GAME, "live"

    def _fetch_league_scoreboard(self, league: str) -> List[Dict]:
        """
        Fetches ESPN's scoreboard endpoint ONCE for this league -- covers
        live, today's completed, and today's not-yet-started games all in
        a single call. Mirrors baseball's own current, real-hardware-
        confirmed-working approach (one fetch per poll) rather than this
        plugin's previous design (a separate fetch per state per league --
        up to 6 requests per update() cycle across 2 leagues x 3 states).
        Deliberately reducing request volume/shape is a real, distinct
        change from the header changes above -- neither alone has been
        confirmed to resolve the 403s investigated in this project, so
        both are worth having in place.

        Raises on a real HTTP error (401/403 included) rather than
        swallowing it, unlike the core-provided fetch methods this plugin
        used to depend on (confirmed via a real 403 that went completely
        invisible to this plugin's own error handling until that was
        fixed) -- the caller's try/except is what actually logs this.
        """
        url = f"https://site.api.espn.com/apis/site/v2/sports/football/{league}/scoreboard"
        resp = self.session.get(url, timeout=10)
        if not resp.ok:
            self.logger.error(
                f"ESPN scoreboard fetch for {league} got HTTP {resp.status_code} -- "
                f"response body: {resp.text[:500]!r}. "
                f"Current User-Agent: {self.session.headers.get('User-Agent')!r}."
            )
        resp.raise_for_status()
        return resp.json().get("events", [])

    def _fetch_recent_lookback(self, league: str, days_back: int = 7) -> List[Dict]:
        """
        Separate fetch covering the last `days_back` days, day by day --
        explicitly requested (confirmed against real usage): the main
        scoreboard call (_fetch_league_scoreboard) only returns the
        CURRENT NFL week's games (Thursday through Monday), not a rolling
        window -- so a game that finished last week is invisible to it
        entirely.

        REAL BUG FOUND AND FIXED HERE: this originally used a single
        hyphenated date-RANGE query (`dates=20260917-20260924`), which
        got a confirmed real 400 Bad Request on real hardware --
        `{"code":400,"message":"Failed to get events endpoint."}` --
        even after narrowing the range from 10 to 7 days (ruling out
        "range too long" as the cause). Re-checked baseball's own current,
        real-hardware-confirmed-working `_fetch_past_games_lookback` and
        found a genuine difference: it NEVER uses a hyphenated range at
        all -- it loops over each individual day with its OWN single-date
        query (`dates=20260917`, `dates=20260918`, etc.), one request per
        day. Switched to that exact approach. Plausible explanation for
        why the range format seemed to "work" (got a 403, not a 400) in
        this plugin's very first, pre-full-rewrite version: that 403 was
        very likely a bot-blocking layer rejecting the request based on
        headers alone, before it ever reached whatever backend logic
        would have validated the date parameter's syntax -- so the range
        format was never actually confirmed valid, just rejected earlier
        in the pipeline for an unrelated reason. Only once the header fix
        got requests past that layer did the real validation error
        underneath become visible.

        More requests than the single range-query approach (one per day
        instead of one for the whole window), but baseball does exactly
        this on the same real Pi/IP and is confirmed working, so this
        isn't a re-introduction of the request-volume concern from
        earlier in this project -- it's the proven-correct shape for this
        specific kind of query, which is different from the main
        per-league scoreboard call.

        Called on its own slower timer (see update()), not every single
        update() cycle -- a completed game's result doesn't change once
        final, so there's no reason to re-fetch this as often as live
        data.
        """
        url = f"https://site.api.espn.com/apis/site/v2/sports/football/{league}/scoreboard"
        today = datetime.now()
        events: List[Dict] = []
        for offset in range(1, days_back + 1):
            day = today - timedelta(days=offset)
            date_param = day.strftime("%Y%m%d")
            try:
                resp = self.session.get(url, params={"dates": date_param}, timeout=10)
                if not resp.ok:
                    self.logger.error(
                        f"ESPN recent-lookback fetch for {league}/{date_param} got HTTP "
                        f"{resp.status_code} -- response body: {resp.text[:500]!r}"
                    )
                resp.raise_for_status()
                events.extend(resp.json().get("events", []))
            except Exception as e:
                # One bad day shouldn't sink the whole lookback -- log and
                # keep going, same as baseball's own equivalent loop.
                self.logger.warning(f"Could not fetch recent-lookback for {league}/{date_param}: {e}")
                continue
        return events

    def _format_game_datetime(self, event: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """Same approach as baseball's own _format_game_datetime (confirmed
        working on real hardware): parses ESPN's event.date (ISO8601 UTC,
        e.g. "2026-08-07T00:00Z") and formats as separate (date, time)
        strings in the system's local timezone -- assumes the Pi's system
        clock/timezone is set correctly, the normal case for a home
        device. Returns (None, None) if missing/unparseable rather than
        crashing."""
        raw = event.get("date")
        if not raw:
            return None, None
        try:
            iso = raw.replace("Z", "+00:00")
            dt_utc = datetime.fromisoformat(iso)
            dt_local = dt_utc.astimezone()
            date_str = f"{dt_local.month}/{dt_local.day}"
            hour_12 = dt_local.hour % 12 or 12
            ampm = "AM" if dt_local.hour < 12 else "PM"
            time_str = f"{hour_12}:{dt_local.minute:02d} {ampm}"
            return date_str, time_str
        except Exception:
            return None, None

    def _parse_game_event(self, event: Dict[str, Any], league: str) -> Optional[Dict[str, Any]]:
        """
        Builds our complete game dict directly from one raw ESPN scoreboard
        event -- standalone, no core Football/SportsCore class to delegate
        the "common" fields to anymore. Mirrors baseball's own _parse_game
        (confirmed working, from its current real-hardware source) for the
        general shape, adapted for football's own fields (yard line,
        possession, down/distance, quarter/clock, timeouts) in place of
        baseball's (balls/strikes/outs, bases, inning).
        """
        try:
            competitions = event.get("competitions", [])
            if not competitions:
                return None
            comp = competitions[0]
            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                return None
            away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[0])
            home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[-1])

            status = comp.get("status", {}) or {}
            status_type = status.get("type", {}) or {}
            situation = comp.get("situation", {}) or {}
            state = status_type.get("state", "pre")  # "pre" | "in" | "post"

            def team_abbr(competitor):
                return competitor.get("team", {}).get("abbreviation", "")[:3].upper()

            def team_record(competitor):
                """NOT fully confirmed against real captured data (same
                caveat baseball's own equivalent extraction carries) --
                ESPN's competitor.records array structure is documented
                by the community but not the exact sub-field names. Tries
                plausible combinations; returns None rather than guessing
                wrong if nothing usable is found."""
                try:
                    records = competitor.get("records", [])
                    if not records:
                        return None
                    overall = next(
                        (r for r in records
                         if str(r.get("type", r.get("name", ""))).lower() in ("total", "overall")),
                        records[0],
                    )
                    return overall.get("summary") or overall.get("displayValue")
                except Exception:
                    return None

            def team_logo_url(competitor):
                team = competitor.get("team", {})
                if team.get("logo"):
                    return team["logo"]
                logos = team.get("logos") or []
                if logos:
                    return logos[0].get("href")
                return None

            def parse_linescores(competitor):
                raw = competitor.get("linescores")
                if not isinstance(raw, list):
                    return []
                out = []
                for entry in raw:
                    try:
                        out.append(int(entry.get("value")))
                    except (AttributeError, TypeError, ValueError):
                        out.append(None)
                return out

            game_date_str, game_time_str = self._format_game_datetime(event)
            away_id = away.get("team", {}).get("id")
            home_id = home.get("team", {}).get("id")

            details = {
                "league": league,
                "event_id": event.get("id"),
                "state": state,
                "away_id": away_id,
                "home_id": home_id,
                "away_abbr": team_abbr(away),
                "home_abbr": team_abbr(home),
                "away_score": int(away.get("score", 0) or 0),
                "home_score": int(home.get("score", 0) or 0),
                "away_record": team_record(away),
                "home_record": team_record(home),
                "away_color": _hex_to_rgb(away.get("team", {}).get("color"), away.get("team", {}).get("alternateColor")),
                "home_color": _hex_to_rgb(home.get("team", {}).get("color"), home.get("team", {}).get("alternateColor")),
                "away_logo_url": team_logo_url(away),
                "home_logo_url": team_logo_url(home),
                "away_logo_path": None,   # filled in below by _resolve_logo()
                "home_logo_path": None,
                "game_date": game_date_str,
                "game_time": game_time_str,
                "period": status.get("period", 0),
                "period_text": f"Q{status.get('period')}" if status.get("period") else "",
                "clock": status.get("displayClock", ""),
                "home_linescores": parse_linescores(home),
                "away_linescores": parse_linescores(away),
            }

            # Live-specific fields -- only meaningful (and only reliably
            # present) while the game is actually in progress.
            details["yard_line"] = situation.get("yardLine")
            details["possession_text"] = situation.get("possessionText", "")
            details["is_redzone"] = situation.get("isRedZone", False)
            possession_team_id = situation.get("possession")
            if possession_team_id == home_id:
                details["possession_indicator"] = "home"
            elif possession_team_id == away_id:
                details["possession_indicator"] = "away"
            else:
                details["possession_indicator"] = None

            # NOT CONFIRMED against real captured live data (unlike most
            # other fields here) -- trying the most likely field names,
            # same "extract what's there, don't guess wrong" approach as
            # baseball's own unconfirmed fields.
            details["down_distance_text"] = (
                situation.get("shortDownDistanceText")
                or situation.get("downDistanceText")
                or ""
            )

            # NOT CONFIRMED against real captured data -- ESPN's
            # lightweight scoreboard response may not include timeouts
            # remaining at all (baseball's own extraction notes several
            # fields, like hits/errors, that simply aren't present at this
            # endpoint and need the detailed summary endpoint instead).
            # Defaulting to 3 (a full allotment) rather than 0, so an
            # unpopulated value doesn't misleadingly render as "all
            # timeouts used."
            details["away_timeouts"] = away.get("timeouts", 3)
            details["home_timeouts"] = home.get("timeouts", 3)

            # ESPN's scoreboard-level `leaders` -- combined across BOTH
            # teams (one top passer/rusher/receiver for the whole game,
            # not per team), so which team each belongs to is checked via
            # the `team` reference. NOT YET VERIFIED: the exact contents
            # of `displayValue` (whether it includes completions/attempts
            # and TDs, or just yards) -- extracted verbatim rather than
            # assuming a specific format.
            try:
                raw_leaders = comp.get("leaders", [])
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

            self._resolve_logo(details, "away")
            self._resolve_logo(details, "home")
            return details
        except Exception as e:
            self.logger.debug(f"Failed to parse game event: {e}", exc_info=True)
            return None

    # Best-guess local-asset folder per league -- NOT confirmed for
    # college-football specifically (only "nfl_logos" was directly
    # confirmed present on real hardware, via this project's test-mode
    # work). A wrong guess here just means a slower first load for that
    # league (falls through to downloading from ESPN and caching that
    # instead), not a broken one.
    _LOGO_DIR_BY_LEAGUE = {
        "nfl": "assets/sports/nfl_logos",
        "college-football": "assets/sports/ncaa_logos",
    }

    def _resolve_logo(self, game: Dict[str, Any], side: str) -> None:
        """
        Fills in game["{side}_logo_path"] with a local file path --
        either a bundled core asset, or an ESPN-downloaded file cached to
        disk on first use. Mirrors baseball's own _resolve_logos/
        _get_team_logo/_load_local_logo pattern (confirmed working on
        real hardware): try a local asset first, fall back to downloading
        via the shared session, cache whichever is found so later polls
        for the same team don't repeat the work.
        """
        abbr = game.get(f"{side}_abbr", "")
        league = game.get("league", "nfl")
        url = game.get(f"{side}_logo_url")

        logo_dir = self._LOGO_DIR_BY_LEAGUE.get(league, "assets/sports/nfl_logos")
        for name in (f"{abbr}.png", f"{abbr.lower()}.png", f"{abbr}.PNG"):
            path = os.path.join(logo_dir, name)
            if os.path.isfile(path):
                game[f"{side}_logo_path"] = path
                return

        if not url:
            return

        try:
            os.makedirs(self._logo_disk_cache_dir, exist_ok=True)
        except Exception as e:
            self.logger.debug(f"Could not create logo cache dir: {e}")
            return

        cache_path = os.path.join(self._logo_disk_cache_dir, f"{league}_{abbr}.png")
        if os.path.isfile(cache_path):
            game[f"{side}_logo_path"] = cache_path
            return

        try:
            resp = self.session.get(url, timeout=8)
            resp.raise_for_status()
            with open(cache_path, "wb") as f:
                f.write(resp.content)
            game[f"{side}_logo_path"] = cache_path
        except Exception as e:
            self.logger.debug(f"Could not download logo for {abbr}: {e}")

    def update(self) -> None:
        test_cfg = self.config.get("test_mode", {})
        if test_cfg.get("enabled", False):
            self._update_test_mode(test_cfg.get("view", "live"))
            return

        favorite_teams = self.config.get("favorite_teams", [])
        live_cfg = self.config.get("live", {})
        recent_cfg = self.config.get("recent", {})
        upcoming_cfg = self.config.get("upcoming", {})

        self.logger.debug(f"update() called: leagues={self.leagues}")

        all_live: List[Dict] = []
        all_recent: List[Dict] = []
        all_upcoming: List[Dict] = []

        for league in self.leagues:
            try:
                events = self._fetch_league_scoreboard(league)
            except Exception as e:
                self.logger.error(f"FETCH FAILED for {league}: {type(e).__name__}: {e}", exc_info=True)
                continue

            live_count = recent_count = upcoming_count = 0
            for event in events:
                competitions = event.get("competitions", [])
                if not competitions:
                    continue
                state = competitions[0].get("status", {}).get("type", {}).get("state")
                if state == "in":
                    game = self._parse_game_event(event, league)
                    if game:
                        all_live.append(game)
                        live_count += 1
                elif state == "post" and recent_cfg.get("enabled", True):
                    game = self._parse_game_event(event, league)
                    if game:
                        all_recent.append(game)
                        recent_count += 1
                elif state == "pre" and upcoming_cfg.get("enabled", True):
                    game = self._parse_game_event(event, league)
                    if game:
                        all_upcoming.append(game)
                        upcoming_count += 1

            self.logger.info(
                f"Fetched {league} OK: {live_count} live, {recent_count} recent, "
                f"{upcoming_count} upcoming"
            )
            if live_count:
                # Diagnostic: lists every live game's exact extracted
                # abbreviations for this league. Needed to debug a real
                # report -- a configured favorite team supposedly playing
                # live never got shown/prioritized, and the only way to
                # confirm whether that's an extraction mismatch (wrong
                # abbreviation string) vs. something else is to see
                # exactly what string this plugin actually extracted for
                # each live game, not assume it matches what ESPN's
                # website displays.
                this_league_live = [g for g in all_live if g.get("league") == league]
                self.logger.info(
                    f"Live games in {league}: "
                    + ", ".join(f"{g.get('away_abbr')}@{g.get('home_abbr')}" for g in this_league_live)
                )

            # Merge in last week's finished games -- the main scoreboard
            # call above only covers the CURRENT NFL week, so a game that
            # finished last week would otherwise never appear as "recent"
            # at all. Refreshed on its own slower timer (not every
            # update() cycle) since a completed game's result never
            # changes once final.
            if recent_cfg.get("enabled", True):
                lookback_interval = recent_cfg.get("update_interval_seconds", 3600)
                last_fetch = self._recent_lookback_last_fetch.get(league, 0.0)
                now = time.time()
                if now - last_fetch >= lookback_interval or league not in self._recent_lookback_cache:
                    try:
                        lookback_events = self._fetch_recent_lookback(league)
                        lookback_games = []
                        for event in lookback_events:
                            competitions = event.get("competitions", [])
                            if not competitions:
                                continue
                            state = competitions[0].get("status", {}).get("type", {}).get("state")
                            if state != "post":
                                continue
                            game = self._parse_game_event(event, league)
                            if game:
                                lookback_games.append(game)
                        self._recent_lookback_cache[league] = lookback_games
                        self._recent_lookback_last_fetch[league] = now
                        self.logger.info(f"Recent-lookback for {league} OK: {len(lookback_games)} finished game(s)")
                    except Exception as e:
                        self.logger.error(
                            f"RECENT LOOKBACK FETCH FAILED for {league}: {type(e).__name__}: {e}",
                            exc_info=True,
                        )
                # Merge, deduplicating by event_id against what the main
                # per-week call already found.
                seen_ids = {g.get("event_id") for g in all_recent}
                for g in self._recent_lookback_cache.get(league, []):
                    if g.get("event_id") not in seen_ids:
                        all_recent.append(g)
                        seen_ids.add(g.get("event_id"))

        self.live_games, self.recent_games, self.upcoming_games = all_live, all_recent, all_upcoming

        # Priority: live > recent > upcoming -- a live game is always more
        # interesting than a completed or not-yet-started one. Within
        # each state, favorite teams are moved to the front.
        #
        # TODO: this shows only the single top-priority game in each
        # state; rotating through *all* live games (or all recent/
        # upcoming ones) over their configured game_duration_seconds is
        # still a stub.
        if live_cfg.get("enabled", True) and all_live:
            if live_cfg.get("live_priority", True):
                all_live = self._favorite_first(all_live, favorite_teams)
            self.current_game = all_live[0]
            self.current_state = "live"
            self.logger.info(
                f"Showing LIVE: {self.current_game.get('away_abbr')}@"
                f"{self.current_game.get('home_abbr')}"
            )
            return

        if recent_cfg.get("enabled", True) and all_recent:
            all_recent = self._favorite_first(all_recent, favorite_teams)
            self.current_game = all_recent[0]
            self.current_state = "recent"
            self.logger.info(
                f"Showing RECENT: {self.current_game.get('away_abbr')}@"
                f"{self.current_game.get('home_abbr')}"
            )
            return

        if upcoming_cfg.get("enabled", True) and all_upcoming:
            all_upcoming = self._favorite_first(all_upcoming, favorite_teams)
            self.current_game = all_upcoming[0]
            self.current_state = "upcoming"
            self.logger.info(
                f"Showing UPCOMING: {self.current_game.get('away_abbr')}@"
                f"{self.current_game.get('home_abbr')}"
            )
            return

        self.logger.warning(
            "No games found across live/recent/upcoming for any configured league -- "
            "current_game will be None and display() will show nothing. If fetches "
            "above show 0/0/0 rather than FETCH FAILED, the fetch itself is working "
            "but genuinely found nothing (check leagues config/season); if you see "
            "FETCH FAILED, that's the actual problem to chase."
        )
        self.current_game = None
        self.current_state = None

    # Maps our manifest's declared display_modes to (config section key,
    # game-list attribute, draw method).
    _MODE_TO_STATE = {
        "nfl_college_live": ("live", "live_games", "_draw_scorebug_layout"),
        "nfl_college_recent": ("recent", "recent_games", "_draw_recent_layout"),
        "nfl_college_upcoming": ("upcoming", "upcoming_games", "_draw_upcoming_layout"),
    }

    def _pick_rotated_game(self, games: List[Dict], duration_seconds: float) -> Dict:
        """
        Real gap found and fixed here: `game_duration_seconds` and
        `games_to_show` have been in config_schema.json since the
        beginning (20s/15s defaults, 10/5 game caps) but were never
        actually implemented -- display() always just rendered games[0],
        so with e.g. 16 upcoming games fetched, only the single
        first-sorted one ever showed, and it never advanced. Confirmed by
        explicit user report: only one upcoming game visible despite a
        full week's slate being fetched.

        Deterministic wall-clock rotation, not mutable per-mode index
        state -- same approach already used by _update_test_mode's "all"
        cycling. Whichever game "should" be showing at this exact moment
        is computed fresh from time.time() // duration_seconds, so it
        doesn't matter how often or irregularly display() gets called;
        every call at a given moment picks the same game, and it
        naturally advances as time passes.
        """
        if len(games) <= 1:
            return games[0]
        index = int(time.time() // max(duration_seconds, 1)) % len(games)
        return games[index]

    def has_live_content(self) -> bool:
        """
        Framework hook (BasePlugin) confirmed from baseball's own real,
        currently-working source: the display controller checks this
        (alongside has_live_priority(), already implemented by BasePlugin
        itself, reading the `live_priority` config toggle) to decide
        whether to stay on this plugin's live mode instead of rotating
        away to something else on schedule.

        Explicitly requested to differ from baseball's own version here:
        baseball only returns True when a FAVORITE team is specifically
        live (favorite live = exclusive cross-plugin priority too). This
        plugin returns True whenever ANY game is live at all, regardless
        of favorites -- e.g. Thursday/Monday Night Football should keep
        the display locked on live mode even with no favorite team
        involved, only falling back to recent/upcoming rotation once
        nothing is actually live. The favorite-exclusive behavior (only
        show MY team, not the whole slate) is handled separately, inside
        display()'s own game selection for live mode -- this hook is
        purely about whether to stay on live mode at the cross-mode
        rotation level, not which specific game to show once there.
        """
        return bool(self.live_games)

    def display(self, force_clear: bool = False, display_mode: Optional[str] = None) -> bool:
        """
        Real bug found and fixed here: the core's display_controller
        inspects display()'s signature and only passes `display_mode`
        (telling the plugin which of its declared display_modes --
        "nfl_college_live"/"_recent"/"_upcoming" -- to show right now) if
        the method actually accepts that keyword. Our previous signature
        (`display(self, force_clear=False)`) didn't, so the core silently
        fell back to calling us with no mode information at all --
        confirmed via real logs: the core correctly logged "Switching to
        mode: nfl_college_recent" etc. every ~15s, but our own diagnostic
        logging showed "Showing UPCOMING: ATL@GB" every single time
        regardless, because we were always falling through to our own
        global current_game/current_state priority selection instead of
        respecting which mode was actually being asked for. That's why
        the display looked "stuck" even though fetching worked correctly
        the whole time (also confirmed in the same logs).

        Test mode and any other caller that doesn't pass display_mode
        keeps the old global-priority behavior (current_game/
        current_state, set by _update_test_mode()/update()) -- since test
        mode has no notion of "which of the three modes is active", it's
        always just previewing one fixed sample.

        Returns True/False (not None) -- confirmed via real
        display_controller behavior/bug reports: a plugin returning
        False for a mode with no content lets the core skip that mode
        immediately and move to the next one, rather than showing a
        blank/stale panel for it.
        """
        if display_mode is not None:
            mapping = self._MODE_TO_STATE.get(display_mode)
            if mapping is None:
                self.logger.warning(f"Unknown display_mode {display_mode!r}, nothing to show")
                return False

            # Real bug found and fixed here: has_live_content() alone
            # (added last round) didn't stop the display from cycling
            # away to recent/upcoming -- confirmed via explicit report:
            # "shows the live game then starts cycling through previous
            # game results" even with a live game still in progress.
            # That hook apparently only affects cross-PLUGIN priority
            # (whether the core stays on this plugin instead of a
            # different one) -- it doesn't stop the core from still
            # cycling through THIS plugin's own three declared modes on
            # their normal schedule, since recent/upcoming honestly
            # reporting "yes, I have content" (returning True) is enough
            # for the core to show them on their turn regardless of live
            # content existing elsewhere in the same plugin. Explicitly
            # requested: live should always win over recent/upcoming
            # whenever ANY game is live, regardless of favorites -- so
            # recent/upcoming now actively refuse to show anything (return
            # False) while live content exists, rather than just letting
            # has_live_content() try to signal it indirectly.
            if display_mode in ("nfl_college_recent", "nfl_college_upcoming") and self.live_games:
                return False

            section_key, games_attr, draw_method_name = mapping
            games = getattr(self, games_attr, None) or []
            if not games:
                return False

            favorite_teams = self.config.get("favorite_teams", [])

            # Live-specific priority, explicitly requested: a favorite
            # team playing live should be shown EXCLUSIVELY (no rotating
            # to other live games at all), but with no favorite currently
            # live (either none configured, or none of them are playing
            # right now), show every live game and rotate normally --
            # e.g. Thursday/Monday Night Football (usually the only live
            # game) stays locked on that one game either way, while a
            # busy Sunday slate rotates through all of them unless a
            # favorite is on.
            if display_mode == "nfl_college_live" and favorite_teams:
                favorite_live = [
                    g for g in games
                    if g.get("away_abbr") in favorite_teams or g.get("home_abbr") in favorite_teams
                ]
                if favorite_live:
                    games = favorite_live  # exclusively this/these, nothing else

            section_cfg = self.config.get(section_key, {})
            games_to_show = section_cfg.get("games_to_show")
            if games_to_show:
                games = games[:games_to_show]
            games = self._favorite_first(games, favorite_teams)
            duration = section_cfg.get("game_duration_seconds", 15)
            game = self._pick_rotated_game(games, duration)
            draw_method = getattr(self, draw_method_name)
            draw_method(game, force_clear=force_clear)
            return True

        # No display_mode passed (test mode, or a caller not aware of the
        # per-mode contract) -- fall back to whichever single game our own
        # global priority selection (live > recent > upcoming) picked.
        if self.current_game is None:
            return False
        if self.current_state == "live":
            self._draw_scorebug_layout(self.current_game, force_clear=force_clear)
        elif self.current_state == "recent":
            self._draw_recent_layout(self.current_game, force_clear=force_clear)
        elif self.current_state == "upcoming":
            self._draw_upcoming_layout(self.current_game, force_clear=force_clear)
        else:
            return False
        return True

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

    def _load_and_resize_logo(self, team_id, abbr, logo_path, logo_url, size: int = 96):
        """
        Opens an already-resolved local logo file and thumbnails it.
        Replaces the old core-delegated version (which went through a
        per-league "worker" instance inheriting _load_and_resize_logo()
        from SportsCore) -- logo resolution (local asset vs. downloading
        from ESPN) now happens ONCE during fetch/extraction, in
        _resolve_logo(), so by the time rendering calls this, logo_path
        is already a valid local file or None. `team_id`/`logo_url` are
        accepted but unused -- kept so call sites didn't all need
        updating for a narrower signature.
        """
        if not logo_path:
            return None
        try:
            img = Image.open(logo_path).convert("RGBA")
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
            return img
        except Exception as e:
            self.logger.debug(f"Could not open logo file {logo_path} for {abbr}: {e}")
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
        comment in _parse_game_event.
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
                try:
                    return self._load_and_resize_logo(
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
            # Real bug found and fixed here: this used to center within
            # the OLD full zone (32, width 15) with an extra -1 shift --
            # left over from before the yellow box's trim direction was
            # fixed (previously x32-44, now x34-46). The box itself moved
            # but this centering math never did, so the score number was
            # still centered relative to where the box USED to be, not
            # where it actually is now -- confirmed via a real screenshot
            # showing 5px of padding on the right vs 1px on the left.
            # Centering directly within the box's real bounds (34, width
            # 13) needs no extra shift; verified this gives 3px/3px,
            # matching the home side (which needed no change, since ITS
            # box start happens to match its full zone's start already).
            score_x0 = centered_x(away_team["score"], 34, 13)
            if away_won:
                # Real bug found and fixed here: this box's trim was cut
                # from its RIGHT edge (x32-44 of the full x32-46 zone),
                # which put the empty gap immediately next to the stroke
                # separating this zone from FINAL -- the most visually
                # prominent spot, since it's right at the center of the
                # display. The home side's equivalent box (below) trims
                # from ITS right edge too, but its zone's adjacent stroke
                # is on the LEFT, so that same rule put its gap on the far
                # side (near the logo block, barely noticeable) purely by
                # coincidence of which side of the display each zone sits
                # on. Confirmed via a real screenshot: winner-on-left
                # reads as "not filling the space" while winner-on-right
                # looks fine, despite both boxes being identically sized.
                # Fixed by trimming from the LEFT edge instead (x34-46),
                # so this box now stays flush against its own adjacent
                # stroke (x47) exactly like the home side does against
                # its own (x80), and the gap moves to the far side (near
                # the logo block) where it's equally unnoticeable.
                draw.rectangle([34, 0, 46, TOP_H - 1], fill=YELLOW)
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

            # Always just "FINAL", even in overtime -- explicitly
            # requested after confirming neither "FINAL/OT" nor "FINAL OT"
            # fits this zone (both have the same 32px ink width vs a 30px
            # zone; removing the slash doesn't help, since a space costs
            # the same advance width as any other character in this font).
            title = "FINAL"
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
        """Best-effort logo load for the ported layouts -- logo_path is
        already resolved (local asset or ESPN download) by _resolve_logo()
        during fetch, so this just opens and thumbnails it."""
        team_id = game.get(f"{side}_id")
        abbr = game.get(f"{side}_abbr", "")
        logo_path = game.get(f"{side}_logo_path")
        logo_url = game.get(f"{side}_logo_url")
        if logo_path is None:
            return None
        try:
            return self._load_and_resize_logo(team_id, abbr, logo_path, logo_url)
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
                try:
                    logo = self._load_and_resize_logo(
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
