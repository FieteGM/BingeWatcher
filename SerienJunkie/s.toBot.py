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

USE_TOR_PROXY: bool = os.getenv("BW_USE_TOR", "true").lower() in {"1", "true", "yes"}
TOR_SOCKS_PORT: int = int(os.getenv("BW_TOR_PORT", "9050"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GECKO_DRIVER_PATH = os.path.join(SCRIPT_DIR, "geckodriver.exe")

PROGRESS_DB_FILE = os.path.join(SCRIPT_DIR, "progress.json")
SETTINGS_DB_FILE = os.path.join(SCRIPT_DIR, "settings.json")

# === GLOBAL STATE ===
current_series: Optional[str] = None
current_season: Optional[int] = None
current_episode: Optional[int] = None
is_playing: bool = False
should_quit: bool = False

logging.basicConfig(
    format="[BingeWatcher] %(levelname)s: %(message)s", level=logging.INFO
)


class BingeWatcherError(Exception):
    pass


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
        val = int(data.get("intro_skip", INTRO_SKIP_SECONDS))
        return max(0, val)
    except Exception:
        return INTRO_SKIP_SECONDS


def set_intro_skip_seconds(series: str, seconds: int) -> bool:
    try:
        seconds = max(0, int(seconds))
        db = load_progress()
        entry = db.get(series, {}) if isinstance(db.get(series, {}), dict) else {}
        entry["intro_skip"] = seconds
        db[series] = entry
        with open(PROGRESS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"Intro time could not be saved: {e}")
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
        "autoNext": True,
        "playbackRate": 1.0,
        "volume": 1.0,
    }


# === NAVIGATION HANDLING --------------------------- ===
def parse_episode_info(url):
    match = re.search(r"/serie/stream/([^/]+)/staffel-(\d+)/episode-(\d+)", url)
    if match:
        return match.group(1), int(match.group(2)), int(match.group(3))
    return None, None, None


def navigate_to_episode(driver, series, season, episode, db):
    target = f"https://s.to/serie/stream/{series}/staffel-{season}/episode-{episode}"
    driver.get(target)
    arm_window_close_guard(driver)
    time.sleep(2)

    cur = driver.current_url
    actual_series, actual_season, actual_episode = parse_episode_info(cur)
    if not actual_series:
        logging.error(f"Konnte URL nicht parsen: {cur}")
        # trotzdem Sidebar/Unlock
        inject_sidebar(driver, db)
        clear_nav_lock(driver)
        return season, episode

    # Akzeptiere Weiterleitungen als Wahrheit
    inject_sidebar(driver, db)
    clear_nav_lock(driver)
    return actual_season, actual_episode


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


def safe_save_progress(driver, series, season, episode) -> int:
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
        save_progress(series, season, episode, pos)
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


def play_episodes_loop(driver, series, season, episode, position=0):
    global should_quit
    current_episode = episode
    current_season = season
    
    # Spezielle Behandlung für One Piece (Staffel 11 Problem)
    is_one_piece = series.lower() in ['one-piece', 'one piece', 'onepiece']

    while True:
        db = load_progress()
        settings = get_settings(driver)
        auto_fs = settings["autoFullscreen"]
        auto_skip = settings["autoSkipIntro"]
        auto_next = settings["autoNext"]
        rate = settings["playbackRate"]
        vol = settings["volume"]

        print(
            f"\n[▶] Playing {series.capitalize()} – Season {current_season}, Episode {current_episode}"
        )
        
        # Navigiere zur Episode und prüfe auf Weiterleitungen
        actual_season, actual_episode = navigate_to_episode(driver, series, current_season, current_episode, db)
        
        # Falls wir zu einer anderen Staffel/Episode weitergeleitet wurden, aktualisiere die Variablen
        if actual_season != current_season or actual_episode != current_episode:
            logging.info(f"Navigation angepasst: S{current_season}E{current_episode} → S{actual_season}E{actual_episode}")
            current_season = actual_season
            current_episode = actual_episode
            
            # Aktualisiere auch den Fortschritt mit den korrekten Werten
            save_progress(series, current_season, current_episode, 0)
        
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

        if position and position > 0:
            skip_intro(driver, position)
        elif auto_skip:
            skip_intro(driver, get_intro_skip_seconds(series))
        position = 0

        play_video(driver)
        apply_media_settings(driver, rate, vol)

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
            driver.execute_script(
                """
                const v = document.querySelector('video');
                const secs = arguments[0];
                if (v && isFinite(v.duration) && v.currentTime < secs && secs < (v.duration - 3)) {
                    v.currentTime = secs;
                    try { v.play().catch(()=>{}); } catch(_) {}
                }
            """,
                const_secs,
            )
        except Exception:
            pass

        if auto_fs and not HEADLESS:
            _hide_sidebar(driver, True)
            ensure_video_context(driver)
            time.sleep(0.25)
            
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
                    time.sleep(0.25)
                    ok = enable_fullscreen(driver)

        try:
            for _ in range(12):
                playing = driver.execute_script(
                    "const v=document.querySelector('video'); return !!(v && !v.paused && v.readyState>2);"
                )
                if playing:
                    break
                driver.execute_script(
                    "const v=document.querySelector('video'); if(v){ try{ v.focus(); v.play().catch(()=>{}); }catch(e){} }"
                )
                time.sleep(0.2)
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

            save_progress(series, current_season, current_episode, cur_pos)

            driver.switch_to.default_content()
            html = build_items_html(load_progress())
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
                safe_save_progress(driver, series, current_season, current_episode)
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
                s2, se2, ep2 = parse_episode_info(cur_url)

                if s2 == series and (se2 is not None and ep2 is not None) \
                and (se2 != current_season or ep2 != current_episode):
                    # Interne Auto-Navigation (z. B. Next Episode / Redirect)
                    safe_save_progress(driver, series, current_season, current_episode)
                    current_season, current_episode = se2, ep2
                    auto_nav = True
                    break  # raus aus innerer Loop, outer Loop startet mit aktualisiertem Zustand

                elif s2 and s2 != series:
                    # Wirklicher Serienwechsel (vom User)
                    safe_save_progress(driver, series, current_season, current_episode)
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
                    auto_next = bool(upd.get("autoNext", auto_next))
                    rate = float(upd.get("playbackRate", rate))
                    vol = float(upd.get("volume", vol))

                    # In-memory Settings-Objekt konsistent halten
                    settings.update(
                        {
                            "autoFullscreen": auto_fs,
                            "autoSkipIntro": auto_skip,
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
                    html = build_items_html(load_progress())
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
                save_progress(series, current_season, current_episode, int(current_pos))
                last_save = now

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
                    test_series, test_season, test_episode = parsed_info
                    
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

        # 1) typische Overlay-Buttons
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
                    time.sleep(0.15)
                    break
            except Exception:
                pass

        # 2) direkt auf das <video> klicken
        v = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "video"))
        )
        ActionChains(driver).move_to_element(v).click().perform()
        time.sleep(0.05)

        # 3) explizit play() (Promise ignorieren)
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
                body = driver.find_element(By.TAG_NAME, "body")
                ActionChains(driver).move_to_element(body).click().perform()
                time.sleep(0.1)
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
                        # Mehrere Klick-Varianten versuchen
                        try:
                            # Direkter Klick
                            ActionChains(driver).move_to_element(el).click().perform()
                            time.sleep(0.3)
                            if _is_fullscreen(driver):
                                return True
                        except Exception:
                            pass
                        
                        try:
                            # JavaScript-Klick als Fallback
                            driver.execute_script("arguments[0].click();", el)
                            time.sleep(0.3)
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
            time.sleep(0.1)
            # Dann Doppelklick
            ActionChains(driver).move_to_element(v).double_click().perform()
            time.sleep(0.3)
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
            # 'f' Taste senden
            ActionChains(driver).send_keys("f").pause(0.1).perform()
            time.sleep(0.3)
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
                    time.sleep(0.1)
                    
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
                    time.sleep(0.4)
                    
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
            time.sleep(0.3)
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
            time.sleep(0.4)
            if _is_fullscreen(driver):
                return True
        except Exception:
            pass

        # 9. Browser-Fenster-Vollbild als Fallback
        try:
            driver.fullscreen_window()
            time.sleep(0.3)
            if _is_fullscreen(driver):
                return True
        except Exception:
            try:
                ActionChains(driver).send_keys(Keys.F11).perform()
                time.sleep(0.4)
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


