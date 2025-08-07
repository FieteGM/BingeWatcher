import os
import re
import sys
import time
import json
import logging
from urllib.parse import unquote
from selenium import webdriver
from selenium.common.exceptions import NoSuchWindowException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# === CONFIGURATION ===
HEADLESS = False
START_URL = 'https://s.to/'
INTRO_SKIP_SECONDS = 320

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GECKO_DRIVER_PATH = os.path.join(SCRIPT_DIR, 'geckodriver.exe')
PROGRESS_DB_FILE = os.path.join(SCRIPT_DIR, 'progress.json')

logging.basicConfig(
    format='[BingeWatcher] %(levelname)s: %(message)s',
    level=logging.INFO
)

# === BROWSER SETUP ===
def start_browser():
    profile_path = os.path.join(SCRIPT_DIR, "user.BingeWatcher")
    options = webdriver.FirefoxOptions()
    options.set_preference("general.useragent.override", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0")
    if HEADLESS:
        options.add_argument("--headless")
    service = Service(executable_path=GECKO_DRIVER_PATH)
    options.profile = profile_path
    driver = webdriver.Firefox(service=service, options=options)
    return driver

def navigate_to_episode(driver, series, season, episode, db):
    next_url = f"https://s.to/serie/stream/{series}/staffel-{season}/episode-{episode}"
    driver.get(next_url)
    WebDriverWait(driver, 10).until(EC.url_contains(f"episode-{episode}"))
    inject_sidebar(driver, db)

def parse_episode_info(url):
    match = re.search(r'/serie/stream/([^/]+)/staffel-(\d+)/episode-(\d+)', url)
    if match:
        return match.group(1), int(match.group(2)), int(match.group(3))
    return None, None, None

def get_cookie(driver, name):
    try:
        # normaler Zugriff
        for c in driver.get_cookies():
            if c['name'] == name:
                return c['value']
    except NoSuchWindowException:
        # Context verloren → zurück zum Hauptdokument, noch mal probieren
        try:
            driver.switch_to.default_content()
            for c in driver.get_cookies():
                if c['name'] == name:
                    return c['value']
        except Exception:
            pass
    return None

# === UTILITY FUNCTIONS ===
def enable_fullscreen(driver):
    driver.execute_script("""
        const video = document.querySelector('video');
        if (video.requestFullscreen) video.requestFullscreen();
        else if (video.webkitRequestFullscreen) video.webkitRequestFullscreen();
    """)

def exit_fullscreen(driver):
    try:
        driver.switch_to.default_content()
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.5)
    except:
        pass

def switch_to_video_frame(driver):
    try:
        iframe = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
        driver.switch_to.frame(iframe)
        return True
    except:
        logging.info("[!] Video iframe not found.")
        return False

def play_video(driver):
    try:
        video = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.TAG_NAME, "video")))
        ActionChains(driver).move_to_element(video).click().perform()
    except Exception as e:
        logging.critical(f"[!] Could not start video: {e}")

def is_video_playing(driver):
    return driver.execute_script("""
        const video = document.querySelector('video');
        return video && !video.paused && video.readyState > 2;
    """)

def skip_intro(driver, seconds):
    WebDriverWait(driver, 15).until(lambda d: d.execute_script("return document.querySelector('video')?.readyState > 0;"))
    driver.execute_script(f"document.querySelector('video').currentTime = {seconds};")

def get_current_position(driver):
    return driver.execute_script("return document.querySelector('video').currentTime || 0;")

def handle_list_item_deletion(series_name):
    name = unquote(series_name).strip()
    db = load_progress()
    if name in db:
        del db[name]
        with open(PROGRESS_DB_FILE, 'w') as f:
            json.dump(db, f, indent=2)

# === PROGRESS MANAGEMENT ===
def save_progress(series, season, episode, position):
    db = {}
    if os.path.exists(PROGRESS_DB_FILE):
        with open(PROGRESS_DB_FILE, 'r') as f:
            db = json.load(f)
    db[series] = {"season": season, "episode": episode, "position": position}
    with open(PROGRESS_DB_FILE, 'w') as f:
        json.dump(db, f, indent=2)

def load_progress():
    if os.path.exists(PROGRESS_DB_FILE):
        with open(PROGRESS_DB_FILE, 'r') as f:
            try:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception as e:
                logging.critical("[!] Corrupt progress DB:", e)
    return {}

