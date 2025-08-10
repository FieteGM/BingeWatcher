import os
import re
import sys
import time
import json
import logging
from typing import Optional, Tuple, Dict, Any
from urllib.parse import unquote

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchWindowException,
    JavascriptException,
    TimeoutException,
    WebDriverException,
    NoSuchElementException,
    ElementClickInterceptedException,
    StaleElementReferenceException,
    InvalidSessionIdException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.firefox.service import Service
import html as _html


# === CONFIGURATION ===
HEADLESS: bool = os.getenv("BW_HEADLESS", "false").lower() in {"1", "true", "yes"}
START_URL: str = os.getenv("BW_START_URL", "https://s.to/")
INTRO_SKIP_SECONDS: int = int(os.getenv("BW_INTRO_SKIP", "80"))
MAX_RETRIES: int = int(os.getenv("BW_MAX_RETRIES", "3"))
WAIT_TIMEOUT: int = int(os.getenv("BW_WAIT_TIMEOUT", "20"))
PROGRESS_SAVE_INTERVAL: int = int(os.getenv("BW_PROGRESS_INTERVAL", "5"))

USE_TOR_PROXY: bool = os.getenv("BW_USE_TOR", "true").lower() in {"1", "true", "yes"}
TOR_SOCKS_PORT: int = int(os.getenv("BW_TOR_PORT", "9050"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GECKO_DRIVER_PATH = os.path.join(SCRIPT_DIR, "geckodriver.exe")
PROGRESS_DB_FILE = os.path.join(SCRIPT_DIR, "progress.json")


# === GLOBAL STATE ===
current_series: Optional[str] = None
current_season: Optional[int] = None
current_episode: Optional[int] = None
is_playing: bool = False
should_quit: bool = False


logging.basicConfig(format='[BingeWatcher] %(levelname)s: %(message)s', level=logging.INFO)


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
        logging.error("progress.json ist korrupt. Versuche Backup wiederherzustellen…")
        backup = PROGRESS_DB_FILE + ".backup"
        if os.path.exists(backup):
            try:
                with open(backup, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        logging.info("Backup wiederhergestellt.")
                        return data
            except Exception:
                pass
        return {}
    except Exception as e:
        logging.error(f"Fehler beim Laden des Fortschritts: {e}")
        return {}


def save_progress(series: str, season: int, episode: int, position: int, extra: Optional[Dict[str, Any]] = None) -> bool:
    try:
        db = load_progress()
        entry = db.get(series, {}) if isinstance(db.get(series, {}), dict) else {}
        entry.update({
            "season": int(season),
            "episode": int(episode),
            "position": int(position),
            "timestamp": time.time(),
        })
        if extra:
            entry.update(extra)
        db[series] = entry

        # Backup
        if os.path.exists(PROGRESS_DB_FILE):
            import shutil
            shutil.copy2(PROGRESS_DB_FILE, PROGRESS_DB_FILE + ".backup")

        with open(PROGRESS_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.error(f"Fehler beim Speichern des Fortschritts: {e}")
        return False


def handle_list_item_deletion(name: str) -> bool:
    try:
        db = load_progress()
        if name in db:
            del db[name]
            with open(PROGRESS_DB_FILE, "w", encoding="utf-8") as f:
                json.dump(db, f, indent=2, ensure_ascii=False)
            logging.info(f"Serie gelöscht: {name}")
        return True
    except Exception as e:
        logging.error(f"Löschen fehlgeschlagen: {e}")
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
        logging.error(f"Intro-Zeit konnte nicht gespeichert werden: {e}")
        return False


# === UTILS: COOKIES ===
def get_cookie(driver: webdriver.Firefox, name: str) -> Optional[str]:
    try:
        for c in driver.get_cookies():
            if c.get("name") == name:
                return c.get("value")
    except Exception:
        try:
            driver.switch_to.default_content()
            for c in driver.get_cookies():
                if c.get("name") == name:
                    return c.get("value")
        except Exception:
            pass
    return None


def set_cookie(driver: webdriver.Firefox, name: str, value: str) -> bool:
    try:
        driver.add_cookie({"name": name, "value": value, "path": "/"})
        return True
    except Exception:
        return False


def delete_cookie(driver: webdriver.Firefox, name: str) -> bool:
    try:
        driver.delete_cookie(name)
        return True
    except Exception:
        return False


# === BROWSER ===
def start_browser() -> webdriver.Firefox:
    try:
        profile_path = os.path.join(SCRIPT_DIR, "user.BingeWatcher")
        os.makedirs(profile_path, exist_ok=True)

        # General
        options = webdriver.FirefoxOptions()
        options.set_preference("dom.popup_allowed_events", "change click dblclick mouseup pointerup touchend")
        options.set_preference("general.useragent.override",
                               "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0")
        options.set_preference("dom.allow_scripts_to_close_windows", True)
        options.set_preference("browser.tabs.warnOnClose", False)
        options.set_preference("browser.warnOnQuit", False)
        options.set_preference("browser.sessionstore.warnOnQuit", False)
        # Autoplay
        options.set_preference("media.autoplay.default", 0)  # 0=allow all
        options.set_preference("media.block-autoplay-until-in-foreground", False)
        options.set_preference("media.autoplay.blocking_policy", 0)
        options.set_preference("media.autoplay.allow-muted", True)
        # Profile
        options.set_preference("profile", profile_path)
        options.profile = profile_path

        if USE_TOR_PROXY:
            # Route traffic through Tor SOCKS
            options.set_preference("network.proxy.type", 1)
            options.set_preference("network.proxy.socks", "127.0.0.1")
            options.set_preference("network.proxy.socks_port", TOR_SOCKS_PORT)
            options.set_preference("network.proxy.socks_remote_dns", True)

        if HEADLESS:
            options.add_argument("--headless")

        if not os.path.exists(GECKO_DRIVER_PATH):
            raise BingeWatcherError(f"Geckodriver fehlt unter {GECKO_DRIVER_PATH}")

        service = Service(executable_path=GECKO_DRIVER_PATH)
        driver = webdriver.Firefox(service=service, options=options)
        driver.set_window_size(1920, 1080)
        logging.info(f"Browser gestartet. Profil: {profile_path} | Tor: {'an' if USE_TOR_PROXY else 'aus'}")
        return driver
    except Exception as e:
        logging.error(f"Browserstart fehlgeschlagen: {e}")
        raise BingeWatcherError("Browserstart fehlgeschlagen")


def safe_navigate(driver: webdriver.Firefox, url: str, max_retries: int = MAX_RETRIES) -> bool:
    for attempt in range(max_retries):
        try:
            driver.get(url)
            WebDriverWait(driver, WAIT_TIMEOUT).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(1.5)
            return True
        except WebDriverException as e:
            logging.warning(f"Navigation fehlgeschlagen (Versuch {attempt + 1}/{max_retries}): {e}")
            time.sleep(2)
    logging.error(f"Navigation zu {url} nach {max_retries} Versuchen gescheitert")
    return False


def parse_episode_info(url: str) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    try:
        m = re.search(r"/serie/stream/([^/]+)/staffel-(\d+)/episode-(\d+)", url)
        if m:
            return m.group(1), int(m.group(2)), int(m.group(3))
    except Exception:
        pass
    return None, None, None


def is_browser_responsive(driver: webdriver.Firefox) -> bool:
    try:
        url = driver.current_url
        return bool(url) and url != "about:blank"
    except Exception:
        return False


def navigate_to_episode(driver: webdriver.Firefox, series: str, season: int, episode: int) -> bool:
    url = f"{START_URL}serie/stream/{series}/staffel-{season}/episode-{episode}"
    if not safe_navigate(driver, url):
        return False
    
    if not driver.execute_script("return !!document.getElementById('bingeSidebar');"):
        inject_sidebar(driver, load_progress())
    return True


def _dismiss_consent_and_overlays(driver: webdriver.Firefox) -> None:
    driver.switch_to.default_content()
    try:
        # gängige Consent-Texte
        labels = [
            "Akzeptieren","Zustimmen","Einverstanden","Alles akzeptieren",
            "Accept","Agree","I agree","Allow all","Got it"
        ]
        # Buttons/links mit Label
        for lb in labels:
            try:
                el = WebDriverWait(driver, 1).until(
                    EC.element_to_be_clickable((By.XPATH, f"//*[self::button or self::a][contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '{lb.lower()}')]"))
                )
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                try:
                    el.click()
                except Exception:
                    ActionChains(driver).move_to_element(el).click().perform()
            except Exception:
                pass
        # bekannte Container wegklicken
        driver.execute_script("""
            try{
              const ids=['didomi','sp_message_container','qc-cmp2-container','usercentrics-root','consent'];
              ids.forEach(id=>{ const e=document.getElementById(id); if(e) e.remove(); });
              const sel=['.sp_veil','.qc-cmp2-container','.pm-accept','.uc-overlay','.cc-window','.osano-cm-dialog'];
              sel.forEach(s=>document.querySelectorAll(s).forEach(x=>x.remove()));
            }catch(_){}
        """)
    except Exception:
        pass


def open_preferred_hoster(driver: webdriver.Firefox) -> bool:
    driver.switch_to.default_content()
    _dismiss_consent_and_overlays(driver)

    # etwas scrollen, damit hoster-liste sicher im DOM ist
    try:
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.3)
        for _ in range(3):
            driver.execute_script("window.scrollBy(0, window.innerHeight * 0.9);")
            time.sleep(0.3)
    except Exception:
        pass

    handles_before = set(driver.window_handles)
    preferred = ["VOE","STREAMTAPE","DOOD","VIDOZA","VIDMOLY","SIBNET","VIDSTREAM","UPSTREAM","FILEMOON"]

    # 1) Mögliche Kandidaten im DOM einsammeln (CSS/XPath, inkl. data-link/-href)
    candidates = []
    try:
        # per XPath: sichtbare Knoten mit Hosternamen im Text ODER href ODER data-Attribut
        xpath = "|".join([
            f"//a[contains(translate(.,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'{h}')]",
            f"//button[contains(translate(.,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'{h}')]",
            f"//*[@data-link][contains(translate(@data-link,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'{h}')]",
            f"//*[@data-href][contains(translate(@data-href,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'{h}')]",
            f"//a[contains(@href, '{h.lower()}') or contains(@href, '{h.capitalize()}')]",
        ] for h in preferred)
        # flatten
        flat = []
        for h in preferred:
            flat.extend(driver.find_elements(By.XPATH, f"//a[contains(translate(.,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'{h}')]"))
            flat.extend(driver.find_elements(By.XPATH, f"//button[contains(translate(.,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'{h}')]"))
            flat.extend(driver.find_elements(By.XPATH, f"//*[@data-link][contains(translate(@data-link,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'{h}')]"))
            flat.extend(driver.find_elements(By.XPATH, f"//*[@data-href][contains(translate(@data-href,'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'{h}')]"))
            flat.extend(driver.find_elements(By.XPATH, f"//a[contains(@href, '{h.lower()}') or contains(@href, '{h.capitalize()}')]"))
        # unique
        seen = set()
        for el in flat:
            try:
                key = (el.tag_name, el.get_attribute("outerHTML")[:160])
            except Exception:
                key = id(el)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(el)
    except Exception:
        pass

    # 2) Fallback: redirect-Links (s.to nutzt oft /redirect/)
    try:
        redirects = driver.find_elements(By.CSS_SELECTOR, "a[href*='/redirect/'], a[data-href*='/redirect/']")
        for el in redirects:
            if el not in candidates:
                candidates.append(el)
    except Exception:
        pass

    # 3) Letzter Fallback: beliebige "watch"/"mirror"/"hoster" Items
    try:
        fuzzy = driver.find_elements(By.XPATH, "//*[contains(translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'watch') or contains(translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'hoster') or contains(translate(@class,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'mirror')]")
        for el in fuzzy:
            if el not in candidates:
                candidates.append(el)
    except Exception:
        pass

    logging.info(f"Hoster-Kandidaten gefunden: {len(candidates)}")

    # 4) Nacheinander versuchen zu öffnen
    for el in candidates:
        try:
            href = (el.get_attribute("href") or el.get_attribute("data-link") or el.get_attribute("data-href") or "").strip()
            label = (el.text or el.get_attribute("aria-label") or el.get_attribute("title") or "")[:60]
            logging.info(f"Versuche Hoster: label='{label}' href='{href}'")

            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.15)

            clickable = None
            try:
                clickable = WebDriverWait(driver, 2).until(EC.element_to_be_clickable(el))
            except Exception:
                clickable = el

            # Selenium-Klick bevorzugen (gilt als user gesture)
            try:
                ActionChains(driver).move_to_element(clickable).pause(0.05).click().perform()
            except Exception:
                try:
                    clickable.click()
                except Exception:
                    # JS-Fallback: window.open bei data-link/href
                    if href.startswith("http"):
                        driver.execute_script("window.open(arguments[0], '_blank');", href)

            # auf neues Fenster oder Domainwechsel warten
            def on_hoster():
                hds = set(driver.window_handles)
                if len(hds) > len(handles_before):
                    return True
                cur = driver.current_url.lower()
                return any(k in cur for k in ["voe","streamtape","dood","vidoza","vidmoly","sibnet","filemoon","upstream","vidstream"])

            try:
                WebDriverWait(driver, 6).until(lambda d: on_hoster())
            except TimeoutException:
                # nächster Kandidat
                continue

            # Wir sind drauf—nun auf ein Fenster mit <video> wechseln
            ok = switch_to_any_window_with_video(driver, max_depth=7)
            if ok:
                return True
            # falls wir hier sind: falsches Popup => nächster Kandidat
        except Exception:
            continue

    logging.error("Keinen funktionierenden Hoster öffnen können.")
    return False


# === VIDEO HANDLING ===
def _has_video(driver: webdriver.Firefox) -> bool:
    try:
        return bool(driver.execute_script("return !!document.querySelector('video')"))
    except Exception:
        return False


def switch_to_any_window_with_video(driver: webdriver.Firefox, max_depth: int = 10) -> bool:
    def has_video_here() -> bool:
        try:
            driver.switch_to.default_content()
        except Exception:
            return False

        return switch_to_frame_with_video(driver, max_depth=max_depth)

    # Erst im aktuellen Fenster probieren
    if has_video_here():
        return True

    # Dann alle Fenster durchgehen
    for handle in driver.window_handles:
        try:
            driver.switch_to.window(handle)
            if has_video_here():
                return True
        except Exception:
            continue
    return False


def switch_to_frame_with_video(driver: webdriver.Firefox, max_depth: int = 5) -> bool:
    # Check main document first
    try:
        driver.switch_to.default_content()
    except Exception:
        return False

    if _has_video(driver):
        return True

    def dfs(level: int) -> bool:
        if level > max_depth:
            return False
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for idx, iframe in enumerate(iframes):
            try:
                driver.switch_to.frame(iframe)
                if _has_video(driver):
                    return True
                # Recurse one level deeper
                if dfs(level + 1):
                    return True
            except Exception:
                pass
            finally:
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass
        return False

    return dfs(1)


def is_video_playing(driver: webdriver.Firefox) -> bool:
    try:
        return bool(driver.execute_script(
            """
            const v = document.querySelector('video');
            return !!(v && !v.paused && v.readyState >= 2 && v.currentTime > 0);
            """
        ))
    except Exception:
        return False


def try_start_playback(driver: webdriver.Firefox, max_retries: int = 3) -> bool:
    for attempt in range(max_retries):
        try:
            # In *irgendeinem* Fenster + Frame mit Video landen
            if not switch_to_any_window_with_video(driver, max_depth=5):
                time.sleep(1)
                continue

            video = None
            try:
                video = WebDriverWait(driver, 8).until(
                    EC.presence_of_element_located((By.TAG_NAME, "video"))
                )
            except TimeoutException:
                video = None

            # Häufig haben Player ein Overlay / Big-Play
            try:
                driver.execute_script("""
                    (function(){
                      const sels = [
                        '.vjs-big-play-button',
                        '.jw-display', '.jw-icon-play',
                        '.plyr__control[data-plyr="play"]',
                        'button[aria-label="Play"]',
                        '.fp-ui .fp-play'
                      ];
                      for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el) { el.click(); break; }
                      }
                    })();
                """)
            except Exception:
                pass

            # Direkt play() versuchen (Autoplay-Blocker umgehen wir mit muted)
            try:
                driver.execute_script("""
                    const v = document.querySelector('video');
                    if (v) { try { v.muted = true; } catch(_) {}
                             try { v.play(); } catch(_) {} }
                """)
            except Exception:
                pass

            # Zusätzlich klicken & Space *auf das Video-Element*, nicht global
            if video is not None:
                try:
                    ActionChains(driver).move_to_element(video).click().perform()
                except Exception:
                    try:
                        video.click()
                    except Exception:
                        pass
                try:
                    video.send_keys(Keys.SPACE)
                except Exception:
                    pass

            time.sleep(1.2)
            if is_video_playing(driver):
                return True

        except Exception:
            pass

        logging.warning(f"Playback Start fehlgeschlagen (Versuch {attempt + 1}/{max_retries})")
        time.sleep(1.5)
    return False


def get_video_state(driver: webdriver.Firefox) -> Dict[str, Any]:
    try:
        return driver.execute_script(
            """
            const v = document.querySelector('video');
            if (!v) return { paused: true, ended: false, currentTime: 0, duration: 0, readyState: 0, playbackRate: 1, muted: false };
            return {
                paused: !!v.paused,
                ended: !!v.ended,
                currentTime: v.currentTime || 0,
                duration: v.duration || 0,
                readyState: v.readyState || 0,
                playbackRate: v.playbackRate || 1,
                muted: !!v.muted
            };
            """
        ) or {}
    except Exception:
        return {}


def set_video_time(driver: webdriver.Firefox, seconds: int) -> None:
    try:
        driver.execute_script(f"const v=document.querySelector('video'); if (v) v.currentTime = {int(seconds)};")
    except Exception:
        pass


def set_playback_rate(driver: webdriver.Firefox, rate: float) -> None:
    try:
        driver.execute_script(f"const v=document.querySelector('video'); if (v) v.playbackRate = {float(rate)};")
    except Exception:
        pass


def enable_fullscreen(driver: webdriver.Firefox) -> bool:
    try:
        driver.execute_script(
            """
            (function(){
                const v = document.querySelector('video');
                if (!v) return false;
                try {
                    if (v.requestFullscreen) { v.requestFullscreen(); return true; }
                    if (v.webkitRequestFullscreen) { v.webkitRequestFullscreen(); return true; }
                } catch(e) {}
                return false;
            })();
            """
        )
        return True
    except Exception:
        return False


def exit_fullscreen(driver: webdriver.Firefox) -> None:
    try:
        driver.switch_to.default_content()
        driver.execute_script(
            """
            if (document.exitFullscreen) document.exitFullscreen();
            else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
            else if (document.mozCancelFullScreen) document.mozCancelFullScreen();
            else if (document.msExitFullscreen) document.msExitFullscreen();
            """
        )
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except Exception:
        pass


# === SETTINGS (UI) ===
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


def inject_video_controls(driver: webdriver.Firefox, intro_seconds: int, default_rate: float) -> None:
    # Assumes we are already switched into the document that contains the video
    try:
        driver.execute_script(
            f"""
            (function(){{
                try {{
                    const DOC = document;
                    const id = 'bwVideoControls';
                    const old = DOC.getElementById(id);
                    if (old) old.remove();

                    const root = DOC.createElement('div');
                    root.id = id;
                    root.style.position = 'absolute';
                    root.style.right = '20px';
                    root.style.bottom = '24px';
                    root.style.zIndex = 2147483647;
                    root.style.display = 'flex';
                    root.style.gap = '10px';
                    root.style.pointerEvents = 'auto';

                    function mkBtn(svg, title) {{
                        const b = DOC.createElement('button');
                        b.title = title;
                        b.style.width = '44px';
                        b.style.height = '44px';
                        b.style.borderRadius = '999px';
                        b.style.border = '1px solid rgba(255,255,255,0.25)';
                        b.style.background = 'linear-gradient(135deg, rgba(15,23,42,0.6), rgba(2,6,23,0.6))';
                        b.style.backdropFilter = 'blur(6px)';
                        b.style.WebkitBackdropFilter = 'blur(6px)';
                        b.style.cursor = 'pointer';
                        b.style.display = 'flex';
                        b.style.alignItems = 'center';
                        b.style.justifyContent = 'center';
                        b.style.boxShadow = '0 10px 20px rgba(0,0,0,0.35)';
                        b.style.color = '#e2e8f0';
                        b.style.transition = 'transform .15s ease, background .2s ease';
                        b.onmouseenter = () => b.style.transform = 'translateY(-1px)';
                        b.onmouseleave = () => b.style.transform = 'translateY(0)';
                        b.innerHTML = svg; return b;
                    }}

                    const v = DOC.querySelector('video');
                    if (!v) return;

                    // Playback rate init
                    try {{ v.playbackRate = {float(default_rate)}; }} catch(_e) {{}}

                    const skip = mkBtn(`<svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M8 5v14l11-7-11-7z" fill="currentColor"/></svg>`, 'Skip Intro');
                    skip.addEventListener('click', () => {{ try {{ v.currentTime = {int(intro_seconds)}; }} catch(_e) {{}} }});

                    const slower = mkBtn(`<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M6 11h12v2H6z"/></svg>`, 'Slower');
                    slower.addEventListener('click', () => {{ try {{ v.playbackRate = Math.max(0.25, +(v.playbackRate||1) - 0.25); }} catch(_e) {{}} }});

                    const faster = mkBtn(`<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M6 11h7V6h2v5h3v2h-3v5h-2v-5H6z"/></svg>`, 'Faster');
                    faster.addEventListener('click', () => {{ try {{ v.playbackRate = Math.min(4, +(v.playbackRate||1) + 0.25); }} catch(_e) {{}} }});

                    root.appendChild(skip);
                    root.appendChild(slower);
                    root.appendChild(faster);

                    // Attach to nearest positioned ancestor
                    let host = v.parentElement, steps = 0;
                    while (host && steps < 5 && getComputedStyle(host).position === 'static') {{ host = host.parentElement; steps++; }}
                    (host || DOC.body).appendChild(root);

                    // Keyboard shortcuts inside iframe
                    DOC.addEventListener('keydown', (e) => {{
                        try {{
                            if (!v) return;
                            if (e.key === ' ') {{ e.preventDefault(); v.paused ? v.play() : v.pause(); }}
                            else if (e.key === 'ArrowLeft') {{ v.currentTime = Math.max(0, v.currentTime - 10); }}
                            else if (e.key === 'ArrowRight') {{ v.currentTime = Math.min(v.duration||1e9, v.currentTime + 10); }}
                            else if (e.key.toLowerCase() === 'f') {{ if (v.requestFullscreen) v.requestFullscreen(); }}
                            else if (e.key.toLowerCase() === 'm') {{ v.muted = !v.muted; }}
                            else if (e.key === '+') {{ v.playbackRate = Math.min(4, (v.playbackRate||1) + 0.25); }}
                            else if (e.key === '-') {{ v.playbackRate = Math.max(0.25, (v.playbackRate||1) - 0.25); }}
                        }} catch(_e) {{}}
                    }}, {{ capture: true }});

                    // Auto-next cancel flag integration
                    const an = DOC.getElementById('bwAutoNextPanel');
                    if (an) an.remove();

                }} catch(e) {{}}
            }})();
            """
        )
    except Exception:
        pass


def inject_sidebar(driver: webdriver.Firefox, db: Dict[str, Dict[str, Any]]) -> bool:
    try:
        driver.switch_to.default_content()
        html_concat = build_items_html(db)

        driver.execute_script("""
        (function(html){
          try {
            let d = document.getElementById('bingeSidebar');

            // Erst-Erstellung + Events nur einmal binden
            if (!d) {
              d = document.createElement('div');
              d.id = 'bingeSidebar';
              Object.assign(d.style, {
                position:'fixed', left:0, top:0, width:'340px', height:'100vh',
                background:'linear-gradient(180deg, rgba(15,23,42,.95), rgba(30,41,59,.95))',
                color:'#f8fafc', overflowY:'auto', zIndex:2147483647,
                borderRight:'1px solid rgba(255,255,255,.1)', backdropFilter:'blur(18px)'
              });
              d.innerHTML = `
                <div style="padding:16px;border-bottom:1px solid rgba(255,255,255,.1);">
                  <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
                    <div style="display:flex;align-items:center;gap:8px;">
                      <div style="width:8px;height:8px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);border-radius:999px;"></div>
                      <span style="font-weight:700;font-size:18px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">BingeWatcher</span>
                    </div>
                    <div style="display:flex;gap:8px;">
                      <button id="bwSettings" title="Settings" style="width:36px;height:36px;border-radius:10px;border:1px solid rgba(148,163,184,.35);background:rgba(148,163,184,.12);color:#cbd5e1;cursor:pointer;">⚙</button>
                      <button id="bwSkip" title="Skip episode" style="width:36px;height:36px;border-radius:10px;border:1px solid rgba(59,130,246,.35);background:rgba(59,130,246,.12);color:#93c5fd;cursor:pointer;">⏭</button>
                      <button id="bwQuit" title="Quit" style="width:36px;height:36px;border-radius:10px;border:1px solid rgba(239,68,68,.35);background:rgba(239,68,68,.12);color:#fecaca;cursor:pointer;">⏻</button>
                    </div>
                  </div>
                  <div style="margin-top:12px;display:flex;gap:8px;">
                    <input id="bwSearch" placeholder="Suche…" style="flex:1;padding:8px;border-radius:8px;border:1px solid rgba(255,255,255,.15);background:rgba(2,6,23,.35);color:#e2e8f0;"/>
                    <select id="bwSort" style="padding:8px;border-radius:8px;border:1px solid rgba(255,255,255,.15);background:rgba(2,6,23,.35);color:#e2e8f0;">
                      <option value="time">Zuletzt gesehen</option>
                      <option value="name">Name</option>
                    </select>
                  </div>
                </div>
                <div style="padding:12px;">
                  <div id="bwSeriesList" style="display:flex;flex-direction:column;gap:6px;"></div>
                </div>
              `;
              (document.body||document.documentElement).appendChild(d);

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

              // Delegierte Events: immer über 'd' + closest()
              d.addEventListener('input', (e)=>{ if (e.target && e.target.id==='bwSearch') onFilter(); });
              d.addEventListener('change', (e)=>{ if (e.target && e.target.id==='bwSort') onSort(); });

              d.addEventListener('click', (e)=>{
                const c = sel => e.target.closest && e.target.closest(sel);

                if (c('#bwSkip')) {
                  try { const v=document.querySelector('video'); if (v && v.duration) v.currentTime=Math.max(0, v.duration-1); } catch(_){}
                  return;
                }

                if (c('#bwQuit')) {
                    try { localStorage.setItem('bw_quit','1'); } catch(_){}
                    document.cookie='bw_quit=1; path=/';
                    return;
                }

                if (c('#bwSettings')) {
                  const existing = document.getElementById('bwSettingsPanel');
                  if (existing) { existing.remove(); return; }
                  const p = document.createElement('div');
                  Object.assign(p, { id:'bwSettingsPanel' });
                  Object.assign(p.style, { position:'fixed', right:'16px', top:'64px', width:'340px', background:'rgba(2,6,23,.94)', border:'1px solid rgba(255,255,255,.12)', borderRadius:'12px', color:'#e2e8f0', padding:'16px', zIndex:2147483647 });
                  p.innerHTML = `
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                      <div style="font-weight:600">Einstellungen</div>
                      <button id="bwCloseSettings" style="background:transparent;border:0;color:#94a3b8;cursor:pointer;font-size:18px;">✕</button>
                    </div>
                    <label style="display:flex;align-items:center;gap:8px;margin:8px 0;">
                      <input type="checkbox" id="bwOptAutoFullscreen"/><span>Auto-Fullscreen</span>
                    </label>
                    <label style="display:flex;align-items:center;gap:8px;margin:8px 0;">
                      <input type="checkbox" id="bwOptAutoSkipIntro"/><span>Intro automatisch überspringen</span>
                    </label>
                    <label style="display:flex;align-items:center;gap:8px;margin:8px 0;">
                      <input type="checkbox" id="bwOptAutoNext" checked/><span>Nächste Episode automatisch</span>
                    </label>
                    <label style="display:flex;align-items:center;gap:8px;margin:8px 0;">
                      <span>Start-Geschwindigkeit</span>
                      <select id="bwOptPlaybackRate">
                        <option value="0.75">0.75x</option>
                        <option value="1" selected>1x</option>
                        <option value="1.25">1.25x</option>
                        <option value="1.5">1.5x</option>
                        <option value="1.75">1.75x</option>
                        <option value="2">2x</option>
                      </select>
                    </label>
                    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px;">
                      <button id="bwSaveSettings" style="padding:6px 10px;border-radius:8px;border:1px solid rgba(59,130,246,.35);background:rgba(59,130,246,.12);color:#93c5fd;cursor:pointer;">Speichern</button>
                    </div>
                  `;
                  document.body.appendChild(p);
                  try {
                    const s = JSON.parse(localStorage.getItem('bw_settings')||'{}');
                    const x = id => document.getElementById(id);
                    if (x('bwOptAutoFullscreen')) x('bwOptAutoFullscreen').checked = !!s.autoFullscreen;
                    if (x('bwOptAutoSkipIntro')) x('bwOptAutoSkipIntro').checked = !!s.autoSkipIntro;
                    if (x('bwOptAutoNext')) x('bwOptAutoNext').checked = s.autoNext!==false;
                    if (x('bwOptPlaybackRate')) x('bwOptPlaybackRate').value = String(s.playbackRate||1);
                  } catch(_){}
                  p.addEventListener('click', (ev)=>{
                    if (ev.target && ev.target.id==='bwCloseSettings') { p.remove(); }
                    if (ev.target && ev.target.id==='bwSaveSettings') {
                      const next = {
                        autoFullscreen: !!document.getElementById('bwOptAutoFullscreen')?.checked,
                        autoSkipIntro: !!document.getElementById('bwOptAutoSkipIntro')?.checked,
                        autoNext: !!document.getElementById('bwOptAutoNext')?.checked,
                        playbackRate: parseFloat(document.getElementById('bwOptPlaybackRate')?.value || '1')
                      };
                      localStorage.setItem('bw_settings', JSON.stringify(next));
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
                const s = item.getAttribute('data-series');
                if (s) {
                    try { localStorage.setItem('bw_series', s); } catch(_) {}
                    document.cookie = 'bw_series=' + encodeURIComponent(s) + '; path=/';
                }
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
              const _ps = history.pushState; history.pushState = function(){ const r=_ps.apply(this,arguments); setTimeout(ensureSidebar,0); return r; };
              const _rs = history.replaceState; history.replaceState = function(){ const r=_rs.apply(this,arguments); setTimeout(ensureSidebar,0); return r; };
              window.addEventListener('popstate', ensureSidebar);
              window.addEventListener('hashchange', ensureSidebar);
              setInterval(ensureSidebar, 1500);
            }

            // Nur aktualisieren wenn nötig
            if (typeof html === 'string') {
              if (window.__bwLastHTML !== html) {
                const list = document.getElementById('bwSeriesList');
                if (list) list.innerHTML = html;
                window.__bwLastHTML = html;
              }
            }

          } catch(e) { console.error('Sidebar injection failed', e); }
        })(arguments[0]);
        """, html_concat)
        return True
    except Exception as e:
        logging.error(f"Sidebar-Injektion fehlgeschlagen: {e}")
        return False

def build_items_html(db: Dict[str, Dict[str, Any]]) -> str:
    items_html = []
    
    sorted_items = sorted(
        db.items(),
        key=lambda kv: float(kv[1].get("timestamp", 0)),
        reverse=True
    )
    
    for series_name, data in sorted_items:
        season = int(data.get("season", 1))
        episode = int(data.get("episode", 1))
        position = int(data.get("position", 0))
        intro_val = int(data.get("intro_skip", INTRO_SKIP_SECONDS))
        ts_val = float(data.get("timestamp", 0))
        safe_name = _html.escape(series_name, quote=True)
        
        items_html.append(f"""
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
    """)
    
    return "\n".join(items_html)

# === MAIN PLAYBACK LOOP ===
def play_episodes_loop(driver: webdriver.Firefox, series: str, season: int, episode: int, position: int = 0) -> None:
    global current_series, current_season, current_episode, is_playing, should_quit
    current_series, current_season, current_episode = series, season, episode
    is_playing = True

    last_save_time = 0.0
    current_ep = int(episode)

    logging.info(f"Starte Wiedergabe: {series} S{season}E{current_ep}")

    while is_playing and not should_quit:
        # Ensure browser alive
        if not is_browser_responsive(driver):
            logging.error("Browser nicht responsiv – Abbruch der Wiedergabe")
            break

        # Navigate to target ep
        if not navigate_to_episode(driver, series, season, current_ep):
            logging.error("Navigation fehlgeschlagen – Abbruch")
            break

        # Hoster öffnen / zu einem Fenster mit Video wechseln
        if not open_preferred_hoster(driver):
            logging.error("Kein Hosterfenster mit Video gefunden – Abbruch")
            break

        # Quit via localStorage
        try:
            qls = driver.execute_script("try { return localStorage.getItem('bw_quit'); } catch(e) { return null; }")
            if qls == '1':
                driver.execute_script("try { localStorage.removeItem('bw_quit'); } catch(e) {}")
                should_quit = True
                break
        except Exception:
            pass

        # Quit via cookie
        if get_cookie(driver, 'bw_quit') == '1':
            delete_cookie(driver, 'bw_quit')
            should_quit = True
            break

        driver.switch_to.default_content()
        logging.info(f"Handles: {len(driver.window_handles)} | URL: {driver.current_url}")

        # Switch to frame with video
        ok = False
        for _ in range(3):
            if switch_to_frame_with_video(driver):
                ok = True
                break
            time.sleep(1.5)
        if not ok:
            logging.error("Kein Video gefunden – Abbruch")
            break

        # Settings
        driver.switch_to.default_content()
        settings = read_settings(driver)
        auto_full = bool(settings.get("autoFullscreen", True))
        auto_skip_intro = bool(settings.get("autoSkipIntro", True))
        auto_next = settings.get("autoNext", True) is not False
        start_rate = float(settings.get("playbackRate", 1))

        # Switch again to video context for controls
        switch_to_frame_with_video(driver)
        inject_video_controls(driver, get_intro_skip_seconds(series), start_rate)

        # If we have previous position, seek near there
        if position and position > 0:
            set_video_time(driver, position)

        # Try start
        if not try_start_playback(driver):
            logging.error("Video konnte nicht gestartet werden")
            break

        if auto_full:
            enable_fullscreen(driver)

        # Auto skip intro
        if auto_skip_intro:
            intro_s = get_intro_skip_seconds(series)
            try:
                state = get_video_state(driver)
                if float(state.get("duration") or 0) > intro_s > 0:
                    set_video_time(driver, intro_s)
            except Exception:
                pass

        # Monitor loop
        ended_episode = False
        overlay_shown = False
        while is_playing and not should_quit:
            driver.switch_to.default_content()

            # Quit via localStorage
            try:
                qls = driver.execute_script("try { return localStorage.getItem('bw_quit'); } catch(e) { return null; }")
                if qls == '1':
                    driver.execute_script("try { localStorage.removeItem('bw_quit'); } catch(e) {}")
                    should_quit = True
                    break
            except Exception:
                pass

            # Quit via cookie
            if get_cookie(driver, 'bw_quit') == '1':
                delete_cookie(driver, 'bw_quit')
                should_quit = True
                break

            # Manual series selection takes precedence
            if  get_cookie(driver, 'bw_series'):
                delete_cookie(driver, 'bw_series')
                return

            try:
                lsel = driver.execute_script("try { return localStorage.getItem('bw_series'); } catch(e) { return null; }")
                if lsel:
                    driver.execute_script("try { localStorage.removeItem('bw_series'); } catch(e) {}")
                    return
            except Exception:
                pass

            # Deletion requests
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
                    html = build_items_html(load_progress())
                    driver.execute_script("if (window.__bwSetList){window.__bwSetList(arguments[0]);}", html)
                    continue
            except Exception:
                pass

            try:
                need = driver.execute_script("""
                    let v = localStorage.getItem('bw_need_reinject');
                    if (v) localStorage.removeItem('bw_need_reinject');
                    return v;
                """)
                if need:
                    inject_sidebar(driver, load_progress())
                    html = build_items_html(load_progress())
                    driver.execute_script("if (window.__bwSetList){window.__bwSetList(arguments[0]);}", html)
            except Exception:
                pass

            # Handle intro updates (from sidebar input)
            try:
                upd = driver.execute_script("""
                    let r = localStorage.getItem('bw_intro_update');
                    if (r) localStorage.removeItem('bw_intro_update');
                    return r;
                """)
                if upd:
                    data = json.loads(upd)
                    ser = data.get('series')
                    secs = data.get('seconds')
                    if ser and isinstance(secs, (int, float)):
                        set_intro_skip_seconds(ser, int(secs))
                        # Liste live pushen
                        html = build_items_html(load_progress())
                        driver.execute_script("if (window.__bwSetList){window.__bwSetList(arguments[0]);}", html)
            except Exception:
                pass

            # Stay within video frame to read state and save pos
            if not switch_to_frame_with_video(driver):
                logging.info("Video-Frame verloren – verlasse aktuelle Episode")
                break

            state = get_video_state(driver)
            duration = float(state.get("duration") or 0)
            cur = float(state.get("currentTime") or 0)
            remaining = max(0.0, duration - cur)

            # Periodic save
            now = time.time()
            if now - last_save_time >= PROGRESS_SAVE_INTERVAL:
                save_progress(series, season, current_ep, int(cur))
                last_save_time = now
                try:
                    driver.switch_to.default_content()
                    html = build_items_html(load_progress())
                    driver.execute_script("if (window.__bwSetList) { window.__bwSetList(arguments[0]); }", html)
                except Exception:
                    pass

            # Near end overlay (only visual, cancel flag via localStorage)
            if auto_next and duration > 0 and remaining <= 15 and not overlay_shown:
                try:
                    driver.execute_script(
                        """
                        (function(){
                          try{
                            const old = document.getElementById('bwAutoNextPanel'); if (old) old.remove();
                            const p = document.createElement('div');
                            p.id = 'bwAutoNextPanel';
                            Object.assign(p.style, { position:'absolute', right:'20px', top:'20px', padding:'10px 12px',
                              borderRadius:'10px', background:'rgba(2,6,23,.75)', color:'#e2e8f0', border:'1px solid rgba(255,255,255,.15)',
                              zIndex:2147483647, display:'flex', gap:'10px', alignItems:'center' });
                            p.innerHTML = `<span>Autoplay: Nächste Episode in wenigen Sekunden…</span>
                              <button id=\"bwCancelAutoplay\" style=\"padding:6px 8px;border-radius:8px;border:1px solid rgba(239,68,68,.35);background:rgba(239,68,68,.12);color:#fecaca;cursor:pointer;\">Abbrechen</button>`;
                            (document.body||document.documentElement).appendChild(p);
                            p.addEventListener('click', (ev)=>{ if (ev.target && ev.target.id==='bwCancelAutoplay') { try{ parent.localStorage.setItem('bw_cancel_autonext','1'); }catch(e){} p.remove(); } });
                          }catch(e){}
                        })();
                        """
                    )
                    overlay_shown = True
                except Exception:
                    pass

            # Ended naturally?
            if state.get("ended") or remaining <= 2:
                ended_episode = True
                break

            # If paused by user, don't interfere
            if state.get("paused"):
                time.sleep(1)
                continue

            # If stalled (not playing), nudge the *video*
            if not is_video_playing(driver):
                try:
                    # im richtigen Frame/Fenster bleiben
                    switch_to_frame_with_video(driver)

                    # 1) Direkt play() versuchen (ggf. muted setzen)
                    driver.execute_script("""
                        const v = document.querySelector('video');
                        if (v) {
                            try { v.muted = true; } catch(e) {}
                            try { v.play(); } catch(e) {}
                        }
                    """)

                    # 2) Typische Big-Play-Overlays klicken
                    driver.execute_script("""
                        (function(){
                        const sels = [
                            '.vjs-big-play-button',
                            '.jw-display', '.jw-icon-play',
                            '.plyr__control[data-plyr="play"]',
                            'button[aria-label="Play"]',
                            '.fp-ui .fp-play'
                        ];
                        for (const s of sels) {
                            const el = document.querySelector(s);
                            if (el) { el.click(); break; }
                        }
                        })();
                    """)

                    # 3) Fokussiert dem Video SPACE geben (falls Player ihn nutzt)
                    try:
                        vid = WebDriverWait(driver, 2).until(
                            EC.presence_of_element_located((By.TAG_NAME, "video"))
                        )
                        try:
                            vid.send_keys(Keys.SPACE)
                        except Exception:
                            ActionChains(driver).move_to_element(vid).click().perform()
                    except Exception:
                        pass
                except Exception:
                    pass

            time.sleep(1.5)

        # Exit fullscreen between episodes
        exit_fullscreen(driver)

        # If not ended, stop loop (user left or error)
        if not ended_episode:
            break

        # Check cancel auto-next flag
        driver.switch_to.default_content()
        try:
            cancel = driver.execute_script(
                """
                try { const v = localStorage.getItem('bw_cancel_autonext'); if (v) { localStorage.removeItem('bw_cancel_autonext'); return true; } } catch(e) {}
                return false;
                """
            )
        except Exception:
            cancel = False

        if cancel:
            logging.info("Autoplay abgebrochen – Wiedergabe beendet")
            break

        # Next episode
        current_ep += 1
        position = 0

    is_playing = False
    logging.info("Wiedergabeschleife beendet")


# === MAIN ===
def main() -> None:
    global should_quit
    logging.info("BingeWatcher startet…")
    driver: Optional[webdriver.Firefox] = None
    try:
        driver = start_browser()
        if not safe_navigate(driver, START_URL):
            raise BingeWatcherError("Startseite konnte nicht geladen werden")

        while not should_quit:
            try:
                driver.switch_to.default_content()
                db = load_progress()
                
                if not driver.execute_script("return !!document.getElementById('bingeSidebar');"):
                    inject_sidebar(driver, load_progress())

                # Quit via localStorage
                try:
                    qls = driver.execute_script("try { return localStorage.getItem('bw_quit'); } catch(e) { return null; }")
                    if qls == '1':
                        driver.execute_script("try { localStorage.removeItem('bw_quit'); } catch(e) {}")
                        should_quit = True
                        break
                except Exception:
                    pass

                # Quit via cookie
                if get_cookie(driver, 'bw_quit') == '1':
                    delete_cookie(driver, 'bw_quit')
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
                        html = build_items_html(load_progress())
                        driver.execute_script("if (window.__bwSetList){window.__bwSetList(arguments[0]);}", html)
                        continue
                except Exception:
                    pass

                try:
                    need = driver.execute_script("""
                        let v = localStorage.getItem('bw_need_reinject');
                        if (v) localStorage.removeItem('bw_need_reinject');
                        return v;
                    """)
                    if need:
                        inject_sidebar(driver, load_progress())
                        html = build_items_html(load_progress())
                        driver.execute_script("if (window.__bwSetList){window.__bwSetList(arguments[0]);}", html)
                except Exception:
                    pass

                # Handle intro updates (from sidebar input)
                try:
                    upd = driver.execute_script("""
                        let r = localStorage.getItem('bw_intro_update');
                        if (r) localStorage.removeItem('bw_intro_update');
                        return r;
                    """)
                    if upd:
                        data = json.loads(upd)
                        ser = data.get('series')
                        secs = data.get('seconds')
                        if ser and isinstance(secs, (int, float)):
                            set_intro_skip_seconds(ser, int(secs))
                            # Liste live pushen
                            html = build_items_html(load_progress())
                            driver.execute_script("if (window.__bwSetList){window.__bwSetList(arguments[0]);}", html)
                except Exception:
                    pass

                # Manual selection via cookie or localStorage
                sel = get_cookie(driver, 'bw_series')

                # Fallback: LS lesen, falls Cookie nicht ankam
                if not sel:
                    try:
                        sel = driver.execute_script("try { return localStorage.getItem('bw_series'); } catch(e) { return null; }")
                    except Exception:
                        sel = None

                if sel:
                    try:
                        sel = unquote(sel)
                    except Exception:
                        pass

                    # Aufräumen (beides)
                    delete_cookie(driver, 'bw_series')
                    try:
                        driver.execute_script("try { localStorage.removeItem('bw_series'); } catch(e) {}")
                    except Exception:
                        pass

                    sdata = db.get(sel)
                    if sdata:
                        season   = int(sdata.get('season', 1))
                        episode  = int(sdata.get('episode', 1))
                        position = int(sdata.get('position', 0))
                    else:
                        # Falls nicht im Fortschritt (sollte selten sein)
                        season, episode, position = 1, 1, 0

                    if safe_navigate(driver, f"{START_URL}serie/stream/{sel}/staffel-{season}/episode-{episode}"):
                        play_episodes_loop(driver, sel, season, episode, position)
                        safe_navigate(driver, START_URL)
                    continue

                # Auto detect if user navigated into an episode
                ser, se, ep = parse_episode_info(driver.current_url or "")
                if ser and se and ep:
                    pos = int(load_progress().get(ser, {}).get('position', 0))
                    play_episodes_loop(driver, ser, se, ep, pos)
                    safe_navigate(driver, START_URL)
                    continue

                time.sleep(0.8)
            except (InvalidSessionIdException, WebDriverException):
                should_quit = True
                break
            except Exception as e:
                logging.warning(f"Main-Loop Warnung: {e}")
                time.sleep(1.2)

    except KeyboardInterrupt:
        logging.info("Vom Benutzer unterbrochen")
    except Exception as e:
        logging.error(f"Fatal: {e}")
    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass
        logging.info("BingeWatcher beendet")


if __name__ == "__main__":
    main()