def build_items_html(db: Dict[str, Dict[str, Any]]) -> str:
    items_html = []
    sorted_items = sorted(
        db.items(), key=lambda kv: float(kv[1].get("timestamp", 0)), reverse=True
    )
    for series_name, data in sorted_items:
        season = int(data.get("season", 1))
        episode = int(data.get("episode", 1))
        position = int(data.get("position", 0))
        ts_val = float(data.get("timestamp", 0))
        intro_val = int(data.get("intro_skip", INTRO_SKIP_SECONDS))
        safe_name = _html.escape(series_name, quote=True)

        items_html.append(
            f"""
      <div class="bw-series-item" data-series="{safe_name}" data-season="{season}" data-episode="{episode}" data-ts="{ts_val}"
           style="margin:8px;padding:16px;background:linear-gradient(135deg,rgba(255,255,255,.05),rgba(255,255,255,.02));
                  border:1px solid rgba(255,255,255,.1);border-radius:12px;cursor:pointer;position:relative;">
        <div style="font-weight:600;font-size:14px;color:#f8fafc;margin-bottom:4px;">{safe_name}</div>
        <div style="font-size:12px;color:#94a3b8;display:flex;align-items:center;gap:8px;">
          <span style="background:rgba(59,130,246,.2);padding:2px 6px;border-radius:4px;border:1px solid rgba(59,130,246,.3);">S{season}E{episode}</span>
          <span style="opacity:.7;">{position}s</span>
        </div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:8px;">
          <input class="bw-intro" data-series="{safe_name}" type="number" min="0" value="{intro_val}" style="width:80px;padding:6px 8px;border-radius:8px;border:1px solid rgba(255,255,255,.15);background:rgba(2,6,23,.35);color:#e2e8f0;"/>
          <div class="bw-delete" data-series="{safe_name}" style="color:#ef4444;cursor:pointer;padding:6px;border-radius:6px;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.2);font-size:12px;">✕</div>
        </div>
      </div>
    """
        )
    return "\n".join(items_html)