# === CORE FUNCTIONS ===
def inject_sidebar(driver, db):
    # Immer erst aus allen iframes raus
    driver.switch_to.default_content()

    # 1) List‐Items bauen
    entries = []
    for series, data in db.items():
        safe = series
        entries.append(f'''
            <li data-series="{safe}"
                data-season="{data["season"]}"
                data-episode="{data["episode"]}"
                style="display:flex;justify-content:space-between;
                    padding:8px 12px;cursor:pointer;
                    border-bottom:1px solid #444;">
            <span class="bw-select">
                <b>{series}</b> S{data["season"]}E{data["episode"]}
                <small style="color:#aaa;">@ {data["position"]}s</small>
            </span>
            <span class="bw-delete" data-series="{safe}"
                    style="color:#a33;cursor:pointer;font-weight:700;">
                ✕
            </span>
            </li>
        ''')
    inner_ul = "\n".join(entries)

    # 2) Sidebar injizieren
    js = f"""
    (function() {{
      console.log('[BingeWatcher] Rebuilding sidebar…');
      let old = document.getElementById('bingeSidebar');
      if (old) old.remove();

      // neues Sidebar
      let d = document.createElement('div');
      d.id = 'bingeSidebar';
      Object.assign(d.style, {{
        position: 'fixed',
        left: '0', top: '0',
        width: '260px', height: '100vh',
        background: '#222', color: '#eee',
        fontFamily: 'Segoe UI,Arial,sans-serif',
        boxShadow: '2px 0 16px #000a',
        overflowY: 'auto',
        pointerEvents: 'auto',
        zIndex: '2147483647'
      }});
      d.innerHTML = `
        <div style="display:flex;justify-content:space-between;
                    align-items:center;padding:10px;
                    border-bottom:1px solid #444;">
          <button id="bwSkip">Skip ▶</button>
          <span style="font-size:16px;font-weight:700;">BingeWatcher</span>
          <button id="bwQuit">Close ✕</button>
        </div>
        <ul style="list-style:none;margin:0;padding:0;">
          {inner_ul}
        </ul>
      `;
      console.log('[BingeWatcher] Sidebar injected with {len(db)} entries');
      document.documentElement.appendChild(d);

      // Event-Delegation
      d.addEventListener('click', e => {{
        const tgt = e.target;

        console.log('[BingeWatcher] Click on', tgt.id || tgt.className);

        // Skip-Button
        if (tgt.id === 'bwSkip') {{
          console.log('[BingeWatcher] Skip pressed');
          const v = document.querySelector('video');
          if (v) v.currentTime = v.duration - 1;
          return;
        }}

        // Close-Button
        if (tgt.id === 'bwQuit') {{
          console.log('[BingeWatcher] Close pressed');
          document.cookie = 'bw_quit=1; path=/';
          window.top.close();
          return;
        }}

        // Delete-Icon
        if (tgt.classList.contains('bw-delete')) {{
          console.log('[BingeWatcher] Delete pressed for series', tgt.dataset.series);
          e.stopPropagation();
          localStorage.setItem('bw_seriesToDelete', tgt.dataset.series.trim());
          location.reload();
          return;
        }}

        // Auswahl der Serie (Zeile)
        const sel = tgt.closest('.bw-select');
        if (sel) {{
          console.log('[BingeWatcher] Jump to', sel.dataset.series, 'S'+sel.dataset.season+'E'+sel.dataset.episode);
          const li = sel.parentElement;
          const s = li.dataset.series;
          const se = li.dataset.season;
          const ep = li.dataset.episode;
          location.href = `/serie/stream/${{s}}/staffel-${{se}}/episode-${{ep}}`;
        }}
      }});
    }})();
    """
    driver.execute_script(js)

