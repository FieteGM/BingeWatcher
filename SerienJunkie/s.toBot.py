import html as _html
import json
import logging
import os
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import unquote

from selenium import webdriver
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput

# === CONFIGURATION ===
HEADLESS: bool = os.getenv("BW_HEADLESS", "false").lower() in {"1", "true", "yes"}
START_URL: str = os.getenv("BW_START_URL", "https://s.to/")
INTRO_SKIP_SECONDS: int = int(os.getenv("BW_INTRO_SKIP", "80"))
MAX_RETRIES: int = int(os.getenv("BW_MAX_RETRIES", "3"))
WAIT_TIMEOUT: int = int(os.getenv("BW_WAIT_TIMEOUT", "25"))
PROGRESS_SAVE_INTERVAL: int = int(os.getenv("BW_PROGRESS_INTERVAL", "5"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GECKO_DRIVER_PATH = os.path.join(SCRIPT_DIR, "geckodriver.exe")

PROGRESS_DB_FILE = os.path.join(SCRIPT_DIR, "progress.json")
SETTINGS_DB_FILE = os.path.join(SCRIPT_DIR, "settings.json")

# === STREAMING PROVIDERS ===
STREAMING_PROVIDERS = {
    "s.to": {
        "name": "SerienJunkie",
        "base_url": "https://s.to/",
        "url_pattern": r"https://s\.to/serie/stream/([^/]+)/staffel-(\d+)(?:/episode-(\d+))?",
        "episode_url_template": "https://s.to/serie/stream/{series}/staffel-{season}/episode-{episode}",
        "color": "#3b82f6"
    },
    "aniworld.to": {
        "name": "AniWorld",
        "base_url": "https://aniworld.to/",
        "url_pattern": r"https://aniworld\.to/anime/stream/([^/]+)/staffel-(\d+)/episode-(\d+)",
        "episode_url_template": "https://aniworld.to/anime/stream/{series}/staffel-{season}/episode-{episode}",
        "color": "#8b5cf6"
    }
}

# === GLOBAL STATE ===
current_series: Optional[str] = None
current_season: Optional[int] = None
current_episode: Optional[int] = None
current_provider: Optional[str] = None
is_playing: bool = False
should_quit: bool = False

logging.basicConfig(
    format="[BingeWatcher] %(levelname)s: %(message)s", level=logging.INFO
)


class BingeWatcherError(Exception):
    pass

# Tor-Einstellung aus settings.json lesen
def get_tor_setting() -> bool:
    try:
        if os.path.exists(SETTINGS_DB_FILE):
            with open(SETTINGS_DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return bool(data.get("useTorProxy", False))
        return False
    except Exception:
        return False

USE_TOR_PROXY: bool = get_tor_setting()
TOR_SOCKS_PORT: int = int(os.getenv("BW_TOR_PORT", "9050"))

# === UTILS: PROGRESS ===
def load_progress() -> Dict[str, Dict[str, Any]]:
    try:
        if os.path.exists(PROGRESS_DB_FILE):
            with open(PROGRESS_DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        return {}
    except json.JSONDecodeError:
        logging.error("progress.json is corrupt.")
        return {}
    except Exception as e:
        logging.error(f"Error loading progress: {e}")
        return {}


def save_progress(
    series: str,
    season: int,
    episode: int,
    position: int,
    extra: Optional[Dict[str, Any]] = None,
    provider: str = "s.to",
) -> bool:
    try:
        db = load_progress()
        entry = db.get(series, {}) if isinstance(db.get(series, {}), dict) else {}
        entry.update(
            {
                "season": int(season),
                "episode": int(episode),
                "position": int(position),
                "timestamp": time.time(),
                "provider": provider,  # Speichere den Provider
            }
        )
        if extra:
            entry.update(extra)
        db[series] = entry

        with open(PROGRESS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"Error saving progress: {e}")
        return False


def handle_list_item_deletion(name: str) -> bool:
    try:
        db = load_progress()
        if name in db:
            del db[name]
            with open(PROGRESS_DB_FILE, "w", encoding="utf-8") as f:
                json.dump(db, f, indent=2, ensure_ascii=False)
            logging.info(f"Series deleted: {name}")
        return True
    except Exception as e:
        logging.error(f"Deletion failed: {e}")
        return False


def get_intro_skip_seconds(series: str) -> int:
    try:
        data = load_progress().get(series, {})
        val = int(data.get("intro_skip_start", INTRO_SKIP_SECONDS))
        return max(0, val)
    except Exception:
        return INTRO_SKIP_SECONDS


def get_intro_skip_end_seconds(series: str) -> int:
    try:
        data = load_progress().get(series, {})
        val = int(data.get("intro_skip_end", INTRO_SKIP_SECONDS + 60))
        return max(0, val)
    except Exception:
        return INTRO_SKIP_SECONDS + 60


def set_intro_skip_seconds(series: str, start_seconds: int, end_seconds: int = None) -> bool:
    try:
        start_seconds = max(0, int(start_seconds))
        if end_seconds is None:
            end_seconds = start_seconds + 60
        end_seconds = max(0, int(end_seconds))
        
        db = load_progress()
        entry = db.get(series, {}) if isinstance(db.get(series, {}), dict) else {}
        entry["intro_skip_start"] = start_seconds
        entry["intro_skip_end"] = end_seconds
        db[series] = entry
        with open(PROGRESS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"Intro time could not be saved: {e}")
        return False


def load_intro_times() -> Dict[str, Any]:
    """Load intro times from intro_times.json"""
    try:
        intro_times_file = os.path.join(SCRIPT_DIR, "intro_times.json")
        if os.path.exists(intro_times_file):
            with open(intro_times_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}
    except Exception as e:
        logging.error(f"Could not load intro times: {e}")
        return {}


def get_default_intro_times(series: str, season: int = 1) -> tuple[int, int]:
    """Get default intro times for a series and season"""
    try:
        intro_times = load_intro_times()
        series_data = intro_times.get(series, {})
        
        # Try to find specific season data
        for intro in series_data.get("intros", []):
            if intro.get("season") == season:
                return intro.get("start_time", 90), intro.get("end_time", 150)
        
        # Fall back to default times
        return series_data.get("default_skip_start", 90), series_data.get("default_skip_end", 150)
    except Exception:
        return 90, 150


def detect_intro_start(driver, series: str, season: int = 1) -> bool:
    """Detect if an intro is currently playing"""
    try:
        intro_times = load_intro_times()
        series_data = intro_times.get(series, {})
        
        # Get current video time
        current_time = driver.execute_script("return document.querySelector('video')?.currentTime || 0;")
        
        # Check if we're in the intro time window
        for intro in series_data.get("intros", []):
            if intro.get("season") == season:
                start_time = intro.get("start_time", 90)
                end_time = intro.get("end_time", 150)
                
                if start_time <= current_time <= end_time:
                    # Additional detection patterns
                    detection_patterns = intro.get("detection_patterns", [])
                    
                    # Check for intro indicators in the page
                    page_text = driver.execute_script("return document.body.innerText.toLowerCase();")
                    
                    for pattern in detection_patterns:
                        if pattern.lower() in page_text:
                            return True
                    
                    # If we're in the time window and no specific patterns found, assume it's an intro
                    return True
        
        return False
    except Exception as e:
        logging.error(f"Error detecting intro: {e}")
        return False


def smart_skip_intro(driver, series: str, season: int = 1):
    """Smart intro skipping that only skips when an intro is detected"""
    try:
        # Wait for video to be ready
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script(
                "return document.querySelector('video')?.readyState > 0;"
            )
        )
        
        # Get intro times
        intro_start, intro_end = get_default_intro_times(series, season)
        
        # Check if we should skip intro
        if detect_intro_start(driver, series, season):
            logging.info(f"Intro detected for {series}, skipping to {intro_end} seconds")
            driver.execute_script(f"document.querySelector('video').currentTime = {intro_end};")
        else:
            logging.info(f"No intro detected for {series}, continuing normally")
            
    except Exception as e:
        logging.error(f"Error in smart intro skip: {e}")
        # Fall back to simple skip
        skip_intro(driver, get_intro_skip_seconds(series))


def get_end_skip_seconds(series: str) -> int:
    try:
        data = load_progress().get(series, {})
        val = int(data.get("end_skip", 0))
        return max(0, val)
    except Exception:
        return 0


def set_end_skip_seconds(series: str, seconds: int) -> bool:
    try:
        seconds = max(0, int(seconds))
        db = load_progress()
        entry = db.get(series, {}) if isinstance(db.get(series, {}), dict) else {}
        entry["end_skip"] = seconds
        db[series] = entry
        with open(PROGRESS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"End skip time could not be saved: {e}")
        return False


def norm_series_key(s: str) -> str:
    try:
        return _html.unescape(str(s or "")).strip()
    except Exception:
        return str(s or "").strip()


# === BROWSER HANDLING --------------------------- ===
def start_browser() -> webdriver.Firefox:
    try:
        profile_path = os.path.join(SCRIPT_DIR, "user.BingeWatcher")
        os.makedirs(profile_path, exist_ok=True)

        options = webdriver.FirefoxOptions()
        options.set_preference(
            "dom.popup_allowed_events",
            "change click dblclick mouseup pointerup touchend",
        )
        options.set_preference("dom.allow_scripts_to_close_windows", False)
        options.set_preference("browser.tabs.warnOnClose", False)
        options.set_preference("browser.warnOnQuit", False)
        options.set_preference("browser.sessionstore.warnOnQuit", False)

        options.set_preference("full-screen-api.enabled", True)
        options.set_preference("full-screen-api.allow-trusted-requests-only", False)
        options.set_preference("full-screen-api.mouse-event-allow-button", True)
        options.set_preference("full-screen-api.warning.delay", 0)
        options.set_preference("full-screen-api.warning.timeout", 0)
        options.set_preference("layers.acceleration.disabled", True)
        options.set_preference("gfx.webrender.force-disabled", True)
        options.set_preference("media.wmf.dxva.enabled", False)
        options.set_preference("media.eme.enabled", True)
        options.set_preference("media.gmp-widevinecdm.enabled", True)
        options.set_preference("media.autoplay.default", 0)
        options.set_preference("media.block-autoplay-until-in-foreground", False)
        options.set_preference("media.autoplay.blocking_policy", 0)
        options.set_preference("media.autoplay.allow-muted", True)

        options.set_preference("profile", profile_path)
        options.profile = profile_path

        if USE_TOR_PROXY:
            options.set_preference("network.proxy.type", 1)
            options.set_preference("network.proxy.socks", "127.0.0.1")
            options.set_preference("network.proxy.socks_port", TOR_SOCKS_PORT)
            options.set_preference("network.proxy.socks_remote_dns", True)
        else:
            options.set_preference("network.proxy.type", 0)
            options.set_preference("network.proxy.socks", "")
            options.set_preference("network.proxy.socks_port", 0)
            options.set_preference("network.proxy.socks_remote_dns", False)

        if HEADLESS:
            options.headless = True

        if not os.path.exists(GECKO_DRIVER_PATH):
            raise BingeWatcherError(f"Geckodriver missing under {GECKO_DRIVER_PATH}")

        service = Service(executable_path=GECKO_DRIVER_PATH)
        driver = webdriver.Firefox(service=service, options=options)

        if os.getenv("BW_KIOSK", "false").lower() in {"1", "true", "yes"}:
            try:
                driver.fullscreen_window()
            except Exception:
                pass

        move_to_primary_and_maximize(driver)

        logging.info(
            f"Browser started. Profile: {profile_path} | Tor: {'on' if USE_TOR_PROXY else 'off'}"
        )
        return driver
    except Exception as e:
        logging.error(f"Browser startup failed: {e}")
        raise BingeWatcherError("Browser startup failed")


def arm_window_close_guard(driver):
    try:
        driver.switch_to.default_content()
        driver.execute_script(
            """
            try {
              const _orig = window.close;
              window.close = function(){ console.warn('[BW] window.close() blocked'); };
              try { window.top.close = window.close; } catch(_){}
            } catch(_){}
        """
        )
    except Exception:
        pass


def move_to_primary_and_maximize(driver):
    """Platziert das Fenster auf dem Primärmonitor (Monitor 1) und maximiert es."""
    if HEADLESS:
        return
    try:
        # 1) Windows: Arbeitsbereich (Taskleiste ausgenommen)
        try:
            import ctypes
            from ctypes import wintypes

            SPI_GETWORKAREA = 0x0030
            rect = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
            )
            x, y = int(rect.left), int(rect.top)
            w, h = int(rect.right - rect.left), int(rect.bottom - rect.top)
        except Exception:
            # 2) Sonst: Primärbildschirm-Größe per tkinter
            try:
                import tkinter as tk

                root = tk.Tk()
                root.withdraw()
                w, h = root.winfo_screenwidth(), root.winfo_screenheight()
                root.destroy()
                x, y = 0, 0
            except Exception:
                # 3) Fallback
                x, y, w, h = 0, 0, 1920, 1080

        driver.set_window_position(x, y)
        # Entweder explizit auf Arbeitsbereich…
        driver.set_window_size(w, h)
        # …oder OS-Maximize als Alternative:
        try:
            driver.maximize_window()
        except Exception:
            pass
    except Exception:
        # Letzte Rettung
        try:
            driver.maximize_window()
        except Exception:
            pass


def safe_navigate(
    driver: webdriver.Firefox, url: str, max_retries: int = MAX_RETRIES
) -> bool:
    for attempt in range(max_retries):
        try:
            driver.get(url)
            arm_window_close_guard(driver)
            WebDriverWait(driver, WAIT_TIMEOUT).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(1.0)
            return True
        except WebDriverException as e:
            logging.warning(
                f"Navigation failed (attempt {attempt + 1}/{max_retries}): {e}"
            )
            time.sleep(2)
    logging.error(f"Navigation to {url} failed after {max_retries} attempts")
    return False


def is_browser_responsive(driver: webdriver.Firefox) -> bool:
    try:
        url = driver.current_url
        return bool(url) and url != "about:blank"
    except Exception:
        return False


def _candidate_fs_points(driver):
    """
    Liefert bis zu ~20 Viewport-Koordinaten (x,y), an denen sehr wahrscheinlich ein Fullscreen-Button sitzt.
    Muss im *Frame mit dem Video* aufgerufen werden!
    """
    try:
        pts = driver.execute_script("""
            const out = [];
            const sels = [
                // Video-Element
                'video',
                // JWPlayer
                '.jwplayer', '.jw-controlbar', '.jw-button-container', '.jw-icon-fullscreen',
                // Video.js
                '.vjs-control-bar', '.vjs-fullscreen-control', '.video-js',
                // Plyr
                '.plyr__controls', '.plyr__controls [data-plyr="fullscreen"]', '.plyr',
                // Shaka Player
                '.shaka-controls-container', '.shaka-fullscreen-button',
                // Weitere Player
                '.mejs-controls', '.mejs-fullscreen-button',
                '.flowplayer', '.flowplayer-fullscreen',
                '.dplayer', '.dplayer-fullscreen',
                // Generische Controls
                '[class*="control"]', '[class*="player"]', '[class*="video"]'
            ];
            
            const pushBR = (r) => {
                if (!r || r.width < 20 || r.height < 15) return;
                
                // Bottom-Right Varianten (häufigste Position)
                out.push({x: Math.floor(r.right - 8), y: Math.floor(r.bottom - 8)});
                out.push({x: Math.floor(r.right - 20), y: Math.floor(r.bottom - 12)});
                out.push({x: Math.floor(r.right - 36), y: Math.floor(r.bottom - 16)});
                out.push({x: Math.floor(r.right - 50), y: Math.floor(r.bottom - 20)});
                
                // Center-Right Varianten
                out.push({x: Math.floor(r.right - 8), y: Math.floor(r.top + r.height/2)});
                out.push({x: Math.floor(r.right - 20), y: Math.floor(r.top + r.height/2)});
                
                // Top-Right Varianten
                out.push({x: Math.floor(r.right - 8), y: Math.floor(r.top + 8)});
                out.push({x: Math.floor(r.right - 20), y: Math.floor(r.top + 12)});
            };
            
            const uniq = new Set();
            for (const s of sels) {
                document.querySelectorAll(s).forEach(el => {
                    const r = el.getBoundingClientRect();
                    const k = [r.left, r.top, r.right, r.bottom].map(x => Math.round(x)).join(',');
                    if (uniq.has(k)) return;
                    uniq.add(k);
                    pushBR(r);
                });
            }
            
            // Fallback nur auf video, falls nichts anderes da ist
            if (out.length === 0) {
                const v = document.querySelector('video');
                if (v) { 
                    const r = v.getBoundingClientRect(); 
                    pushBR(r); 
                }
            }
            
            return out.slice(0, 20);
        """)
        return [(int(p["x"]), int(p["y"])) for p in (pts or [])]
    except Exception:
        return []


def _click_viewport_xy(driver, x, y, double=False):
    try:
        builder = ActionBuilder(driver)
        mouse = PointerInput(PointerInput.MOUSE, "mouse")
        builder.add_action(mouse)
        mouse.create_pointer_move(duration=80, x=x, y=y, origin="viewport")
        mouse.create_pointer_down(button=PointerInput.LEFT)
        mouse.create_pointer_up(button=PointerInput.LEFT)
        if double:
            mouse.create_pause(0.05)
            mouse.create_pointer_down(button=PointerInput.LEFT)
            mouse.create_pointer_up(button=PointerInput.LEFT)
        builder.perform()
        return True
    except Exception:
        return False


def _hard_fullscreen_click(driver) -> bool:
    """
    Sucht Kandidatenpunkte und klickt dort „wie ein Mensch" - prüft nach jedem Klick auf Fullscreen.
    Muss im *Frame mit dem Video* aufgerufen werden!
    """
    try:
        _reveal_controls(driver)
    except Exception:
        pass

    points = _candidate_fs_points(driver)
    
    # Verschiedene Klick-Strategien versuchen
    for strategy in ['single', 'double', 'long']:
        for (x, y) in points:
            if strategy == 'single':
                _click_viewport_xy(driver, x, y, double=False)
                time.sleep(0.25)
            elif strategy == 'double':
                _click_viewport_xy(driver, x, y, double=True)
                time.sleep(0.35)
            elif strategy == 'long':
                # Längerer Klick (für manche Player)
                try:
                    builder = ActionBuilder(driver)
                    mouse = PointerInput(PointerInput.MOUSE, "mouse")
                    builder.add_action(mouse)
                    mouse.create_pointer_move(duration=100, x=x, y=y, origin="viewport")
                    mouse.create_pointer_down(button=PointerInput.LEFT)
                    mouse.create_pause(0.3)  # 300ms halten
                    mouse.create_pointer_up(button=PointerInput.LEFT)
                    builder.perform()
                    time.sleep(0.3)
                except Exception:
                    continue
            
            if _is_fullscreen(driver):
                return True
                
            # Kurze Pause zwischen Klicks
            time.sleep(0.1)
    
    return False


# === SETTINGS HANDLING --------------------------- ===
def get_settings(driver):
    file_s = load_settings_file()
    ls_s = read_settings(driver) or {}

    merged = {**file_s, **ls_s}
    merged["autoFullscreen"] = bool(merged.get("autoFullscreen", True))
    merged["autoSkipIntro"] = bool(merged.get("autoSkipIntro", True))
    merged["autoSkipEndScreen"] = bool(merged.get("autoSkipEndScreen", True))
    merged["autoNext"] = bool(merged.get("autoNext", True))
    merged["playbackRate"] = float(merged.get("playbackRate", 1))
    merged["volume"] = float(merged.get("volume", 1))

    return merged


def load_settings_file() -> Dict[str, Any]:
    try:
        if os.path.exists(SETTINGS_DB_FILE):
            with open(SETTINGS_DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    d = _default_settings()
                    d.update({k: data[k] for k in data if k in d})
                    return d
        return _default_settings()
    except Exception as e:
        logging.warning(f"Settings failed to load: {e}")
        return _default_settings()


def save_settings_file(settings: Dict[str, Any]) -> bool:
    try:
        d = _default_settings()

        for k in d.keys():
            if k in settings:
                d[k] = settings[k]

        with open(SETTINGS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"Saving settings failed: {e}")
        return False


def sync_settings_to_localstorage(driver):
    """Schreibt Datei-Settings in localStorage, falls dort leer/nicht gesetzt."""
    try:
        driver.switch_to.default_content()
        need = driver.execute_script(
            """
            try {
                const raw = localStorage.getItem('bw_settings');
                if (!raw || raw.trim() === '' ) return true;
                const obj = JSON.parse(raw);
                if (!obj || typeof obj !== 'object') return true;
                return false;
            } catch(e){ return true; }
        """
        )
        if need:
            s = load_settings_file()
            driver.execute_script(
                "localStorage.setItem('bw_settings', arguments[0]);", json.dumps(s)
            )
    except Exception as e:
        logging.debug(f"sync_settings_to_localstorage: {e}")


def _default_settings() -> Dict[str, Any]:
    return {
        "autoFullscreen": True,
        "autoSkipIntro": True,
        "autoSkipEndScreen": False,
        "autoNext": True,
        "playbackRate": 1.0,
        "volume": 1.0,
    }


# === NAVIGATION HANDLING --------------------------- ===
def slugify_series(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9\-]+", "", s)
    return s

def detect_provider_from_url(url: str) -> Optional[str]:
    """Erkennt den Streaming-Anbieter aus der URL."""
    for provider_id, provider_info in STREAMING_PROVIDERS.items():
        if provider_id in url:
            return provider_id
    return None

def parse_episode_info(url):
    """Erweiterte Episode-Info-Parsing für verschiedene Streaming-Anbieter."""
    for provider_id, provider_info in STREAMING_PROVIDERS.items():
        m = re.search(provider_info["url_pattern"], url)
        if m:
            series = m.group(1).lower()
            season = int(m.group(2))
            episode = int(m.group(3)) if m.group(3) else None
            return series, season, episode, provider_id
    return None, None, None, None


def navigate_to_episode(driver, series, season, episode, db, provider="s.to"):
    """Navigiert zu einer Episode mit Unterstützung für verschiedene Streaming-Anbieter."""
    series = slugify_series(series)  # <- Eingabe normalize
    
    # Verwende den entsprechenden Anbieter
    provider_info = STREAMING_PROVIDERS.get(provider, STREAMING_PROVIDERS["s.to"])
    target = provider_info["episode_url_template"].format(
        series=series, season=season, episode=episode
    )
    
    driver.get(target)
    arm_window_close_guard(driver)
    time.sleep(2)

    cur = driver.current_url
    a_series, a_season, a_episode, a_provider = parse_episode_info(cur)

    if a_series and a_season and a_episode is None:
        try:
            driver.switch_to.default_content()
            # Anbieter-spezifische CSS-Selektoren
            if provider == "s.to":
                selector = f'a[href*="/staffel-{a_season}/episode-"]'
            elif provider == "aniworld.to":
                selector = f'a[href*="/staffel-{a_season}/episode-"]'
            else:
                selector = f'a[href*="/staffel-{a_season}/episode-"]'
                
            link = WebDriverWait(driver, 6).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            href = link.get_attribute("href")
            if href:
                driver.get(href)
                time.sleep(1)
                a_series, a_season, a_episode, a_provider = parse_episode_info(driver.current_url)
        except Exception:
            # Fallback zur ersten Episode
            fallback_url = provider_info["episode_url_template"].format(
                series=a_series or series, season=a_season or season, episode=1
            )
            driver.get(fallback_url)
            time.sleep(1)
            a_series, a_season, a_episode, a_provider = parse_episode_info(driver.current_url)

    if not a_series:
        inject_sidebar(driver, db); clear_nav_lock(driver)
        return series, season, episode, provider

    inject_sidebar(driver, db); clear_nav_lock(driver)
    return a_series or series, a_season or season, a_episode or episode, a_provider or provider


def find_and_switch_to_video_frame(driver, timeout=12) -> bool:
    """Search up to depth 2 for a <video> and switch to the appropriate frame."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            driver.switch_to.default_content()
            if driver.execute_script("return !!document.querySelector('video')"):
                return True
        except Exception:
            pass

        try:
            frames_lvl1 = driver.find_elements(By.TAG_NAME, "iframe")
        except Exception:
            frames_lvl1 = []

        for f1 in frames_lvl1:
            try:
                driver.switch_to.default_content()
                _arm_iframe_for_fullscreen(driver, f1)
                driver.switch_to.frame(f1)
                if driver.execute_script("return !!document.querySelector('video')"):
                    return True

                try:
                    frames_lvl2 = driver.find_elements(By.TAG_NAME, "iframe")
                except Exception:
                    frames_lvl2 = []
                for f2 in frames_lvl2:
                    try:
                        _arm_iframe_for_fullscreen(driver, f2)
                        driver.switch_to.frame(f2)
                        if driver.execute_script(
                            "return !!document.querySelector('video')"
                        ):
                            return True
                    finally:
                        driver.switch_to.parent_frame()
            except Exception:
                pass

        time.sleep(0.25)
    return False


def ensure_video_context(driver) -> bool:
    try:
        return find_and_switch_to_video_frame(driver, timeout=6)
    except Exception:
        return False


def safe_save_progress(driver, series, season, episode, provider="s.to") -> int:
    pos = 0
    try:
        if ensure_video_context(driver):
            try:
                pos = int(
                    driver.execute_script(
                        "return (document.querySelector('video')?.currentTime||0)"
                    )
                )
            except Exception:
                pos = 0
        save_progress(series, season, episode, pos, provider=provider)
    except Exception:
        pass
    return pos


def cleanup_before_switch(driver):
    try:
        try:
            if ensure_video_context(driver):
                pause_video(driver)
        except Exception:
            pass
        exit_fullscreen(driver)
        _hide_sidebar(driver, False)
        time.sleep(0.2)
    except Exception:
        pass


def poll_ui_flags(driver):
    driver.switch_to.default_content()
    return driver.execute_script(
        """
      const out={};
      try{out.quit = localStorage.getItem('bw_quit')==='1'; localStorage.removeItem('bw_quit');}catch(_){}
      try{out.skip = localStorage.getItem('bw_skip_now')==='1'; localStorage.removeItem('bw_skip_now');}catch(_){}
      try{out.del  = localStorage.getItem('bw_seriesToDelete'); if(out.del) localStorage.removeItem('bw_seriesToDelete');}catch(_){}
      try{ out.sel = localStorage.getItem('bw_series'); }catch(_){}
      return out;
    """
    )


def popout_player_iframe(driver) -> bool:
    """Opens the src of the first relevant iframe in the same tab. Returns true if navigation occurred.."""
    try:
        driver.switch_to.default_content()
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        if not iframes:
            return False

        best, area = None, 0
        for fr in iframes:
            try:
                r = fr.rect
                a = r.get("width", 0) * r.get("height", 0)
                if a > area:
                    best, area = fr, a
            except Exception:
                pass
        if not best:
            return False
        src = best.get_attribute("src") or ""
        if not src or src.startswith("about:"):
            return False
        driver.get(src)
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        return True
    except Exception:
        return False


def play_episodes_loop(driver, series, season, episode, position=0, provider="s.to"):
    global should_quit
    current_episode = episode
    current_season = season
    current_provider = provider
    
    # Spezielle Behandlung für One Piece (Staffel 11 Problem)
    is_one_piece = series.lower() in ['one-piece', 'one piece', 'onepiece']

    while True:
        db = load_progress()
        settings = get_settings(driver)
        auto_fs = settings["autoFullscreen"]
        auto_skip = settings["autoSkipIntro"]
        auto_skip_end = settings["autoSkipEndScreen"]
        auto_next = settings["autoNext"]
        rate = settings["playbackRate"]
        vol = settings["volume"]

        print(
            f"\n[▶] Playing {series.capitalize()} – Season {current_season}, Episode {current_episode}"
        )
        
        # Navigiere zur Episode und prüfe auf Weiterleitungen
        new_series, actual_season, actual_episode, actual_provider = navigate_to_episode(driver, series, current_season, current_episode, db, current_provider)

        if new_series != series:
            logging.info(f"Canonical slug applied: {series} → {new_series}")
            series = new_series
            is_one_piece = series.replace('-', '').replace(' ', '') in ('onepiece',)

        if (actual_season != current_season) or (actual_episode is not None and actual_episode != current_episode) or (actual_provider != current_provider):
            logging.info(f"Navigation angepasst: S{current_season}E{current_episode} → S{actual_season}E{actual_episode} (Provider: {current_provider} → {actual_provider})")
            current_season = actual_season
            current_provider = actual_provider
            if actual_episode is not None:
                current_episode = actual_episode
                save_progress(series, current_season, current_episode, 0, provider=current_provider)
            
        sync_settings_to_localstorage(driver)

        if not ensure_video_context(driver):
            ok_ctx = False
            for _ in range(3):
                time.sleep(0.4)
                if ensure_video_context(driver):
                    ok_ctx = True
                    break
            if not ok_ctx:
                break

        play_video(driver)
        apply_media_settings(driver, rate, vol)
        
        if position and position > 0:
            skip_intro(driver, position)
        elif auto_skip:
            smart_skip_intro(driver, series, current_season)
        position = 0

        recovery_tries = 0
        while detect_232011(driver) and recovery_tries < 3:
            logging.warning("JW 232011 detected - attempting recovery...")
            recovery_tries += 1

            try:
                driver.execute_script(
                    """
                    const v = document.querySelector('video');
                    if (v){ const t = v.currentTime || 0; v.load?.(); v.currentTime = t; }
                """
                )
                time.sleep(0.6)
                play_video(driver)
                if not detect_232011(driver):
                    break
            except Exception:
                pass

            if popout_player_iframe(driver):
                ensure_video_context(driver)
                play_video(driver)
                if not detect_232011(driver):
                    break

            driver.refresh()
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            ensure_video_context(driver)
            play_video(driver)

        try:
            const_ser = series
            const_secs = get_intro_skip_seconds(const_ser)
            const_secs_end = get_intro_skip_end_seconds(const_ser)
            driver.execute_script(
                """
                const v = document.querySelector('video');
                const secs = arguments[0];
                const secsEnd = arguments[1];
                if (!v || !isFinite(v.duration)) return;
                if (secsEnd <= secs || secsEnd >= (v.duration - 1)) return;
                const currentTime = v.currentTime;
                if (currentTime >= secs && currentTime <= secsEnd) {
                    v.currentTime = secsEnd;
                    try { v.play().catch(()=>{}); } catch(_) {}
                }
            """,
                const_secs,
                const_secs_end,
            )
        except Exception:
            pass

        if auto_fs and not HEADLESS:
            _hide_sidebar(driver, True)
            ensure_video_context(driver)
            time.sleep(0.1)
            
            ok = enable_fullscreen(driver)
            
            if not ok and os.getenv("BW_POPOUT_IFRAME", "false").lower() in {
                "1",
                "true",
                "yes",
            }:
                if popout_player_iframe(driver):
                    ensure_video_context(driver)
                    _hide_sidebar(driver, True)
                    ensure_video_context(driver)
                    time.sleep(0.1)
                    ok = enable_fullscreen(driver)

        try:
            for _ in range(8):
                playing = driver.execute_script(
                    "const v=document.querySelector('video'); return !!(v && !v.paused && v.readyState>2);"
                )
                if playing:
                    break
                driver.execute_script(
                    "const v=document.querySelector('video'); if(v){ try{ v.focus(); v.play().catch(()=>{}); }catch(e){} }"
                )
                time.sleep(0.1)
        except Exception:
            pass

        initial_src = ""
        try:
            initial_src = (
                driver.execute_script(
                    "const v=document.querySelector('video');return v?(v.currentSrc||v.src||''):'';"
                )
                or ""
            )
        except Exception:
            pass

        user_switched = False
        last_save = time.time()

        try:
            cur_pos = 0
            try:
                if ensure_video_context(driver):
                    cur_pos = int(
                        driver.execute_script(
                            "return (document.querySelector('video')?.currentTime||0)"
                        )
                        or 0
                    )
            except Exception:
                cur_pos = 0

            save_progress(series, current_season, current_episode, cur_pos, provider=current_provider)

            driver.switch_to.default_content()
            html = build_items_html(load_progress(), settings)
            driver.execute_script(
                "if (window.__bwSetList){window.__bwSetList(arguments[0]);}",
                html,
            )
        except Exception:
            pass
        finally:
            try:
                ensure_video_context(driver)
            except Exception:
                pass
            
        auto_nav = False

        while True:
            flags = poll_ui_flags(driver)

            if flags.get("sel"):
                safe_save_progress(driver, series, current_season, current_episode, current_provider)
                cleanup_before_switch(driver)
                time.sleep(0.5)

                try:
                    driver.switch_to.default_content()
                    driver.execute_script(
                        "document.cookie = 'bw_series=' + encodeURIComponent(arguments[0]) + '; path=/';",
                        flags["sel"],
                    )
                finally:
                    ensure_video_context(driver)

                user_switched = True
                clear_nav_lock(driver)
                break

            try:
                driver.switch_to.default_content()
                cur_url = driver.current_url or ""
                s2, se2, ep2, p2 = parse_episode_info(cur_url)

                if s2 == series and (se2 is not None and ep2 is not None) \
                and (se2 != current_season or ep2 != current_episode):
                    # Interne Auto-Navigation (z. B. Next Episode / Redirect)
                    safe_save_progress(driver, series, current_season, current_episode, current_provider)
                    current_season, current_episode = se2, ep2
                    auto_nav = True
                    break  # raus aus innerer Loop, outer Loop startet mit aktualisiertem Zustand

                elif s2 and s2 != series:
                    # Wirklicher Serienwechsel (vom User)
                    safe_save_progress(driver, series, current_season, current_episode, current_provider)
                    cleanup_before_switch(driver)
                    time.sleep(0.5)
                    user_switched = True
                    break
            finally:
                ensure_video_context(driver)

            try:
                cur_src = (
                    driver.execute_script(
                        "const v=document.querySelector('video');return v?(v.currentSrc||v.src||''):'';"
                    )
                    or ""
                )
                if initial_src and cur_src and cur_src != initial_src:
                    try:
                        WebDriverWait(driver, 10).until(
                            lambda d: d.execute_script(
                                "return document.querySelector('video')?.readyState>0;"
                            )
                        )
                    except Exception:
                        pass
                    apply_media_settings(driver, rate, vol)

                    try:
                        if auto_fs:
                            _hide_sidebar(driver, True)
                            ensure_video_context(driver)
                            enable_fullscreen(driver)
                    except Exception:
                        pass
                    initial_src = cur_src
            except Exception:
                pass

            # --- LIVE SETTINGS UPDATE ---------------------------------------
            try:
                raw = driver.execute_script(
                    """
                    let r = localStorage.getItem('bw_settings_update');
                    if (r) localStorage.removeItem('bw_settings_update');
                    return r;
                """
                )
                if raw:
                    upd = json.loads(raw)
                    # Datei persistieren
                    save_settings_file(upd)

                    # Lokale Variablen MERGEN
                    auto_fs = bool(upd.get("autoFullscreen", auto_fs))
                    auto_skip = bool(upd.get("autoSkipIntro", auto_skip))
                    auto_skip_end = bool(upd.get("autoSkipEndScreen", auto_skip_end))
                    auto_next = bool(upd.get("autoNext", auto_next))
                    rate = float(upd.get("playbackRate", rate))
                    vol = float(upd.get("volume", vol))

                    # In-memory Settings-Objekt konsistent halten
                    settings.update(
                        {
                            "autoFullscreen": auto_fs,
                            "autoSkipIntro": auto_skip,
                            "autoSkipEndScreen": auto_skip_end,
                            "autoNext": auto_next,
                            "playbackRate": rate,
                            "volume": vol,
                        }
                    )

                    # Sofort auf das Video anwenden
                    try:
                        if ensure_video_context(driver):
                            apply_media_settings(driver, rate, vol)
                    finally:
                        try:
                            driver.switch_to.default_content()
                        except Exception:
                            pass

                    # Fullscreen bei Änderung direkt toggeln
                    try:
                        driver.switch_to.default_content()
                        const_fs = _is_fullscreen(driver)
                        ensure_video_context(driver)
                        if auto_fs and not const_fs and not HEADLESS:
                            _hide_sidebar(driver, True)
                            ensure_video_context(driver)
                            time.sleep(0.45)
                            enable_fullscreen(driver)
                        elif not auto_fs and const_fs:
                            exit_fullscreen(driver)
                            _hide_sidebar(driver, False)
                    except Exception:
                        pass

                    # LocalStorage mit Datei-Version synchron halten
                    try:
                        driver.execute_script(
                            "localStorage.setItem('bw_settings', arguments[0]);",
                            json.dumps(load_settings_file()),
                        )
                    except Exception:
                        pass
                    
                    # UI sofort aktualisieren, um neue Eingabefelder anzuzeigen/verstecken
                    try:
                        html = build_items_html(load_progress(), settings)
                        driver.execute_script(
                            "if (window.__bwSetList){window.__bwSetList(arguments[0]);}",
                            html,
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            # ----------------------------------------------------------------

            if flags.get("quit"):
                should_quit = True
                break

            if flags.get("del"):
                deleted = str(flags["del"])
                handle_list_item_deletion(deleted)
                try:
                    driver.switch_to.default_content()
                    settings = get_settings(driver)
                    html = build_items_html(load_progress(), settings)
                    driver.execute_script(
                        "if (window.__bwSetList){window.__bwSetList(arguments[0]);}",
                        html,
                    )
                finally:
                    ensure_video_context(driver)

                if deleted == series:
                    try:
                        cleanup_before_switch(driver)
                        time.sleep(0.5)
                        driver.switch_to.default_content()

                        driver.execute_script(
                            """
                            try { localStorage.removeItem('bw_series'); } catch(e){}
                            document.cookie = 'bw_series=; expires=Thu, 01 Jan 1970 00:00:01 GMT; path=/';
                        """
                        )
                    except Exception:
                        pass

                    safe_navigate(driver, START_URL)
                    arm_window_close_guard(driver)
                    return

            if not ensure_video_context(driver):
                time.sleep(0.2)
                if not ensure_video_context(driver):
                    break

            if flags.get("skip"):
                try:
                    driver.execute_script(
                        """
                        const v = document.querySelector('video');
                        if (v && isFinite(v.duration) && v.duration > 1) {
                            v.currentTime = Math.max(0, v.duration - 1);
                            try { v.muted = true; v.play(); } catch(_){}
                        }
                    """
                    )
                except Exception:
                    pass

            remaining_time = driver.execute_script(
                """
                const v = document.querySelector('video');
                if (!v || !isFinite(v.duration)) return 99999;
                return v.duration - v.currentTime;
            """
            )

            now = time.time()
            if now - last_save >= PROGRESS_SAVE_INTERVAL:
                current_pos = get_current_position(driver)
                save_progress(series, current_season, current_episode, int(current_pos), provider=current_provider)
                last_save = now

            # End-Screen-Skip Logik
            if auto_skip_end:
                end_skip_seconds = get_end_skip_seconds(series)
                if end_skip_seconds > 0 and remaining_time <= end_skip_seconds:
                    try:
                        driver.execute_script(
                            """
                            const v = document.querySelector('video');
                            if (v && isFinite(v.duration)) {
                                const skipTo = Math.max(0, v.duration - arguments[0]);
                                if (v.currentTime < skipTo) {
                                    v.currentTime = skipTo;
                                    try { v.play().catch(()=>{}); } catch(_) {}
                                }
                            }
                        """,
                            end_skip_seconds,
                        )
                    except Exception:
                        pass
                    # Wenn wir das Ende überspringen, warten wir kurz und brechen dann ab
                    time.sleep(0.5)
                    break
            
            if remaining_time <= 3:
                break

            time.sleep(1.0)

        if auto_nav:
            position = get_intro_skip_seconds(series) if auto_skip else 0
            continue

        exit_fullscreen(driver)
        _hide_sidebar(driver, False)
        time.sleep(0.5)

        if should_quit:
            return

        if user_switched:
            return

        if not auto_next:
            return

        # Spezielle Behandlung für One Piece: Verhindere Sprung zu Staffel 11
        if is_one_piece and current_season == 1:
            # Prüfe, ob die nächste Episode in Staffel 1 existiert
            next_episode = current_episode + 1
            
            # Versuche zur nächsten Episode zu navigieren, um zu prüfen, ob sie existiert
            test_url = f"https://s.to/serie/stream/{series}/staffel-{current_season}/episode-{next_episode}"
            try:
                driver.get(test_url)
                arm_window_close_guard(driver)
                time.sleep(3)  # Längere Wartezeit für bessere Erkennung
                
                # Prüfe, ob wir zur richtigen Episode weitergeleitet wurden
                current_url = driver.current_url
                parsed_info = parse_episode_info(current_url)
                
                if parsed_info:
                    test_series, test_season, test_episode, test_provider = parsed_info
                    
                    # Prüfe explizit auf Staffel 11 Weiterleitung
                    if test_series == series and test_season == 11:
                        logging.warning(f"One Piece: Staffel 11 Weiterleitung erkannt! S{test_season}E{test_episode}")
                        logging.info(f"One Piece: Beende Staffel 1 bei Episode {current_episode}")
                        return  # Beende die Schleife, da Staffel 1 zu Ende ist
                    
                    # Prüfe, ob wir zur richtigen Episode weitergeleitet wurden
                    if test_series == series and test_season == current_season and test_episode == next_episode:
                        # Episode existiert in Staffel 1, normal fortfahren
                        current_episode = next_episode
                        logging.info(f"One Piece: Nächste Episode S{current_season}E{current_episode} gefunden")
                    else:
                        # Unerwartete Weiterleitung
                        logging.warning(f"One Piece: Unerwartete Weiterleitung zu S{test_season}E{test_episode}")
                        # Prüfe, ob es sich um Staffel 11 handelt
                        if test_season == 11:
                            logging.info(f"One Piece: Beende Staffel 1 bei Episode {current_episode} (Staffel 11 erkannt)")
                            return  # Beende die Schleife
                        else:
                            # Andere unerwartete Weiterleitung, versuche es trotzdem
                            current_episode = next_episode
                else:
                    # URL konnte nicht geparst werden, prüfe manuell auf Staffel 11
                    if "staffel-11" in current_url.lower():
                        logging.warning(f"One Piece: Staffel 11 in URL erkannt: {current_url}")
                        logging.info(f"One Piece: Beende Staffel 1 bei Episode {current_episode}")
                        return  # Beende die Schleife
                    else:
                        # URL konnte nicht geparst werden, vermutlich existiert die Episode nicht
                        logging.info(f"One Piece: Episode {next_episode} in Staffel 1 existiert nicht")
                        return  # Beende die Schleife
            except Exception as e:
                logging.error(f"Fehler beim Testen der nächsten One Piece Episode: {e}")
                # Bei Fehler trotzdem zur nächsten Episode
                current_episode = next_episode
        else:
            # Normale Episode-Inkrementierung für andere Serien
            current_episode += 1
            
        position = get_intro_skip_seconds(series) if auto_skip else 0
        continue


def _reveal_controls(driver):
    try:
        v = WebDriverWait(driver, 3).until(
            EC.presence_of_element_located((By.TAG_NAME, "video"))
        )
        ActionChains(driver).move_to_element(v).pause(0.05).move_by_offset(
            0, 0
        ).perform()
        time.sleep(0.1)
    except Exception:
        pass


def _mark_probable_fs_button(driver):
    """Markiere wahrscheinlichen Vollbild-Button mit data-bw-fullscreen='1' (Heuristik)."""
    driver.execute_script(
        """
        const v = document.querySelector('video'); if (!v) return;
        const vr = v.getBoundingClientRect();
        const cand = Array.from(document.querySelectorAll('button,[role="button"],[class*="control"],[class*="fullscreen"],[class*="player"],[aria-label],[title]'));
        let best = null, score = -1;
        
        const labelHit = el => {
            const text = (el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent || '') + ' ' + (el.className || '');
            return text.toLowerCase().match(/vollbild|full.?screen|fullscreen|maximi|expand|zoom/);
        };
        
        const vis = el => { 
            const s = getComputedStyle(el); 
            const r = el.getBoundingClientRect(); 
            return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 12 && r.height > 12;
        };
        
        cand.forEach(el => {
            if (!vis(el)) return;
            const r = el.getBoundingClientRect();
            
            // Nähe zur rechten unteren Ecke des Videos
            const cx = (r.left + r.right) / 2, cy = (r.top + r.bottom) / 2;
            let s = -Math.hypot(cx - vr.right, cy - vr.bottom);
            
            // Scoring-System
            if (labelHit(el)) s += 500;
            if ((el.className || '').toLowerCase().includes('full')) s += 250;
            if ((el.className || '').toLowerCase().includes('screen')) s += 200;
            if ((el.className || '').toLowerCase().includes('expand')) s += 150;
            if ((el.className || '').toLowerCase().includes('zoom')) s += 150;
            
            // Bestrafung für Elemente außerhalb des Videos
            if (r.right < vr.left - 20 || r.left > vr.right + 20 || r.bottom < vr.top - 20 || r.top > vr.bottom + 20) s -= 200;
            
            // Bonus für kleine, quadratische Buttons (typisch für Vollbild-Buttons)
            const aspectRatio = Math.abs(r.width - r.height);
            if (aspectRatio < 5 && r.width < 50 && r.height < 50) s += 100;
            
            if (s > score) { score = s; best = el; }
        });
        
        if (best) best.setAttribute('data-bw-fullscreen', '1');
    """
    )


def _gesture_fullscreen_in_frame(driver) -> bool:
    try:
        driver.execute_script("""
            const v = document.querySelector('video'); if(!v) return false;
            if (window.__bw_fs_armed) return true;
            window.__bw_fs_armed = true;
            const tryFS = ()=>{
                let el=v;
                for(let i=0;i<4 && el && el.parentElement; i++) el = el.parentElement;
                const tgt = el || v;
                const p = (tgt.requestFullscreen?.() || tgt.webkitRequestFullscreen?.() || tgt.mozRequestFullScreen?.());
                if (p && p.catch) p.catch(()=>{});
            };
            const once = (ev)=>{ document.removeEventListener('click', once, true); tryFS(); setTimeout(()=>{window.__bw_fs_armed=false;},0); };
            document.addEventListener('click', once, true);
            return true;
        """)
        v = driver.find_element(By.TAG_NAME, "video")
        ActionChains(driver).move_to_element(v).click().perform()
        time.sleep(0.25)
        return _is_fullscreen(driver)
    except Exception:
        return False


def _gesture_fullscreen_on_iframe_from_top(driver) -> bool:
    """Aus Iframe-Kontext aufrufen! Holt die frameElement-ID, wechselt nach oben und
       ruft requestFullscreen() auf dem <iframe> im echten Click-Handler auf."""
    try:
        iframe_id = driver.execute_script("""
            const f = window.frameElement || null;
            return f && f.id ? f.id : (f ? (f.id = 'bw_iframe_' + Math.random().toString(36).slice(2)) : null);
        """)
        driver.switch_to.default_content()
        if not iframe_id:
            return False
        iframe = driver.find_element(By.ID, iframe_id)
        _arm_iframe_for_fullscreen(driver, iframe)

        driver.execute_script("""
            const f = arguments[0];
            if (!window.__bw_fs_top_armed){
                window.__bw_fs_top_armed = true;
                const handler = ()=>{ document.removeEventListener('click', handler, true);
                    const p = (f.requestFullscreen?.()|| f.webkitRequestFullscreen?.()|| f.mozRequestFullScreen?.());
                    if (p && p.catch) p.catch(()=>{});
                    setTimeout(()=>{ window.__bw_fs_top_armed = false; }, 0);
                };
                document.addEventListener('click', handler, true);
            }
        """, iframe)
        ActionChains(driver).move_to_element(iframe).click().perform()
        time.sleep(0.25)
        ok = bool(driver.execute_script("return !!document.fullscreenElement"))
        if ok:
            try: driver.switch_to.frame(iframe)
            except Exception: pass
        return ok
    except Exception:
        return False


def _hide_sidebar(driver, hide: bool):
    try:
        driver.switch_to.default_content()
        if hide:
            driver.execute_script(
                """
                const s = document.getElementById('bingeSidebar');
                if (s){ s.dataset._prevDisplay = s.style.display || ''; s.style.display = 'none'; }
            """
            )
        else:
            driver.execute_script(
                """
                const s = document.getElementById('bingeSidebar');
                if (s){ s.style.display = s.dataset._prevDisplay || ''; delete s.dataset._prevDisplay; }
            """
            )
    finally:
        try:
            ensure_video_context(driver)
        except:
            pass


def _arm_iframe_for_fullscreen(driver, iframe_el):
    try:
        driver.execute_script("""
            const f = arguments[0];
            try{
              const cur = (f.getAttribute('allow') || '');
              const want = ['fullscreen','fullscreen *','autoplay','autoplay *','encrypted-media'];
              const merged = Array.from(new Set(
                cur.split(';').map(s=>s.trim()).filter(Boolean).concat(want)
              )).join('; ');
              f.setAttribute('allow', merged);
            }catch(_){}
            try{ f.setAttribute('allowfullscreen',''); }catch(_){}
        """, iframe_el)
    except Exception:
        pass


# === VIDEO FUNCTIONS ===
def exit_fullscreen(driver):
    try:
        driver.switch_to.default_content()
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.5)
    except:
        pass


def switch_to_video_frame(driver):
    try:
        iframe = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "iframe"))
        )
        driver.switch_to.frame(iframe)
        return True
    except:
        print("[!] Video iframe not found.")
        return False


def play_video(driver):
    """Start robust: Overlays klicken, Video fokusieren, play() als Fallback."""
    try:
        # Wir sind im iframe (ensure_video_context vorher!)
        driver.execute_script(
            "const v=document.querySelector('video'); if(v){ v.muted=true; }"
        )

        # 1) Schneller: Direkt play() versuchen (häufig erfolgreich)
        try:
            driver.execute_script(
                """
                const v=document.querySelector('video');
                if (v && v.paused) { 
                    try{ 
                        v.play().catch(()=>{}); 
                        return true;
                    }catch(e){ 
                        return false; 
                    } 
                }
                return true;
            """
            )
        except Exception:
            pass

        # 2) Falls nötig: Overlay-Buttons klicken (reduzierte Pausen)
        overlay_selectors = [
            ".vjs-big-play-button",
            ".jw-display-icon-container",
            ".jw-display",
            ".plyr__control--overlaid",
            'button[aria-label*="Play" i]',
            'button[class*="play"]',
            ".shaka-play-button",
        ]
        for sel in overlay_selectors:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed() and el.is_enabled():
                    ActionChains(driver).move_to_element(el).click().perform()
                    time.sleep(0.08)
                    break
            except Exception:
                pass

        # 3) Falls nötig: direkt auf das <video> klicken
        try:
            v = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((By.TAG_NAME, "video"))
            )
            ActionChains(driver).move_to_element(v).click().perform()
            time.sleep(0.03)
        except Exception:
            pass

        # 4) Finaler play() Aufruf
        driver.execute_script(
            """
            const v=document.querySelector('video');
            if (v && v.paused) { try{ v.play().catch(()=>{}); }catch(e){} }
        """
        )
    except Exception as e:
        print(f"[!] Could not start video: {e}")


def pause_video(driver):
    try:
        driver.execute_script(
            """
            const v=document.querySelector('video');
            if (v) { try{ v.pause(); }catch(e){} }
        """
        )
    except Exception:
        pass


def enable_fullscreen(driver):
    """
    Verbesserte Vollbild-Aktivierung mit mehreren Fallback-Strategien.
    Berücksichtigt User-Gesture-Requirements und verschiedene Player-APIs.
    """
    try:
        ensure_video_context(driver)
        if _is_fullscreen(driver):
            return True

        # 1. Zuerst sicherstellen, dass wir im richtigen Kontext sind
        try:
            driver.switch_to.default_content()
            driver.execute_script("try{ window.focus(); }catch(_){ }")
            # Echten Klick auf Body für User-Gesture
            try:
                iframe = WebDriverWait(driver, 3).until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
                _arm_iframe_for_fullscreen(driver, iframe)
                ActionChains(driver).move_to_element(iframe).click().perform()
                time.sleep(0.05)
            except Exception:
                pass
        finally:
            ensure_video_context(driver)
            _reveal_controls(driver)

        # 2. Player-spezifische Vollbild-Buttons suchen und klicken
        fullscreen_selectors = [
            # JWPlayer
            ".jw-icon-fullscreen",
            ".jw-display-icon-container .jw-icon-fullscreen",
            ".jw-controlbar .jw-icon-fullscreen",
            # Video.js
            ".vjs-fullscreen-control",
            ".vjs-control-bar .vjs-fullscreen-control",
            # Plyr
            ".plyr__control--fullscreen",
            ".plyr__controls [data-plyr='fullscreen']",
            # Shaka Player
            ".shaka-fullscreen-button",
            ".shaka-controls-container .shaka-fullscreen-button",
            # Generische Vollbild-Buttons
            'button[aria-label*="full" i]',
            'button[title*="full" i]',
            'button[aria-label*="Vollbild" i]',
            '[class*="fullscreen" i]',
            '[class*="full-screen" i]',
            # Weitere Player
            ".mejs-fullscreen-button",
            ".flowplayer-fullscreen",
            ".dplayer-fullscreen",
        ]

        for sel in fullscreen_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in elements:
                    if el.is_displayed() and el.is_enabled():
                        try:
                            ActionChains(driver).move_to_element(el).click().perform()
                            time.sleep(0.15)
                            if _is_fullscreen(driver):
                                return True
                        except Exception:
                            pass
                        
                        try:
                            driver.execute_script("arguments[0].click();", el)
                            time.sleep(0.15)
                            if _is_fullscreen(driver):
                                return True
                        except Exception:
                            pass
            except Exception:
                continue

        # 3. Intelligente Button-Erkennung mit Heuristik
        _mark_probable_fs_button(driver)
        try:
            btn = driver.find_element(By.CSS_SELECTOR, '[data-bw-fullscreen="1"]')
            ActionChains(driver).move_to_element(btn).click().perform()
            time.sleep(0.3)
            if _is_fullscreen(driver):
                return True
        except Exception:
            pass

        # 4. Video-Doppelklick (häufig verwendete Methode)
        try:
            v = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((By.TAG_NAME, "video"))
            )
            # Erst einfacher Klick für User-Gesture
            ActionChains(driver).move_to_element(v).click().perform()
            time.sleep(0.05)
            # Dann Doppelklick
            ActionChains(driver).move_to_element(v).double_click().perform()
            time.sleep(0.15)
            if _is_fullscreen(driver):
                return True
        except Exception:
            pass

        # 5. Tastatur-Shortcuts
        try:
            # Video fokussieren
            driver.execute_script(
                """
                const v = document.querySelector('video');
                if (v) {
                    v.tabIndex = 0;
                    v.focus();
                    // Event-Listener für 'f' hinzufügen
                    if (!v.__bw_fs_listener) {
                        v.__bw_fs_listener = true;
                        document.addEventListener('keydown', (e) => {
                            if (e.key === 'f' || e.key === 'F') {
                                e.preventDefault();
                                const target = v.parentElement || v;
                                const p = (target.requestFullscreen?.() || 
                                          target.webkitRequestFullscreen?.() || 
                                          target.mozRequestFullScreen?.());
                                if (p && p.catch) p.catch(() => {});
                            }
                        }, { passive: false });
                    }
                }
            """
            )

            ActionChains(driver).send_keys("f").pause(0.05).perform()
            time.sleep(0.15)
            if _is_fullscreen(driver):
                return True
        except Exception:
            pass

        # 6. Iframe-spezifische Vollbild-Aktivierung
        try:
            iframe_id = driver.execute_script(
                """
                const f = window.frameElement || null;
                if (!f) return null;
                if (!f.id) f.id = 'bw_iframe_' + Math.random().toString(36).slice(2);
                return f.id;
            """
            )

            if iframe_id:
                driver.switch_to.default_content()
                try:
                    target_iframe = driver.find_element(By.ID, iframe_id)
                    _arm_iframe_for_fullscreen(driver, target_iframe)
                    
                    # Iframe klicken für User-Gesture
                    ActionChains(driver).move_to_element(target_iframe).click().perform()
                    time.sleep(0.05)
                    
                    # Vollbild über Iframe versuchen
                    driver.execute_script(
                        """
                        const f = arguments[0];
                        const p = (f.requestFullscreen?.() || 
                                  f.webkitRequestFullscreen?.() || 
                                  f.mozRequestFullScreen?.());
                        if (p && p.catch) p.catch(() => {});
                    """,
                        target_iframe,
                    )
                    time.sleep(0.2)
                    
                    if _is_fullscreen(driver):
                        try:
                            driver.switch_to.frame(target_iframe)
                        except Exception:
                            pass
                        return True
                except Exception:
                    pass
                finally:
                    ensure_video_context(driver)
        except Exception:
            pass

        # 7. Geckodriver-spezifische Viewport-Klicks
        try:
            if _hard_fullscreen_click(driver):
                return True
        except Exception:
            pass

        # 8. Native Browser-APIs als letzter Versuch
        try:
            driver.execute_script(
                """
                const v = document.querySelector('video');
                if (!v) return;
                
                // Verschiedene Vollbild-APIs versuchen
                const apis = [
                    () => v.requestFullscreen?.(),
                    () => v.webkitRequestFullscreen?.(),
                    () => v.mozRequestFullScreen?.(),
                    () => v.msRequestFullscreen?.(),
                    () => v.parentElement?.requestFullscreen?.(),
                    () => v.parentElement?.webkitRequestFullscreen?.(),
                    () => v.parentElement?.mozRequestFullScreen?.(),
                    () => v.parentElement?.msRequestFullscreen?.(),
                ];
                
                for (const api of apis) {
                    try {
                        const p = api();
                        if (p && p.catch) p.catch(() => {});
                        break;
                    } catch (e) {
                        continue;
                    }
                }
            """
            )
            time.sleep(0.15)
            if _is_fullscreen(driver):
                return True
        except Exception:
            pass

        # 8.5. Player-spezifische APIs für s.to
        try:
            driver.execute_script(
                """
                // JWPlayer API
                if (window.jwplayer && window.jwplayer().getContainer) {
                    try {
                        const player = window.jwplayer();
                        if (player && typeof player.setFullscreen === 'function') {
                            player.setFullscreen(true);
                            return;
                        }
                    } catch (e) {}
                }
                
                // Video.js API
                if (window.videojs) {
                    try {
                        const players = window.videojs.getPlayers();
                        for (const id in players) {
                            const player = players[id];
                            if (player && typeof player.requestFullscreen === 'function') {
                                player.requestFullscreen();
                                return;
                            }
                        }
                    } catch (e) {}
                }
                
                // Plyr API
                if (window.Plyr) {
                    try {
                        const players = document.querySelectorAll('[data-plyr]');
                        players.forEach(el => {
                            if (el.plyr && typeof el.plyr.fullscreen.enter === 'function') {
                                el.plyr.fullscreen.enter();
                            }
                        });
                    } catch (e) {}
                }
                
                // Shaka Player API
                if (window.shaka && window.shaka.Player) {
                    try {
                        const video = document.querySelector('video');
                        if (video && video.shakaPlayer) {
                            video.shakaPlayer.getControls().getFullscreenButton().click();
                        }
                    } catch (e) {}
                }
            """
            )
            time.sleep(0.2)
            if _is_fullscreen(driver):
                return True
        except Exception:
            pass

        # 9. Browser-Fenster-Vollbild als Fallback
        try:
            driver.fullscreen_window()
            time.sleep(0.15)
            if _is_fullscreen(driver):
                return True
        except Exception:
            try:
                ActionChains(driver).send_keys(Keys.F11).perform()
                time.sleep(0.2)
                if _is_fullscreen(driver):
                    return True
            except Exception:
                pass

        return _is_fullscreen(driver)
    except Exception as e:
        logging.debug(f"Fullscreen activation failed: {e}")
        return False


def apply_media_settings(driver, rate, vol):
    """Setzt playbackRate/Volume/Unmute sofort und hält sie kurzzeitig stabil (Events/Interval)."""
    try:
        driver.execute_script(
            """
            const rate = arguments[0], vol = arguments[1];
            const v = document.querySelector('video');
            if (!v) return;

            const setit = () => {
              try {
                // manche Player setzen 'muted' als Attribut → komplett entfernen
                v.removeAttribute('muted');
                v.defaultMuted = false;
                v.muted = (vol === 0);
                if (v.volume !== vol) v.volume = vol;
                if (v.playbackRate !== rate) v.playbackRate = rate;
              } catch(_) {}
            };

            setit();

            // für einige Sekunden „gegenhalten", falls src/MSE/Player neu setzt
            if (!v.__bwPin){
            v.__bwPin = true;
            const evs = ['loadedmetadata','canplay','playing','ratechange','volumechange','stalled'];
            evs.forEach(e => v.addEventListener(e, setit, {passive:true}));

            const cleanup = () => {
                try { evs.forEach(e => v.removeEventListener(e, setit)); } catch(_){}
                v.__bwPin = false;
            };

            let n = 0;
            const iv = setInterval(()=>{ setit(); if (++n > 20) { clearInterval(iv); cleanup(); } }, 150);
            setTimeout(()=>{ try{ clearInterval(iv); cleanup(); }catch(_){} }, 4000);
            }
        """,
            rate,
            vol,
        )
    except Exception:
        pass


def skip_intro(driver, seconds):
    WebDriverWait(driver, 15).until(
        lambda d: d.execute_script(
            "return document.querySelector('video')?.readyState > 0;"
        )
    )
    driver.execute_script(f"document.querySelector('video').currentTime = {seconds};")


def get_current_position(driver):
    return driver.execute_script(
        "return document.querySelector('video').currentTime || 0;"
    )


def detect_232011(driver) -> bool:
    try:
        return bool(
            driver.execute_script(
                """
            const el = document.querySelector('.jw-error-msg,.jw-error-text,[class*="jw-error"]');
            const txt = (el && (el.textContent||'').toLowerCase()) || '';
            return txt.includes('232011');
        """
            )
        )
    except Exception:
        return False


def _is_fullscreen(driver) -> bool:
    try:
        return bool(
            driver.execute_script(
                """
            try {
              const inFrame = !!(document.fullscreenElement
                                 || document.webkitFullscreenElement
                                 || document.mozFullScreenElement);
              let inTop = false;
              try {
                const td = window.top && window.top.document;
                inTop = !!(td && (td.fullscreenElement
                                  || td.webkitFullscreenElement
                                  || td.mozFullScreenElement));
              } catch(_) {}
              return inFrame || inTop;
            } catch(e) { return false; }
        """
            )
        )
    except Exception:
        return False


# === COOKIE FUNCTIONS --------------------------- ===
def delete_cookie(driver: webdriver.Firefox, name: str) -> bool:
    try:
        driver.delete_cookie(name)
        return True
    except Exception:
        return False


def get_cookie(driver, name):
    for c in driver.get_cookies():
        if c["name"] == name:
            return c["value"]
    return None


# === SIDEBAR FUNCTIONS --------------------------- ===
def read_settings(driver: webdriver.Firefox) -> Dict[str, Any]:
    try:
        data = driver.execute_script(
            """
            try {
                const raw = localStorage.getItem('bw_settings') || '{}';
                const obj = JSON.parse(raw);
                if (obj && typeof obj === 'object') return obj;
                return {};
            } catch(e) { return {}; }
            """
        )
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def build_items_html(db: Dict[str, Dict[str, Any]], settings: Optional[Dict[str, Any]] = None) -> str:
    """Erstellt HTML für die Sidebar mit Streaming-Anbieter-Tabs."""
    if settings is None:
        settings = {}
    auto_skip_intro = settings.get("autoSkipIntro", True)
    auto_skip_end = settings.get("autoSkipEndScreen", False)
    
    # Gruppiere Serien nach Anbietern
    provider_series = {}
    for series_name, data in db.items():
        provider = data.get("provider", "s.to")  # Standard ist s.to für Backward-Kompatibilität
        if provider not in provider_series:
            provider_series[provider] = []
        provider_series[provider].append((series_name, data))
    
    # Erstelle Tabs für jeden Anbieter
    tabs_html = []
    content_html = []
    
    for provider_id, series_list in provider_series.items():
        provider_info = STREAMING_PROVIDERS.get(provider_id, STREAMING_PROVIDERS["s.to"])
        
        # Sortiere Serien nach Timestamp
        sorted_series = sorted(series_list, key=lambda kv: float(kv[1].get("timestamp", 0)), reverse=True)
        
        # Tab-Header
        tabs_html.append(f'''
            <button class="bw-provider-tab" data-provider="{provider_id}" 
                    style="flex:1;padding:8px 12px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);
                           color:#94a3b8;border-radius:8px 8px 0 0;cursor:pointer;font-size:12px;font-weight:500;
                           transition:all .2s ease;border-bottom:none;">
                <div style="display:flex;align-items:center;gap:6px;justify-content:center;">
                    <div style="width:8px;height:8px;border-radius:50%;background:{provider_info['color']};"></div>
                    {provider_info['name']}
                </div>
            </button>
        ''')
        
        # Tab-Inhalt
        series_items = []
        for series_name, data in sorted_series:
            season = int(data.get("season", 1))
            episode = int(data.get("episode", 1))
            position = int(data.get("position", 0))
            ts_val = float(data.get("timestamp", 0))
            intro_val = int(data.get("intro_skip_start", INTRO_SKIP_SECONDS))
            intro_end_val = int(data.get("intro_skip_end", INTRO_SKIP_SECONDS + 60))
            end_skip_val = int(data.get("end_skip", 0))
            safe_name = _html.escape(series_name, quote=True)

            # Beautiful Skip Time Input Fields
            input_fields_html = ""
            
            if auto_skip_intro or auto_skip_end:
                # Build intro section HTML
                intro_section_html = ""
                if auto_skip_intro:
                    intro_section_html = f'''
                            <div class="bw-intro-section" style="display:flex;flex-direction:column;gap:6px;">
                                <div style="display:flex;align-items:center;gap:6px;">
                                    <div style="width:12px;height:12px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);border-radius:3px;display:flex;align-items:center;justify-content:center;font-size:8px;color:white;">&gt;</div>
                                    <span style="font-size:11px;color:#cbd5e1;font-weight:500;">Intro Skip</span>
                                </div>
                                <div style="display:flex;gap:6px;align-items:center;">
                                    <div style="display:flex;flex-direction:column;gap:2px;flex:1;">
                                        <label style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;">Start (s)</label>
                                        <input class="bw-intro-start" data-series="{safe_name}" type="number" min="0" value="{intro_val}" 
                                               style="width:100%;padding:8px 10px;border-radius:8px;border:1px solid rgba(59,130,246,.3);background:rgba(59,130,246,.1);color:#e2e8f0;font-size:12px;font-weight:500;text-align:center;transition:all .2s ease;outline:none;" 
                                               placeholder="0" title="Intro start time (seconds)"/>
                                    </div>
                                    <div style="display:flex;flex-direction:column;gap:2px;flex:1;">
                                        <label style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;">End (s)</label>
                                        <input class="bw-intro-end" data-series="{safe_name}" type="number" min="0" value="{intro_end_val}" 
                                               style="width:100%;padding:8px 10px;border-radius:8px;border:1px solid rgba(139,92,246,.3);background:rgba(139,92,246,.1);color:#e2e8f0;font-size:12px;font-weight:500;text-align:center;transition:all .2s ease;outline:none;" 
                                               placeholder="0" title="Intro end time (seconds)"/>
                                    </div>
                                </div>
                            </div>
                            '''
                
                # Build end section HTML
                end_section_html = ""
                if auto_skip_end:
                    end_section_html = f'''
                            <div class="bw-end-section" style="display:flex;flex-direction:column;gap:6px;">
                                <div style="display:flex;align-items:center;gap:6px;">
                                    <div style="width:12px;height:12px;background:linear-gradient(135deg,#ef4444,#dc2626);border-radius:3px;display:flex;align-items:center;justify-content:center;font-size:8px;color:white;">[]</div>
                                    <span style="font-size:11px;color:#cbd5e1;font-weight:500;">End Skip</span>
                                </div>
                                <div style="display:flex;gap:6px;align-items:center;">
                                    <div style="display:flex;flex-direction:column;gap:2px;flex:1;">
                                        <label style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;">Skip at (s)</label>
                                        <input class="bw-end" data-series="{safe_name}" type="number" min="0" value="{end_skip_val}" 
                                               style="width:100%;padding:8px 10px;border-radius:8px;border:1px solid rgba(239,68,68,.3);background:rgba(239,68,68,.1);color:#e2e8f0;font-size:12px;font-weight:500;text-align:center;transition:all .2s ease;outline:none;" 
                                               placeholder="0" title="Skip to end at this time (seconds)"/>
                                    </div>
                                    <div style="width:60px;height:36px;display:flex;align-items:center;justify-content:center;background:rgba(239,68,68,.05);border:1px solid rgba(239,68,68,.2);border-radius:10px;margin-top:auto">
                                        <span style="font-size:10px;color:#fca5a5;">End</span>
                                    </div>
                                </div>
                            </div>
                            '''
                
                # Combine into final input fields HTML
                input_fields_html = f'''
                    <div class="bw-skip-controls" style="margin-top:12px;padding:12px;background:linear-gradient(135deg,rgba(59,130,246,.08),rgba(139,92,246,.08));border:1px solid rgba(59,130,246,.2);border-radius:10px;position:relative;">
                        <div style="position:absolute;top:-8px;left:12px;background:linear-gradient(135deg,rgba(15,23,42,.95),rgba(30,41,59,.95));padding:2px 8px;border-radius:6px;font-size:10px;color:#93c5fd;font-weight:600;border:1px solid rgba(59,130,246,.3);">Skip Times</div>
                        
                        <div style="display:flex;flex-direction:column;gap:8px;">
                            {intro_section_html}
                            {end_section_html}
                        </div>
                    </div>
                '''

            series_items.append(f"""
                <div class="bw-series-item" data-series="{safe_name}" data-season="{season}" data-episode="{episode}" data-ts="{ts_val}" data-provider="{provider_id}"
                     style="margin:8px;padding:16px;background:linear-gradient(135deg,rgba(255,255,255,.05),rgba(255,255,255,.02));
                            border:1px solid rgba(255,255,255,.1);border-radius:12px;cursor:pointer;position:relative;">
                    
                    <!-- Header with title and delete button -->
                    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
                        <div style="flex:1;">
                            <div style="font-weight:600;font-size:14px;color:#f8fafc;margin-bottom:4px;">{safe_name}</div>
                            <div style="font-size:12px;color:#94a3b8;display:flex;align-items:center;gap:8px;">
                                <span style="background:{provider_info['color']}20;padding:2px 6px;border-radius:4px;border:1px solid {provider_info['color']}40;">S{season}E{episode}</span>
                                <span style="opacity:.7;">{position}s</span>
                            </div>
                        </div>
                        <div class="bw-delete" data-series="{safe_name}" style="color:#ef4444;cursor:pointer;padding:8px;border-radius:8px;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.2);font-size:12px;margin-left:8px;transition:all .2s ease;hover:background:rgba(239,68,68,.2);" title="Remove series">X</div>
                    </div>
                    
                    <!-- Skip Time Controls -->
                    {input_fields_html}
                </div>
            """)
        
        content_html.append(f'''
            <div class="bw-provider-content" data-provider="{provider_id}" style="display:flex;flex-direction:column;gap:6px;">
                {"".join(series_items)}
            </div>
        ''')
    
    # Kombiniere alles
    tabs_container = f'''
        <div class="bw-provider-tabs" style="display:flex;gap:2px;margin-bottom:12px;">
            {"".join(tabs_html)}
        </div>
    '''
    
    content_container = f'''
        <div class="bw-provider-contents">
            {"".join(content_html)}
        </div>
    '''
    
    return tabs_container + content_container


def inject_sidebar(driver: webdriver.Firefox, db: Dict[str, Dict[str, Any]]) -> bool:
    try:
        driver.switch_to.default_content()
        settings = get_settings(driver)
        html_concat = build_items_html(db, settings)
        driver.execute_script(
            """
        (function(html){
          try {
            let d = document.getElementById('bingeSidebar');
            if (!d) {
              d = document.createElement('div');
              d.id = 'bingeSidebar';
              Object.assign(d.style, {
                position:'fixed', left:0, top:0, width:'340px', height:'100vh',
                background:'linear-gradient(180deg, rgba(15,23,42,.95), rgba(30,41,59,.95))',
                color:'#f8fafc', zIndex:2147483647,
                borderRight:'1px solid rgba(255,255,255,.1)', backdropFilter:'blur(18px)'
              });
              d.innerHTML = `
              <div class="bw-head" style="position:sticky;top:0;z-index:2;padding:12px 16px;border-bottom:1px solid rgba(255,255,255,.08);
                                          background:linear-gradient(180deg,rgba(15,23,42,.96),rgba(30,41,59,.92));backdrop-filter:blur(12px)">
                  <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">
                  <div style="display:flex;align-items:center;gap:8px;">
                      <div style="width:8px;height:8px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);border-radius:999px;"></div>
                      <span style="font-weight:700;font-size:18px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">BingeWatcher</span>
                  </div>
                  <div class="bw-actions" style="display:flex;gap:8px;">
                      <button id="bwSettings" class="bw-btn" title="Einstellungen">⚙</button>
                      <button id="bwSkip" class="bw-btn" title="Episode skippen">⏭</button>
                      <button id="bwQuit" class="bw-btn danger" title="Beenden">⏻</button>
                  </div>
                  </div>

                  <!-- Handle, hängt halb raus -->
                  <button id="bwCollapse" class="bw-handle" title="Einklappen">
                  <span class="chev">❮</span>
                  </button>

                  <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;">
                  <input id="bwSearch" placeholder="Search..." style="flex:1;padding:8px;border-radius:10px;border:1px solid rgba(255,255,255,.12);background:rgba(2,6,23,.35);color:#e2e8f0;"/>
                  <select id="bwSort" style="padding:8px;border-radius:10px;border:1px solid rgba(255,255,255,.12);background:rgba(2,6,23,.35);color:#e2e8f0;min-width:120px;max-width:120px;flex:0 0 120px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                      <option value="time">Last watched</option>
                      <option value="name">Name</option>
                  </select>
                  </div>
                  
                  <!-- Website Selection Switch -->
                  <div style="margin-top:8px;display:flex;align-items:center;gap:8px;padding:8px;border-radius:10px;border:1px solid rgba(255,255,255,.12);background:rgba(2,6,23,.35);">
                      <span style="font-size:12px;color:#94a3b8;">Website:</span>
                      <div style="display:flex;gap:4px;flex:1;">
                          <button id="bwProviderS" class="bw-provider-switch" data-provider="s.to" style="flex:1;padding:6px 8px;border-radius:6px;border:1px solid rgba(59,130,246,.3);background:rgba(59,130,246,.15);color:#93c5fd;font-size:11px;cursor:pointer;transition:all .2s ease;">SerienJunkie</button>
                          <button id="bwProviderA" class="bw-provider-switch" data-provider="aniworld.to" style="flex:1;padding:6px 8px;border-radius:6px;border:1px solid rgba(139,92,246,.3);background:rgba(139,92,246,.15);color:#c4b5fd;font-size:11px;cursor:pointer;transition:all .2s ease;">AniWorld</button>
                      </div>
                  </div>
              </div>

              <div id="bwBody" class="bw-body" style="padding:12px;">
                  <div id="bwSeriesList" style="display:flex;flex-direction:column;gap:6px;"></div>
              </div>
              `;
              
              (document.body||document.documentElement).appendChild(d);

              document.addEventListener('fullscreenchange', ()=>{
                const sb = document.getElementById('bingeSidebar');
                if (!sb) return;
                if (document.fullscreenElement) {
                    sb.style.display = 'none';
                } else {
                    sb.style.display = '';
                }
              }, {passive:true});

              const style = document.createElement('style');
              style.textContent = `
              #bingeSidebar{ width:340px; transition: transform .28s cubic-bezier(.22,.61,.36,1); box-shadow:0 10px 30px rgba(0,0,0,.35); overflow:visible; }
  
              /* Collapsed: 56px sichtbar lassen */
              #bingeSidebar[data-collapsed="1"]{ transform: translateX(calc(-100% + 56px)); }
  
              /* PEEK - bei Hover ODER wenn JS data-peek="1" setzt */
              #bingeSidebar[data-collapsed="1"]:is(:hover,[data-peek="1"]){
                transform: translateX(calc(-100% + 92px)); /* 56 + ~36 (Handlebreite) */
              }
  
              /* Handle stets oben */
              #bingeSidebar .bw-handle{ z-index:5; }
  
              /* Action-Buttons */
              #bingeSidebar .bw-btn{
                width:36px;height:36px;border-radius:12px;border:1px solid rgba(148,163,184,.22);
                background:rgba(148,163,184,.10); color:#cbd5e1; cursor:pointer;
                display:flex;align-items:center;justify-content:center;
                box-shadow:inset 0 -1px rgba(255,255,255,.06);
                transition: border-color .2s, background .2s, transform .15s;
              }
              #bingeSidebar .bw-btn:hover{ border-color:rgba(148,163,184,.38); background:rgba(148,163,184,.16); transform:translateY(-1px); }
              #bingeSidebar .bw-btn:active{ transform:translateY(0); }
              #bingeSidebar .bw-btn.danger{ border-color:rgba(239,68,68,.25); background:rgba(239,68,68,.10); color:#fecaca; }
              #bingeSidebar .bw-btn.danger:hover{ border-color:rgba(239,68,68,.45); background:rgba(239,68,68,.16); }
  
              /* Handle */
              #bingeSidebar .bw-handle{
                position:absolute; top:85px; right:-18px; width:32px; height:32px; border-radius:999px;
                border:1px solid rgba(59,130,246,.6); background:linear-gradient(135deg,rgba(30,41,59,.95),rgba(2,6,23,.95));
                backdrop-filter:blur(10px); display:flex; align-items:center; justify-content:center; cursor:pointer;
                box-shadow:0 6px 20px rgba(0,0,0,.4), 0 0 0 2px rgba(59,130,246,.18);
                transition: transform .2s ease, background .2s ease, border-color .2s ease, box-shadow .2s ease;
              }
              #bingeSidebar .bw-handle::after{ content:""; position:absolute; inset:-8px; } /* größere Klickfläche */
              #bingeSidebar .bw-handle:hover{ transform:translateY(-1px); background:linear-gradient(135deg,rgba(30,41,59,1),rgba(15,23,42,1)); border-color:rgba(59,130,246,.85); box-shadow:0 8px 22px rgba(0,0,0,.45), 0 0 0 2px rgba(59,130,246,.3); }
              #bingeSidebar .chev{ font-size:16px; line-height:1; color:#e2e8f0; text-shadow:0 0 8px rgba(59,130,246,.45); transition: transform .2s ease, color .2s ease; }
              #bingeSidebar[data-collapsed="1"] .bw-handle .chev{ transform: rotate(180deg); }
  
              /* Body */
              #bingeSidebar .bw-body{
                transition: opacity .2s ease;
                padding:12px;
                overflow-y:auto;
                height:calc(100vh - 116px); /* Headerhöhe anpassen falls nötig */
              }
  
              /* Elemente nur ausblenden, wenn wirklich collapsed UND nicht gepeekt/gehovered */
              #bingeSidebar[data-collapsed="1"]:not(:hover):not([data-peek="1"]) .bw-actions,
              #bingeSidebar[data-collapsed="1"]:not(:hover):not([data-peek="1"]) #bwSearch,
              #bingeSidebar[data-collapsed="1"]:not(:hover):not([data-peek="1"]) #bwSort,
              #bingeSidebar[data-collapsed="1"]:not(:hover):not([data-peek="1"]) .bw-body{
                opacity:0; pointer-events:none;
              }
  
              /* Beim Peek/Hover wieder einblenden (interaktiv) */
              #bingeSidebar[data-collapsed="1"]:is(:hover,[data-peek="1"]) .bw-actions,
              #bingeSidebar[data-collapsed="1"]:is(:hover,[data-peek="1"]) #bwSearch,
              #bingeSidebar[data-collapsed="1"]:is(:hover,[data-peek="1"]) #bwSort,
              #bingeSidebar[data-collapsed="1"]:is(:hover,[data-peek="1"]) .bw-body{
                opacity:1; pointer-events:auto;
                transition: opacity .16s ease .05s;
              }
              
              /* Provider Tabs */
              #bingeSidebar .bw-provider-tabs {
                border-bottom: 1px solid rgba(255,255,255,.1);
                margin-bottom: 12px;
              }
              
              #bingeSidebar .bw-provider-tab {
                transition: all .2s ease;
              }
              
              #bingeSidebar .bw-provider-tab[data-active="1"] {
                background: rgba(255,255,255,.08) !important;
                color: #f8fafc !important;
                border-bottom: 2px solid rgba(148,163,184,.55) !important;
                box-shadow: inset 0 -2px 0 rgba(148,163,184,.2);
              }
              
              #bingeSidebar .bw-provider-tab:hover {
                background: rgba(255,255,255,.08) !important;
                color: #f8fafc !important;
              }
              
              #bingeSidebar .bw-provider-content {
                display: none;
              }
              
              #bingeSidebar .bw-provider-content:first-child {
                display: flex;
              }
              
              /* Provider Switch Buttons */
              #bingeSidebar .bw-provider-switch {
                border: 1px solid rgba(148,163,184,.2) !important;
                background: rgba(148,163,184,.08) !important;
                color: #94a3b8 !important;
              }
              
              #bingeSidebar .bw-provider-switch.active {
                border-color: rgba(59,130,246,.7) !important;
                background: linear-gradient(135deg, rgba(59,130,246,.25), rgba(139,92,246,.25)) !important;
                color: #f8fafc !important;
                box-shadow: 0 0 0 1px rgba(59,130,246,.25), 0 6px 16px rgba(15,23,42,.35);
                position: relative;
              }
              #bingeSidebar .bw-provider-switch.active::after {
                content: '';
                position: absolute;
                inset: -2px;
                border-radius: 8px;
                border: 1px solid rgba(59,130,246,.35);
                pointer-events: none;
              }
              
              #bingeSidebar .bw-provider-switch:hover {
                border-color: rgba(255,255,255,.25) !important;
                background: rgba(255,255,255,.12) !important;
                transform: translateY(-1px);
              }
              
              /* Beautiful Skip Time Input Fields */
              #bingeSidebar .bw-skip-controls {
                transition: all .3s ease;
              }
              
              #bingeSidebar .bw-skip-controls:hover {
                transform: translateY(-1px);
                box-shadow: 0 4px 12px rgba(59,130,246,.15);
              }
              
              #bingeSidebar .bw-intro-start,
              #bingeSidebar .bw-intro-end,
              #bingeSidebar .bw-end {
                transition: all .2s ease !important;
              }
              
              #bingeSidebar .bw-intro-start:focus,
              #bingeSidebar .bw-intro-end:focus,
              #bingeSidebar .bw-end:focus {
                transform: scale(1.02);
                box-shadow: 0 0 0 2px rgba(59,130,246,.3);
                border-color: rgba(59,130,246,.6) !important;
              }
              
              #bingeSidebar .bw-intro-start:hover,
              #bingeSidebar .bw-intro-end:hover,
              #bingeSidebar .bw-end:hover {
                border-color: rgba(59,130,246,.5) !important;
                background: rgba(59,130,246,.15) !important;
              }
              
              #bingeSidebar .bw-intro-end:focus {
                box-shadow: 0 0 0 2px rgba(139,92,246,.3);
                border-color: rgba(139,92,246,.6) !important;
              }
              
              #bingeSidebar .bw-intro-end:hover {
                border-color: rgba(139,92,246,.5) !important;
                background: rgba(139,92,246,.15) !important;
              }
              
              #bingeSidebar .bw-end:focus {
                box-shadow: 0 0 0 2px rgba(239,68,68,.3);
                border-color: rgba(239,68,68,.6) !important;
              }
              
              #bingeSidebar .bw-end:hover {
                border-color: rgba(239,68,68,.5) !important;
                background: rgba(239,68,68,.15) !important;
              }
              
              /* Input field animations */
              #bingeSidebar .bw-intro-section,
              #bingeSidebar .bw-end-section {
                transition: all .2s ease;
              }
              
              #bingeSidebar .bw-intro-section:hover,
              #bingeSidebar .bw-end-section:hover {
                transform: translateX(2px);
              }
              
              /* Delete button hover effect */
              #bingeSidebar .bw-delete:hover {
                background: rgba(239,68,68,.2) !important;
                transform: scale(1.1);
                box-shadow: 0 2px 8px rgba(239,68,68,.3);
              }
              `;
              d.appendChild(style);
  
              const tgl = document.getElementById('bwCollapse');
              const setHandleTitle = () => {
                const collapsed = d.getAttribute('data-collapsed') === '1';
                tgl.title = collapsed ? 'Unfold' : 'Collapse';
              };
              if (localStorage.getItem('bw_sidebar_collapsed') === '1') d.setAttribute('data-collapsed','1');
              setHandleTitle();
              
              // Provider Detection from URL
              function detectProviderFromUrl(url) {
                if (url.includes('s.to')) {
                  return 's.to';
                } else if (url.includes('aniworld.to')) {
                  return 'aniworld.to';
                }
                return null;
              }
              
              // Update UI based on current URL
              function updateUIFromCurrentUrl() {
                const currentUrl = window.location.href;
                const detectedProvider = detectProviderFromUrl(currentUrl);
                
                // Determine which provider to highlight
                let providerToHighlight = detectedProvider;
                
                // If no provider detected from URL, use the last active provider from localStorage
                if (!providerToHighlight) {
                  providerToHighlight = localStorage.getItem('bw_active_provider') || 's.to';
                }
                
                // Update website switch highlighting
                updateProviderSwitch(providerToHighlight);
                
                // Switch to correct provider tab (only if elements exist)
                if (typeof switchProviderTab === 'function') {
                  switchProviderTab(providerToHighlight);
                }
              }
              
              // Website Selection Switch Management
              function updateProviderSwitch(providerId) {
                // Alle Switch-Buttons zurücksetzen
                const switchButtons = document.querySelectorAll('.bw-provider-switch');
                if (switchButtons.length > 0) {
                  switchButtons.forEach(btn => {
                    btn.classList.remove('active');
                  });
                  
                  // Aktiven Button markieren
                  const activeBtn = document.querySelector(`.bw-provider-switch[data-provider="${providerId}"]`);
                  if (activeBtn) {
                    activeBtn.classList.add('active');
                  }
                }
                
                // Provider in localStorage speichern
                localStorage.setItem('bw_active_provider', providerId);
                localStorage.setItem('bw_website_switch', providerId);
              }
              
              // Initialisiere UI basierend auf aktueller URL
              setTimeout(() => {
                updateUIFromCurrentUrl();
              }, 300);
              
              // Listen for URL changes (navigation, back/forward buttons)
              let lastUrl = window.location.href;
              const urlObserver = new MutationObserver(() => {
                if (window.location.href !== lastUrl) {
                  lastUrl = window.location.href;
                  setTimeout(() => updateUIFromCurrentUrl(), 100);
                }
              });
              
              // Observe changes to the document
              urlObserver.observe(document, { subtree: true, childList: true });
              
              // Also listen for popstate events (back/forward buttons)
              window.addEventListener('popstate', () => {
                setTimeout(() => updateUIFromCurrentUrl(), 100);
              });
              
              // Website Switch Click Handler
              d.addEventListener('click', (e) => {
                const switchBtn = e.target.closest('.bw-provider-switch');
                if (switchBtn) {
                  const providerId = switchBtn.getAttribute('data-provider');
                  if (providerId) {
                    updateProviderSwitch(providerId);
                    
                    // Navigate to the selected provider's website
                    const providerUrls = {
                      's.to': 'https://s.to/',
                      'aniworld.to': 'https://aniworld.to/'
                    };
                    
                    const targetUrl = providerUrls[providerId];
                    if (targetUrl) {
                      window.location.href = targetUrl;
                    }
                  }
                }
              });
  
              /* Peek via JS, wenn nur der Griff gehovert wird */
              tgl.addEventListener('mouseenter', ()=> {
                if (d.getAttribute('data-collapsed') === '1') d.setAttribute('data-peek','1');
              });
                tgl.addEventListener('mouseleave', ()=> {
                d.removeAttribute('data-peek');
              });
  
              /* Toggle */
              tgl.addEventListener('click', (e)=>{
                e.preventDefault(); e.stopPropagation();
                const collapsed = d.getAttribute('data-collapsed') === '1';
                d.setAttribute('data-collapsed', collapsed ? '0' : '1');
                localStorage.setItem('bw_sidebar_collapsed', collapsed ? '0' : '1');
                d.removeAttribute('data-peek');
                setHandleTitle();
              });
                
              /* Buttons */
              const btnSkip = document.getElementById('bwSkip');
              const btnQuit = document.getElementById('bwQuit');
              if (btnSkip) btnSkip.addEventListener('click', (e)=>{
                e.preventDefault(); e.stopPropagation();
                try { localStorage.setItem('bw_skip_now','1'); } catch(_){}
                document.cookie = 'bw_skip=1; path=/';
              });
              if (btnQuit) btnQuit.addEventListener('click', (e)=>{
                e.preventDefault(); e.stopPropagation();
                try { localStorage.setItem('bw_quit','1'); } catch(_){}
                document.cookie = 'bw_quit=1; path=/';
              });

              // Provider Tab Management
              function switchProviderTab(providerId) {
                // Alle Tabs deaktivieren
                document.querySelectorAll('.bw-provider-tab').forEach(tab => {
                  tab.style.background = 'rgba(255,255,255,.05)';
                  tab.style.color = '#94a3b8';
                  tab.style.borderBottom = 'none';
                  tab.removeAttribute('data-active');
                });
                
                // Alle Contents ausblenden
                document.querySelectorAll('.bw-provider-content').forEach(content => {
                  content.style.display = 'none';
                });
                
                // Gewählten Tab aktivieren
                const activeTab = document.querySelector(`[data-provider="${providerId}"]`);
                if (activeTab) {
                  activeTab.style.background = 'rgba(255,255,255,.1)';
                  activeTab.style.color = '#f8fafc';
                  activeTab.style.borderBottom = '2px solid rgba(255,255,255,.2)';
                  activeTab.setAttribute('data-active', '1');
                }
                
                // Gewählten Content anzeigen
                const activeContent = document.querySelector(`.bw-provider-content[data-provider="${providerId}"]`);
                if (activeContent) {
                  activeContent.style.display = 'flex';
                }
              }
              
              // Provider Tab Click Handler
              d.addEventListener('click', (e) => {
                const tab = e.target.closest('.bw-provider-tab');
                if (tab) {
                  const providerId = tab.getAttribute('data-provider');
                  if (providerId) {
                    switchProviderTab(providerId);
                    localStorage.setItem('bw_active_provider', providerId);
                  }
                }
              });
              
              function onSort(){
                const mode = document.getElementById('bwSort').value;
                const activeProvider = localStorage.getItem('bw_active_provider') || 's.to';
                const list = document.querySelector(`.bw-provider-content[data-provider="${activeProvider}"]`);
                if (!list) return;
                
                const items = Array.from(list.children);
                items.sort((a,b)=>{
                  if (mode==='name') return a.dataset.series.localeCompare(b.dataset.series);
                  const tsA = parseFloat(a.getAttribute('data-ts')||'0');
                  const tsB = parseFloat(b.getAttribute('data-ts')||'0');
                  if (tsA !== tsB) return tsB - tsA;
                  return a.dataset.series.localeCompare(b.dataset.series);
                });
                list.replaceChildren(...items);
              }
              function onFilter(){
                const q = (document.getElementById('bwSearch').value||'').toLowerCase();
                const activeProvider = localStorage.getItem('bw_active_provider') || 's.to';
                const list = document.querySelector(`.bw-provider-content[data-provider="${activeProvider}"]`);
                if (!list) return;
                
                Array.from(list.children).forEach(el=>{
                  const show = el.dataset.series.toLowerCase().includes(q);
                  el.style.display = show ? '' : 'none';
                });
              }

              d.addEventListener('input', (e)=>{ if (e.target && e.target.id==='bwSearch') onFilter(); });
              d.addEventListener('change', (e)=>{ if (e.target && e.target.id==='bwSort') onSort(); });

              d.addEventListener('click', (e)=>{
                const c = sel => e.target.closest && e.target.closest(sel);

                if (c('#bwSettings')) {
                  const existing = document.getElementById('bwSettingsPanel');
                  if (existing) { existing.remove(); return; }
                  const p = document.createElement('div');
                  Object.assign(p, { id:'bwSettingsPanel' });
                  Object.assign(p.style, { position:'fixed', right:'16px', top:'64px', width:'340px', background:'rgba(2,6,23,.94)', border:'1px solid rgba(255,255,255,.12)', borderRadius:'12px', color:'#e2e8f0', padding:'16px', zIndex:2147483647, cursor:'move', userSelect:'none' });
                  p.innerHTML = `
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;cursor:move;" id="bwSettingsDragHandle">
                      <div style="font-weight:600">Settings</div>
                      <button id="bwCloseSettings" style="background:transparent;border:0;color:#94a3b8;cursor:pointer;font-size:18px;">X</button>
                    </div>
                    <label style="display:flex;align-items:center;gap:8px;margin:8px 0;">
                      <input type="checkbox" id="bwOptAutoFullscreen"/><span>Auto-Fullscreen</span>
                    </label>
                                         <label style="display:flex;align-items:center;gap:8px;margin:8px 0;">
                       <input type="checkbox" id="bwOptAutoSkipIntro"/><span>Skip intro</span>
                     </label>
                     <label style="display:flex;align-items:center;gap:8px;margin:8px 0;">
                       <input type="checkbox" id="bwOptAutoSkipEndScreen"/><span>Skip end screen</span>
                     </label>
                     <label style="display:flex;align-items:center;gap:8px;margin:8px 0;">
                       <input type="checkbox" id="bwOptAutoNext" checked/><span>Autoplay next episode</span>
                     </label>
                    <label style="display:flex;align-items:center;gap:8px;margin:8px 0;">
                      <span>Playback Speed</span>
                      <select id="bwOptPlaybackRate">
                        <option value="0.75">0.75x</option>
                        <option value="1" selected>1x</option>
                        <option value="1.25">1.25x</option>
                        <option value="1.5">1.5x</option>
                        <option value="1.75">1.75x</option>
                        <option value="2">2x</option>
                      </select>
                    </label>
                    <label style="display:flex;align-items:center;gap:8px;margin:8px 0;width:100%;box-sizing:border-box;">
                        <span>Volume</span>
                        <input type="range" id="bwOptVolume" min="0" max="1" step="0.05" style="flex:1;min-width:0;"/>
                        <span id="bwVolumeVal" style="flex:0 0 44px;min-width:44px;max-width:44px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right;display:inline-block;"></span>
                    </label>
                    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px;">
                      <button id="bwSaveSettings" style="padding:6px 10px;border-radius:8px;border:1px solid rgba(59,130,246,.35);background:rgba(59,130,246,.12);color:#93c5fd;cursor:pointer;">Save</button>
                    </div>
                  `;
                  document.body.appendChild(p);
                  
                  // Drag and drop functionality
                  let isDragging = false;
                  let dragStartX = 0;
                  let dragStartY = 0;
                  let initialLeft = 0;
                  let initialTop = 0;
                  
                  const dragHandle = document.getElementById('bwSettingsDragHandle');
                  const panel = document.getElementById('bwSettingsPanel');
                  
                  // Load saved position or use default
                  let savedPosition = null;
                  try {
                    const saved = localStorage.getItem('bw_settings_panel_position');
                    if (saved) {
                      savedPosition = JSON.parse(saved);
                    }
                  } catch(_) {}
                  
                                        // Calculate initial position
                      if (savedPosition && savedPosition.left !== undefined && savedPosition.top !== undefined) {
                    // Use saved position, but ensure it's within viewport bounds
                    initialLeft = Math.max(0, Math.min(window.innerWidth - 340, savedPosition.left));
                    initialTop = Math.max(0, Math.min(window.innerHeight - 400, savedPosition.top));
                  } else {
                    // Default position (top-right)
                    initialLeft = window.innerWidth - 356; // 340px width + 16px margin
                    initialTop = 64;
                  }
                  
                  // Set initial position
                  panel.style.right = 'auto';
                  panel.style.left = initialLeft + 'px';
                  panel.style.top = initialTop + 'px';
                  
                  dragHandle.addEventListener('mousedown', (e) => {
                    isDragging = true;
                    dragStartX = e.clientX;
                    dragStartY = e.clientY;
                    initialLeft = parseInt(panel.style.left) || 0;
                    initialTop = parseInt(panel.style.top) || 0;
                    e.preventDefault();
                  });
                  
                  document.addEventListener('mousemove', (e) => {
                    if (!isDragging) return;
                    
                    const deltaX = e.clientX - dragStartX;
                    const deltaY = e.clientY - dragStartY;
                    
                    const newLeft = Math.max(0, Math.min(window.innerWidth - panel.offsetWidth, initialLeft + deltaX));
                    const newTop = Math.max(0, Math.min(window.innerHeight - panel.offsetHeight, initialTop + deltaY));
                    
                    panel.style.left = newLeft + 'px';
                    panel.style.top = newTop + 'px';
                  });
                  
                  document.addEventListener('mouseup', () => {
                    if (isDragging) {
                      // Save the current position when dragging stops
                      try {
                        const currentLeft = parseInt(panel.style.left) || 0;
                        const currentTop = parseInt(panel.style.top) || 0;
                        const position = { left: currentLeft, top: currentTop };
                        localStorage.setItem('bw_settings_panel_position', JSON.stringify(position));
                      } catch(_) {}
                    }
                    isDragging = false;
                  });
                  
                  // Prevent dragging when interacting with form elements
                  const formElements = panel.querySelectorAll('input, select, button, label');
                  formElements.forEach(element => {
                    element.addEventListener('mousedown', (e) => {
                      if (element.id === 'bwCloseSettings') {
                        return;
                      }
                      e.stopPropagation();
                    });
                    element.addEventListener('click', (e) => {
                      if (element.id === 'bwCloseSettings') {
                        return;
                      }
                      e.stopPropagation();
                    });
                  });
                  
                  const closeButton = document.getElementById('bwCloseSettings');
                  if (closeButton) {
                    closeButton.addEventListener('click', (e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      p.remove();
                    });
                  }
                  
                  try {
                    const s = JSON.parse(localStorage.getItem('bw_settings')||'{}');
                    const x = id => document.getElementById(id);
                                         if (x('bwOptAutoFullscreen')) x('bwOptAutoFullscreen').checked = (s.autoFullscreen !== false);
                     if (x('bwOptAutoSkipIntro')) x('bwOptAutoSkipIntro').checked = (s.autoSkipIntro !== false);
                     if (x('bwOptAutoSkipEndScreen')) x('bwOptAutoSkipEndScreen').checked = (s.autoSkipEndScreen !== false);
                     if (x('bwOptAutoNext')) x('bwOptAutoNext').checked = (s.autoNext !== false);
                    if (x('bwOptPlaybackRate')) x('bwOptPlaybackRate').value = String(s.playbackRate ?? 1);
                    if (x('bwOptVolume')) x('bwOptVolume').value = String(Math.max(0, Math.min(1, s.volume ?? 1)));
                    if (x('bwVolumeVal')) x('bwVolumeVal').textContent = Math.round(parseFloat(x('bwOptVolume').value||'1')*100) + '%';
                    document.getElementById('bwOptVolume')?.addEventListener('input', (e)=>{
                        const v = parseFloat(e.target.value||'1');
                        const vv = document.getElementById('bwVolumeVal');
                        if (vv) vv.textContent = Math.round(v*100)+'%';
                    });
                  } catch(_){}
                  p.addEventListener('click', (ev)=>{
                    if (ev.target && ev.target.id === 'bwCloseSettings') {
                      p.remove();
                      return;
                    }
                    // Prevent dragging when clicking on interactive elements
                    if (ev.target.tagName === 'INPUT' || ev.target.tagName === 'SELECT' || ev.target.tagName === 'BUTTON' || ev.target.closest('label')) {
                      return;
                    }
                                         if (ev.target && ev.target.id==='bwSaveSettings') {
                                              const next = {
                          autoFullscreen: !!document.getElementById('bwOptAutoFullscreen')?.checked,
                          autoSkipIntro: !!document.getElementById('bwOptAutoSkipIntro')?.checked,
                          autoSkipEndScreen: !!document.getElementById('bwOptAutoSkipEndScreen')?.checked,
                          autoNext: !!document.getElementById('bwOptAutoNext')?.checked,
                          playbackRate: parseFloat(document.getElementById('bwOptPlaybackRate')?.value || '1'),
                          volume: Math.max(0, Math.min(1, parseFloat(document.getElementById('bwOptVolume')?.value || '1')))
                        };
                       localStorage.setItem('bw_settings', JSON.stringify(next));
                       localStorage.setItem('bw_settings_update', JSON.stringify(next));
                       
                       // UI sofort aktualisieren, um neue Eingabefelder anzuzeigen/verstecken
                       try {
                         // Trigger UI update by setting a flag
                         localStorage.setItem('bw_ui_update_needed', '1');
                       } catch(_) {}
                       
                       p.remove();
                     }
                  });
                  return;
                }

                const del = c('.bw-delete');
                if (del) {
                  const s = del.getAttribute('data-series');
                  if (s) localStorage.setItem('bw_seriesToDelete', s);
                  return;
                }

                                 const item = c('.bw-series-item');
                 if (item) {
                     // Check if click originated from input field or delete button - don't trigger navigation
                     const clickedInput = e.target.closest && (e.target.closest('input.bw-intro-start') || e.target.closest('input.bw-intro-end'));
                     const clickedEndInput = e.target.closest && e.target.closest('input.bw-end');
                     const clickedDelete = e.target.closest && e.target.closest('.bw-delete');
                     if (clickedInput || clickedEndInput || clickedDelete) return;
                    
                    if (localStorage.getItem('bw_nav_inflight') === '1') return; // throttle
                    localStorage.setItem('bw_nav_inflight','1');

                    const body = document.getElementById('bwBody');
                    if (body) { body.style.pointerEvents='none'; body.style.opacity='.6'; }

                    const s = item.getAttribute('data-series') || '';
                    const provider = item.getAttribute('data-provider') || 's.to';
                    try { 
                        localStorage.setItem('bw_series', s); 
                        localStorage.setItem('bw_series_provider', provider);
                    } catch(_) {}
                    document.cookie = 'bw_series=' + encodeURIComponent(s) + '; path=/';
                    document.cookie = 'bw_series_provider=' + encodeURIComponent(provider) + '; path=/';

                    setTimeout(()=>{ try{
                    if (localStorage.getItem('bw_nav_inflight') === '1') {
                        localStorage.removeItem('bw_nav_inflight');
                        if (body) { body.style.pointerEvents=''; body.style.opacity=''; }
                    }
                    }catch(_){ } }, 4000);
                    return;
                }
              });

                             // Debounce für Intro-Input und End-Input
               if (!window.__bwDebouncers) window.__bwDebouncers = Object.create(null);
               d.addEventListener('input', (e)=>{
                 const inp = e.target.closest && e.target.closest('input.bw-intro-start');
                 if (inp) {
                   const series = inp.dataset.series; if (!series) return;
                   const key = '__deb_intro_start_' + series;
                   if (window.__bwDebouncers[key]) clearTimeout(window.__bwDebouncers[key]);
                   window.__bwDebouncers[key] = setTimeout(()=>{
                     const seconds = parseInt(inp.value||'0',10)||0;
                     localStorage.setItem('bw_intro_start_update', JSON.stringify({series, seconds}));
                   }, 600);
                 }
                 
                 const inpEnd = e.target.closest && e.target.closest('input.bw-intro-end');
                 if (inpEnd) {
                   const series = inpEnd.dataset.series; if (!series) return;
                   const key = '__deb_intro_end_' + series;
                   if (window.__bwDebouncers[key]) clearTimeout(window.__bwDebouncers[key]);
                   window.__bwDebouncers[key] = setTimeout(()=>{
                     const seconds = parseInt(inpEnd.value||'0',10)||0;
                     localStorage.setItem('bw_intro_end_update', JSON.stringify({series, seconds}));
                   }, 600);
                 }
                 
                 const endInp = e.target.closest && e.target.closest('input.bw-end');
                 if (endInp) {
                   const series = endInp.dataset.series; if (!series) return;
                   const key = '__deb_end_' + series;
                   if (window.__bwDebouncers[key]) clearTimeout(window.__bwDebouncers[key]);
                   window.__bwDebouncers[key] = setTimeout(()=>{
                     const seconds = parseInt(endInp.value||'0',10)||0;
                     localStorage.setItem('bw_end_update', JSON.stringify({series, seconds}));
                   }, 600);
                 }
               });

              // APIs & Keepalive
              window.__bwLastHTML = '';
              window.__bwSetList = function (newHtml) {
                if (localStorage.getItem('bw_nav_inflight') === '1') return;
                if (typeof newHtml !== 'string') return;
                if (window.__bwLastHTML === newHtml) return;
                const l = document.getElementById('bwSeriesList');
                if (l) l.innerHTML = newHtml;
                window.__bwLastHTML = newHtml;
              };

              function ensureSidebar(){
                if (document.getElementById('bingeSidebar')) return;
                try { localStorage.setItem('bw_need_reinject','1'); } catch(_){}
              }
              const _rs = history.replaceState; history.replaceState = function(){ const r=_rs.apply(this,arguments); setTimeout(ensureSidebar,0); return r; };
              window.addEventListener('popstate', ensureSidebar);
              window.addEventListener('hashchange', ensureSidebar);
            }

            if (typeof html === 'string') {
              if (window.__bwLastHTML !== html) {
                const list = document.getElementById('bwSeriesList');
                if (list) list.innerHTML = html;
                window.__bwLastHTML = html;
              }
            }

            function ensureSidebar(){
            if (document.getElementById('bingeSidebar')) return;
                try { localStorage.setItem('bw_need_reinject','1'); } catch(_){}
            }
            const _rs = history.replaceState; history.replaceState = function(){ const r=_rs.apply(this,arguments); setTimeout(ensureSidebar,0); return r; };
            window.addEventListener('popstate', ensureSidebar);
            window.addEventListener('hashchange', ensureSidebar);
          } catch(e) { console.error('Sidebar injection failed', e); }
        })(arguments[0]);
        """,
            html_concat,
        )
        return True
    except Exception as e:
        logging.error(f"Sidebar injection failed: {e}")
        return False


def clear_nav_lock(driver):
    try:
        driver.switch_to.default_content()
        driver.execute_script(
            """
            try { localStorage.removeItem('bw_nav_inflight'); } catch(_){}
            try {
              const b = document.getElementById('bwBody');
              if (b){ b.style.pointerEvents=''; b.style.opacity=''; }
            } catch(_){}
        """
        )
    except Exception:
        pass


# === MAIN ===
def main() -> None:
    global should_quit
    logging.info("BingeWatcher is starting...")
    restarts = 0
    driver: Optional[webdriver.Firefox] = None
    
    try:
        driver = start_browser()
        if not safe_navigate(driver, START_URL):
            raise BingeWatcherError("Home page could not be loaded")

        while not should_quit:
            try:
                driver.switch_to.default_content()
                db = load_progress()

                if not driver.execute_script(
                    "return !!document.getElementById('bingeSidebar');"
                ):
                    settings = get_settings(driver)
                    inject_sidebar(driver, load_progress())
                    sync_settings_to_localstorage(driver)

                # Quit via localStorage
                try:
                    qls = driver.execute_script(
                        "try { return localStorage.getItem('bw_quit'); } catch(e) { return null; }"
                    )
                    if qls == "1":
                        driver.execute_script(
                            "try { localStorage.removeItem('bw_quit'); } catch(e) {}"
                        )
                        should_quit = True
                        break
                except Exception:
                    pass

                # Quit via cookie
                if get_cookie(driver, "bw_quit") == "1":
                    delete_cookie(driver, "bw_quit")
                    should_quit = True
                    break

                # Handle deletion
                try:
                    tod = driver.execute_script(
                        """
                        let s = localStorage.getItem('bw_seriesToDelete');
                        if (s) localStorage.removeItem('bw_seriesToDelete');
                        return s;
                        """
                    )
                    if tod:
                        handle_list_item_deletion(str(tod))
                        settings = get_settings(driver)
                        inject_sidebar(driver, load_progress())
                        sync_settings_to_localstorage(driver)
                        html = build_items_html(load_progress(), settings)
                        driver.execute_script(
                            "if (window.__bwSetList){window.__bwSetList(arguments[0]);}",
                            html,
                        )
                        continue
                except Exception:
                    pass

                try:
                    need = driver.execute_script(
                        """
                        let v = localStorage.getItem('bw_need_reinject');
                        if (v) localStorage.removeItem('bw_need_reinject');
                        return v;
                    """
                    )
                    if need:
                        settings = get_settings(driver)
                        inject_sidebar(driver, load_progress())
                        sync_settings_to_localstorage(driver)
                        html = build_items_html(load_progress(), settings)
                        driver.execute_script(
                            "if (window.__bwSetList){window.__bwSetList(arguments[0]);}",
                            html,
                        )
                except Exception:
                    pass

                # Handle UI update needed (from settings panel save)
                try:
                    ui_update_needed = driver.execute_script(
                        """
                        let r = localStorage.getItem('bw_ui_update_needed');
                        if (r) localStorage.removeItem('bw_ui_update_needed');
                        return r;
                    """
                    )
                    if ui_update_needed:
                        # UI sofort aktualisieren, um neue Eingabefelder anzuzeigen/verstecken
                        try:
                            html = build_items_html(load_progress(), get_settings(driver))
                            driver.execute_script(
                                "if (window.__bwSetList){window.__bwSetList(arguments[0]);}",
                                html,
                            )
                        except Exception:
                            pass
                except Exception:
                    pass

                # Handle settings updates (from settings panel)
                try:
                    upd = driver.execute_script(
                        """
                        let r = localStorage.getItem('bw_settings_update');
                        if (r) localStorage.removeItem('bw_settings_update');
                        return r;
                    """
                    )
                    if upd:
                        data = json.loads(upd)
                        save_settings_file(data)

                        try:
                            driver.execute_script(
                                "localStorage.setItem('bw_settings', arguments[0]);",
                                json.dumps(load_settings_file()),
                            )
                        except Exception:
                            pass
                except Exception:
                    pass

                # Handle intro start updates (from sidebar input) – normalisieren + live anwenden
                try:
                    upd = driver.execute_script(
                        """
                        let r = localStorage.getItem('bw_intro_start_update');
                        if (r) localStorage.removeItem('bw_intro_start_update');
                        return r;
                    """
                    )
                    if upd:
                        data = json.loads(upd)
                        ser_raw = data.get("series", "")
                        secs_raw = data.get("seconds", 0)
                        ser = norm_series_key(ser_raw)
                        try:
                            secs = max(0, int(float(secs_raw)))
                        except Exception:
                            secs = 0

                        if ser:
                            # Get current end time to preserve it
                            current_end = get_intro_skip_end_seconds(ser)
                            set_intro_skip_seconds(ser, secs, current_end)

                            # UI sofort aktualisieren
                            html = build_items_html(load_progress(), get_settings(driver))
                            driver.execute_script(
                                "if (window.__bwSetList){window.__bwSetList(arguments[0]);}",
                                html,
                            )
                except Exception:
                    pass

                # Handle intro end updates (from sidebar input) – normalisieren + live anwenden
                try:
                    upd = driver.execute_script(
                        """
                        let r = localStorage.getItem('bw_intro_end_update');
                        if (r) localStorage.removeItem('bw_intro_end_update');
                        return r;
                    """
                    )
                    if upd:
                        data = json.loads(upd)
                        ser_raw = data.get("series", "")
                        secs_raw = data.get("seconds", 0)
                        ser = norm_series_key(ser_raw)
                        try:
                            secs = max(0, int(float(secs_raw)))
                        except Exception:
                            secs = 0

                        if ser:
                            # Get current start time to preserve it
                            current_start = get_intro_skip_seconds(ser)
                            set_intro_skip_seconds(ser, current_start, secs)

                            # UI sofort aktualisieren
                            html = build_items_html(load_progress(), get_settings(driver))
                            driver.execute_script(
                                "if (window.__bwSetList){window.__bwSetList(arguments[0]);}",
                                html,
                            )
                except Exception:
                    pass

                # Handle end screen updates (from sidebar input) – normalisieren + live anwenden
                try:
                    upd = driver.execute_script(
                        """
                        let r = localStorage.getItem('bw_end_update');
                        if (r) localStorage.removeItem('bw_end_update');
                        return r;
                    """
                    )
                    if upd:
                        data = json.loads(upd)
                        ser_raw = data.get("series", "")
                        secs_raw = data.get("seconds", 0)
                        ser = norm_series_key(ser_raw)
                        try:
                            secs = max(0, int(float(secs_raw)))
                        except Exception:
                            secs = 0

                        if ser:
                            set_end_skip_seconds(ser, secs)

                            # UI sofort aktualisieren
                            html = build_items_html(load_progress(), get_settings(driver))
                            driver.execute_script(
                                "if (window.__bwSetList){window.__bwSetList(arguments[0]);}",
                                html,
                            )
                except Exception:
                    pass

                # Manual selection via cookie or localStorage
                sel = get_cookie(driver, "bw_series")
                series_provider = get_cookie(driver, "bw_series_provider")

                # Fallback: LS lesen, falls Cookie nicht ankam
                if not sel:
                    try:
                        sel = driver.execute_script(
                            "try { return localStorage.getItem('bw_series'); } catch(e) { return null; }"
                        )
                    except Exception:
                        sel = None
                
                if not series_provider:
                    try:
                        series_provider = driver.execute_script(
                            "try { return localStorage.getItem('bw_series_provider'); } catch(e) { return null; }"
                        )
                    except Exception:
                        series_provider = None

                if sel:
                    try:
                        sel = unquote(sel)
                    except Exception:
                        pass

                    sel = norm_series_key(sel)

                    # Aufräumen (beides)
                    delete_cookie(driver, "bw_series")
                    delete_cookie(driver, "bw_series_provider")
                    try:
                        driver.execute_script(
                            "try { localStorage.removeItem('bw_series'); } catch(e) {}"
                        )
                        driver.execute_script(
                            "try { localStorage.removeItem('bw_series_provider'); } catch(e) {}"
                        )
                    except Exception:
                        pass

                    sdata = db.get(sel)
                    if sdata:
                        season = int(sdata.get("season", 1))
                        episode = int(sdata.get("episode", 1))
                        position = int(sdata.get("position", 0))
                    else:
                        # Falls nicht im Fortschritt (sollte selten sein)
                        season, episode, position = 1, 1, 0

                    # Verwende den Provider der Serie oder den ausgewählten Provider
                    if series_provider and series_provider in STREAMING_PROVIDERS:
                        selected_provider = series_provider
                    else:
                        try:
                            selected_provider = driver.execute_script(
                                "try { return localStorage.getItem('bw_website_switch') || 's.to'; } catch(e) { return 's.to'; }"
                            )
                        except Exception:
                            selected_provider = "s.to"
                    
                    # Verwende den ausgewählten Provider für die Navigation
                    provider_info = STREAMING_PROVIDERS.get(selected_provider, STREAMING_PROVIDERS["s.to"])
                    target_url = provider_info["episode_url_template"].format(
                        series=sel, season=season, episode=episode
                    )
                    if safe_navigate(driver, target_url):
                        play_episodes_loop(driver, sel, season, episode, position, selected_provider)
                    continue

                # Auto detect if user navigated into an episode
                ser, se, ep, detected_provider = parse_episode_info(driver.current_url or "")
                if ser and se and ep:
                    # Verwende den erkannten Provider oder den ausgewählten Provider
                    try:
                        selected_provider = driver.execute_script(
                            "try { return localStorage.getItem('bw_website_switch') || 's.to'; } catch(e) { return 's.to'; }"
                        )
                        # Wenn der erkannte Provider mit dem ausgewählten übereinstimmt oder kein Provider erkannt wurde
                        if detected_provider == selected_provider or not detected_provider:
                            provider = selected_provider
                        else:
                            provider = detected_provider
                    except Exception:
                        provider = detected_provider or "s.to"
                    
                    sdata = load_progress().get(ser, {})
                    if (
                        int(sdata.get("season", -1)) == se
                        and int(sdata.get("episode", -1)) == ep
                    ):
                        pos = int(sdata.get("position", 0))
                    else:
                        pos = 0
                    play_episodes_loop(driver, ser, se, ep, pos, provider)
                    continue

                time.sleep(0.8)
            except (InvalidSessionIdException, WebDriverException) as e:
                logging.warning(f"Session error: {e}. Restarting Firefox...")
                try:
                    if driver:
                        driver.quit()
                except Exception:
                    pass
                if restarts >= 2:
                    logging.error("Too many restarts, giving up.")
                    should_quit = True
                    break
                restarts += 1
                driver = start_browser()
                if not safe_navigate(driver, START_URL):
                    logging.error("Restarted, but start page failed.")
                    should_quit = True
                    break
                arm_window_close_guard(driver)
                continue
            except Exception as e:
                logging.warning(f"Main-Loop Warning: {e}")
                time.sleep(1.2)

    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    except Exception as e:
        logging.error(f"Fatal: {e}")
    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass
        logging.info("BingeWatcher finished")


if __name__ == "__main__":
    main()