def inject_sidebar(driver: webdriver.Firefox, db: Dict[str, Dict[str, Any]]) -> bool:
    try:
        driver.switch_to.default_content()
        html_concat = build_items_html(db)
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

                  <div style="margin-top:12px;display:flex;gap:8px;">
                  <input id="bwSearch" placeholder="Search..." style="flex:1;padding:8px;border-radius:10px;border:1px solid rgba(255,255,255,.12);background:rgba(2,6,23,.35);color:#e2e8f0;"/>
                  <select id="bwSort" style="padding:8px;border-radius:10px;border:1px solid rgba(255,255,255,.12);background:rgba(2,6,23,.35);color:#e2e8f0;">
                      <option value="time">Last watched</option>
                      <option value="name">Name</option>
                  </select>
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
                border:1px solid rgba(148,163,184,.35); background:rgba(2,6,23,.85);
                backdrop-filter:blur(10px); display:flex; align-items:center; justify-content:center; cursor:pointer;
                box-shadow:0 6px 20px rgba(0,0,0,.4);
                transition: transform .2s ease, background .2s ease, border-color .2s ease;
              }
              #bingeSidebar .bw-handle::after{ content:""; position:absolute; inset:-8px; } /* größere Klickfläche */
              #bingeSidebar .bw-handle:hover{ transform:translateY(-1px); background:rgba(15,23,42,.9); border-color:rgba(148,163,184,.5); }
              #bingeSidebar .chev{ font-size:16px; line-height:1; transition: transform .2s ease; }
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
              `;
              d.appendChild(style);
  
              const tgl = document.getElementById('bwCollapse');
              const setHandleTitle = () => {
                const collapsed = d.getAttribute('data-collapsed') === '1';
                tgl.title = collapsed ? 'Unfold' : 'Collapse';
              };
              if (localStorage.getItem('bw_sidebar_collapsed') === '1') d.setAttribute('data-collapsed','1');
              setHandleTitle();
  
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

              function onSort(){
                const mode = document.getElementById('bwSort').value;
                const list = document.getElementById('bwSeriesList');
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
                const list = document.getElementById('bwSeriesList');
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
                  Object.assign(p.style, { position:'fixed', right:'16px', top:'64px', width:'340px', background:'rgba(2,6,23,.94)', border:'1px solid rgba(255,255,255,.12)', borderRadius:'12px', color:'#e2e8f0', padding:'16px', zIndex:2147483647 });
                  p.innerHTML = `
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                      <div style="font-weight:600">Settings</div>
                      <button id="bwCloseSettings" style="background:transparent;border:0;color:#94a3b8;cursor:pointer;font-size:18px;">✕</button>
                    </div>
                    <label style="display:flex;align-items:center;gap:8px;margin:8px 0;">
                      <input type="checkbox" id="bwOptAutoFullscreen"/><span>Auto-Fullscreen</span>
                    </label>
                    <label style="display:flex;align-items:center;gap:8px;margin:8px 0;">
                      <input type="checkbox" id="bwOptAutoSkipIntro"/><span>Skip intro</span>
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
                    <label style="display:flex;align-items:center;gap:8px;margin:8px 0;">
                        <span>Volume</span>
                        <input type="range" id="bwOptVolume" min="0" max="1" step="0.05" style="flex:1"/>
                        <span id="bwVolumeVal" style="width:40px;text-align:right;"></span>
                    </label>
                    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px;">
                      <button id="bwSaveSettings" style="padding:6px 10px;border-radius:8px;border:1px solid rgba(59,130,246,.35);background:rgba(59,130,246,.12);color:#93c5fd;cursor:pointer;">Save</button>
                    </div>
                  `;
                  document.body.appendChild(p);
                  try {
                    const s = JSON.parse(localStorage.getItem('bw_settings')||'{}');
                    const x = id => document.getElementById(id);
                    if (x('bwOptAutoFullscreen')) x('bwOptAutoFullscreen').checked = (s.autoFullscreen !== false);
                    if (x('bwOptAutoSkipIntro')) x('bwOptAutoSkipIntro').checked = (s.autoSkipIntro !== false);
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
                    if (ev.target && ev.target.id==='bwCloseSettings') { p.remove(); }
                    if (ev.target && ev.target.id==='bwSaveSettings') {
                      const next = {
                        autoFullscreen: !!document.getElementById('bwOptAutoFullscreen')?.checked,
                        autoSkipIntro: !!document.getElementById('bwOptAutoSkipIntro')?.checked,
                        autoNext: !!document.getElementById('bwOptAutoNext')?.checked,
                        playbackRate: parseFloat(document.getElementById('bwOptPlaybackRate')?.value || '1'),
                        volume: Math.max(0, Math.min(1, parseFloat(document.getElementById('bwOptVolume')?.value || '1')))
                      };
                      localStorage.setItem('bw_settings', JSON.stringify(next));
                      localStorage.setItem('bw_settings_update', JSON.stringify(next));
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
                    if (localStorage.getItem('bw_nav_inflight') === '1') return; // throttle
                    localStorage.setItem('bw_nav_inflight','1');

                    const body = document.getElementById('bwBody');
                    if (body) { body.style.pointerEvents='none'; body.style.opacity='.6'; }

                    const s = item.getAttribute('data-series') || '';
                    try { localStorage.setItem('bw_series', s); } catch(_) {}
                    document.cookie = 'bw_series=' + encodeURIComponent(s) + '; path=/';

                    setTimeout(()=>{ try{
                    if (localStorage.getItem('bw_nav_inflight') === '1') {
                        localStorage.removeItem('bw_nav_inflight');
                        if (body) { body.style.pointerEvents=''; body.style.opacity=''; }
                    }
                    }catch(_){ } }, 4000);
                    return;
                }
              });

              // Debounce für Intro-Input
              if (!window.__bwDebouncers) window.__bwDebouncers = Object.create(null);
              d.addEventListener('input', (e)=>{
                const inp = e.target.closest && e.target.closest('input.bw-intro');
                if (!inp) return;
                const series = inp.dataset.series; if (!series) return;
                const key = '__deb_' + series;
                if (window.__bwDebouncers[key]) clearTimeout(window.__bwDebouncers[key]);
                window.__bwDebouncers[key] = setTimeout(()=>{
                  const seconds = parseInt(inp.value||'0',10)||0;
                  localStorage.setItem('bw_intro_update', JSON.stringify({series, seconds}));
                }, 600);
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
                        inject_sidebar(driver, load_progress())
                        sync_settings_to_localstorage(driver)
                        html = build_items_html(load_progress())
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
                        inject_sidebar(driver, load_progress())
                        sync_settings_to_localstorage(driver)
                        html = build_items_html(load_progress())
                        driver.execute_script(
                            "if (window.__bwSetList){window.__bwSetList(arguments[0]);}",
                            html,
                        )
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

                # Handle intro updates (from sidebar input) – normalisieren + live anwenden
                try:
                    upd = driver.execute_script(
                        """
                        let r = localStorage.getItem('bw_intro_update');
                        if (r) localStorage.removeItem('bw_intro_update');
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
                            set_intro_skip_seconds(ser, secs)

                            # UI sofort aktualisieren
                            html = build_items_html(load_progress())
                            driver.execute_script(
                                "if (window.__bwSetList){window.__bwSetList(arguments[0]);}",
                                html,
                            )

                            # Live auf aktuell laufende Serie anwenden
                            try:
                                cur_ser, cur_se, cur_ep = parse_episode_info(
                                    driver.current_url or ""
                                )
                                if cur_ser == ser and ensure_video_context(driver):
                                    driver.execute_script(
                                        """
                                        const v = document.querySelector('video');
                                        const secs = arguments[0];
                                        if (v && isFinite(v.duration)) {
                                            const nearEnd = v.duration - v.currentTime <= 5;
                                            if (!nearEnd && (v.currentTime + 1) < secs && secs < (v.duration - 3)) {
                                                v.currentTime = secs;
                                                try { v.play().catch(()=>{}); } catch(_) {}
                                            }
                                        }
                                    """,
                                        secs,
                                    )
                            except Exception:
                                pass
                except Exception:
                    pass

                # Manual selection via cookie or localStorage
                sel = get_cookie(driver, "bw_series")

                # Fallback: LS lesen, falls Cookie nicht ankam
                if not sel:
                    try:
                        sel = driver.execute_script(
                            "try { return localStorage.getItem('bw_series'); } catch(e) { return null; }"
                        )
                    except Exception:
                        sel = None

                if sel:
                    try:
                        sel = unquote(sel)
                    except Exception:
                        pass

                    sel = norm_series_key(sel)

                    # Aufräumen (beides)
                    delete_cookie(driver, "bw_series")
                    try:
                        driver.execute_script(
                            "try { localStorage.removeItem('bw_series'); } catch(e) {}"
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

                    if safe_navigate(
                        driver,
                        f"{START_URL}serie/stream/{sel}/staffel-{season}/episode-{episode}",
                    ):
                        play_episodes_loop(driver, sel, season, episode, position)
                    continue

                # Auto detect if user navigated into an episode
                ser, se, ep = parse_episode_info(driver.current_url or "")
                if ser and se and ep:
                    sdata = load_progress().get(ser, {})
                    if (
                        int(sdata.get("season", -1)) == se
                        and int(sdata.get("episode", -1)) == ep
                    ):
                        pos = int(sdata.get("position", 0))
                    else:
                        pos = 0
                    play_episodes_loop(driver, ser, se, ep, pos)
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