def play_episodes_loop(driver, series, season, episode, position=0):
    current_episode = episode

    # 1) Einmalig Sidebar bauen
    db = load_progress()
    inject_sidebar(driver, db)

    while True:
        # Immer sicher ins Top‐Level‐Dokument
        try:
            driver.switch_to.default_content()
        except:
            pass

        # --- DELETE per JS-Flag (global in Sidebar) ---
        series_to_delete = driver.execute_script("""
            const s = localStorage.getItem('bw_seriesToDelete');
            if (s) localStorage.removeItem('bw_seriesToDelete');
            return s;
        """)
        if series_to_delete:
            handle_list_item_deletion(series_to_delete)
            logging.info(f"Deleting series '{series_to_delete}' per JS request")
            # Sidebar updaten
            db = load_progress()
            inject_sidebar(driver, db)
            continue

        # --- QUIT ---
        if get_cookie(driver, 'bw_quit') == '1':
            driver.delete_cookie('bw_quit')
            logging.info("Browser closed, exiting now.")
            driver.quit()
            sys.exit(0)

        # 2) Zur nächsten Episode navigieren
        logging.info(f"▶ Playing {series} S{season}E{current_episode}")
        navigate_to_episode(driver, series, season, current_episode, db)

        # 3) Video-Frame und Start
        if not switch_to_video_frame(driver):  
            break
        if not is_video_playing(driver):
            play_video(driver)
        skip_intro(driver, position or INTRO_SKIP_SECONDS)
        enable_fullscreen(driver)

        # 4) Playback-Monitoring
        while True:
            try:
                driver.switch_to.default_content()
            except:
                pass

            # Wechsel Serie?
            if get_cookie(driver, 'bw_series'):
                driver.delete_cookie('bw_series')
                return

            # Quit?
            if get_cookie(driver, 'bw_quit') == '1':
                driver.delete_cookie('bw_quit')
                logging.info("Browser closed, exiting now.")
                driver.quit()
                sys.exit(0)

            # Speicher-Fortschritt und log verbleibende Zeit
            if not switch_to_video_frame(driver):
                break
            remaining = driver.execute_script("""
            const v = document.querySelector('video');
            return v ? v.duration - v.currentTime : 0;
            """)
            pos = get_current_position(driver)
            save_progress(series, season, current_episode, int(pos))

            # Nur ein einziges In-Place-Update via print
            print(f"[>] Remaining {int(remaining)}s", end="\r", flush=True)
            if remaining <= 3:
                print()
                break
            time.sleep(2)

        # 5) Episode beendet → Vollbild schließen, Episode++ und weiter
        exit_fullscreen(driver)
        current_episode += 1

        # 6) Versuch, nächste Episode zu laden
        try:
            navigate_to_episode(driver, series, season, current_episode, db)
        except:
            logging.info("Next episode unavailable. Exiting loop.")
            break
        time.sleep(2)

# === MAIN ===
def main():
    logging.info("Starting Python script…")
    driver = start_browser()
    logging.info("Browser started, navigating to start URL")
    driver.get(START_URL)

    while True:
        # 1) Sicher ins Haupt‐Dokument
        try:
            driver.switch_to.default_content()
        except:
            pass

        try:
            db = load_progress()
            inject_sidebar(driver, db)
            logging.info(f"Injected sidebar with {len(db)} entries")

            while True:
                driver.switch_to.default_content()

                # --- UI-Aktionen (Delete / Select / Quit) ---
                # 2a) Delete per JS-Flag?
                series_to_delete = driver.execute_script("""
                    const s = localStorage.getItem('bw_seriesToDelete');
                    if (s) localStorage.removeItem('bw_seriesToDelete');
                    return s;
                """)
                if series_to_delete:
                    handle_list_item_deletion(series_to_delete)
                    logging.info(f"Deleted series '{series_to_delete}'")
                    # nur hier neu laden und Sidebar injizieren
                    db = load_progress()
                    inject_sidebar(driver, db)
                    continue

                # 2b) Quit?
                if get_cookie(driver, 'bw_quit') == '1':
                    driver.delete_cookie('bw_quit')
                    logging.info("Quit-Flag gefunden, beende Browser")
                    driver.quit()
                    sys.exit(0)

                # 2c) Auswahl einer Serie?
                sel = get_cookie(driver, 'bw_series')
                if sel and sel in db:
                    driver.delete_cookie('bw_series')
                    logging.info(f"User selected series '{sel}'")
                    # beim Navigieren in den Play-Loop musst du nicht die Sidebar updaten
                    play_url = f"{START_URL}serie/stream/{sel}/staffel-{db[sel]['season']}/episode-{db[sel]['episode']}"
                    driver.get(play_url)
                    play_episodes_loop(driver, sel, db[sel]['season'], db[sel]['episode'], db[sel]['position'])
                    # nach Rückkehr: Seite neu laden, Sidebar neu injizieren
                    driver.get(START_URL)
                    db = load_progress()
                    inject_sidebar(driver, db)
                    continue

                # --- Autoplay-Erkennung (wenn du schon auf einer Episode-Seite bist) ---
                ser, se, ep = parse_episode_info(driver.current_url)
                if ser:
                    logging.info(f"Detected stream page: {ser} S{se}E{ep}")
                    pos = db.get(ser, {}).get('position', 0)
                    play_episodes_loop(driver, ser, se, ep, pos)
                    # nach Ende wieder ins Menü
                    driver.get(START_URL)
                    db = load_progress()
                    inject_sidebar(driver, db)
                    continue

                # Wenn nichts zu tun, warte kurz
                time.sleep(1)
        except Exception:
            logging.exception("Uncaught error, quitting")
        finally:
            driver.quit()

if __name__ == "__main__":
    main()