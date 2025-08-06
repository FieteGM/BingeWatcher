import os
import time
import re
import json
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# === CONFIGURATION ===
HEADLESS = False
START_URL = 'https://s.to/serie/stream/one-piece/staffel-1/episode-1'
INTRO_SKIP_SECONDS = 320

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GECKO_DRIVER_PATH = os.path.join(SCRIPT_DIR, 'geckodriver.exe')
PROGRESS_DB_FILE = os.path.join(SCRIPT_DIR, 'progress.json')

# === PROGRESS MANAGEMENT ===
def save_progress(series, season, episode, position):
    db = {}
    if os.path.exists(PROGRESS_DB_FILE):
        with open(PROGRESS_DB_FILE, 'r') as f:
            db = json.load(f)
    db[series] = {"season": season, "episode": episode, "position": position}
    with open(PROGRESS_DB_FILE, 'w') as f:
        json.dump(db, f, indent=2)
    print(f"[✓] Progress saved: {series} S{season}E{episode} @ {position}s")

def load_progress():
    if os.path.exists(PROGRESS_DB_FILE):
        with open(PROGRESS_DB_FILE, 'r') as f:
            try:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception as e:
                print("[!] Corrupt progress DB:", e)
    return {}

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

# === UTILITY FUNCTIONS ===
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
        print("[!] Video iframe not found.")
        return False

def is_video_playing(driver):
    return driver.execute_script("""
        const video = document.querySelector('video');
        return video && !video.paused && video.readyState > 2;
    """)

def play_video(driver):
    try:
        video = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.TAG_NAME, "video")))
        ActionChains(driver).move_to_element(video).click().perform()
    except Exception as e:
        print(f"[!] Could not start video: {e}")

def enable_fullscreen(driver):
    driver.execute_script("""
        const video = document.querySelector('video');
        if (video.requestFullscreen) video.requestFullscreen();
        else if (video.webkitRequestFullscreen) video.webkitRequestFullscreen();
    """)

def parse_episode_info(url):
    match = re.search(r'/serie/stream/([^/]+)/staffel-(\d+)/episode-(\d+)', url)
    if match:
        return match.group(1), int(match.group(2)), int(match.group(3))
    return None, None, None

def navigate_to_episode(driver, series, season, episode, db):
    next_url = f"https://s.to/serie/stream/{series}/staffel-{season}/episode-{episode}"
    driver.get(next_url)
    WebDriverWait(driver, 10).until(EC.url_contains(f"episode-{episode}"))
    inject_sidebar(driver, db)

def skip_intro(driver, seconds):
    WebDriverWait(driver, 15).until(lambda d: d.execute_script("return document.querySelector('video')?.readyState > 0;"))
    driver.execute_script(f"document.querySelector('video').currentTime = {seconds};")

def get_current_position(driver):
    return driver.execute_script("return document.querySelector('video').currentTime || 0;")

def play_episodes_loop(driver, series, season, episode, position=0):
    db = load_progress()
    current_episode = episode

    while True:
        print(f"\n[▶] Playing {series.capitalize()} – Season {season}, Episode {current_episode}")

        navigate_to_episode(driver, series, season, current_episode, db)
        inject_sidebar(driver, db)
        
        if not switch_to_video_frame(driver):
            break

        if not is_video_playing(driver):
            play_video(driver)

        skip_intro(driver, position or INTRO_SKIP_SECONDS)
        position = 0
        enable_fullscreen(driver)

        while True:
            remaining_time = driver.execute_script("""
                const vid = document.querySelector('video');
                return vid.duration - vid.currentTime;
            """)
            current_pos = get_current_position(driver)
            save_progress(series, season, current_episode, int(current_pos))

            print(f"[>] Remaining: {int(remaining_time)} sec.", end="\r")
            if remaining_time <= 3:
                break
            time.sleep(2)

        current_episode += 1
        exit_fullscreen(driver)
        time.sleep(1)
        db = load_progress() 

        try:
            navigate_to_episode(driver, series, season, current_episode)
        except:
            print("[!] Next episode unavailable. Exiting.")
            break
        time.sleep(2)

def inject_sidebar(driver, db):
    # 1) Baue die Listeneinträge
    entries = []
    for series, data in db.items():
        safe_series = series.replace("'", "\\'")
        entries.append(
            f'<li style="padding:8px 12px; cursor:pointer; border-bottom:1px solid #444;" '
            f'onclick="window.selectSeries(\'{safe_series}\')">'
            f'<b>{series}</b> S{data["season"]}E{data["episode"]} '
            f'<span style="color:#aaa; font-size:12px;">@ {data["position"]}s</span>'
            f'</li>'
        )
    inner_ul = "\n".join(entries)

    # 2) JavaScript zum Einfügen der Sidebar
    js = f"""
    // Alte Sidebar entfernen, falls vorhanden
    var old = document.getElementById('bingeSidebar');
    if (old) old.remove();

    // Neues Sidebar-Element anlegen
    var d = document.createElement('div');
    d.id = 'bingeSidebar';
    Object.assign(d.style, {{
        position: 'fixed',
        left: '0',
        top: '0',
        width: '260px',
        height: '100vh',
        background: '#222',
        color: '#eee',
        zIndex: '999999',
        fontFamily: 'Segoe UI, Arial, sans-serif',
        boxShadow: '2px 0 16px #000a',
        overflowY: 'auto'
    }});
    d.innerHTML = `
        <!-- Kopfzeile mit Skip und Close -->
        <div style="
            display:flex;
            justify-content:space-between;
            align-items:center;
            padding:10px 12px;
            border-bottom:1px solid #444;
        ">
            <button id="bwSkip" style="
                background:#555;
                border:none;
                color:#fff;
                padding:4px 8px;
                cursor:pointer;
                font-size:12px;
            ">Skip ▶</button>
            <span style="font-size:16px; font-weight:700;">BingeWatcher</span>
            <button id="bwQuit" style="
                background:#a33;
                border:none;
                color:#fff;
                padding:4px 8px;
                cursor:pointer;
                font-size:12px;
            ">Close ✕</button>
        </div>
        <!-- Liste der Serien -->
        <ul style="
            list-style:none;
            margin:0;
            padding:0;
        ">
            {inner_ul}
        </ul>
    `;
    document.body.appendChild(d);

    // Skip-Button: Springe ans Ende des Videos
    document.getElementById('bwSkip').onclick = function() {{
        var vid = document.querySelector('video');
        if (vid && !vid.paused) {{
            vid.currentTime = vid.duration - 1;
        }}
    }};

    // Close-Button: Fenster schließen
    document.getElementById('bwQuit').onclick = function() {{
        window.close();
    }};

    // Handler für Listeneinträge
    window.selectSeries = function(seriesName) {{
        document.cookie = "bw_series=" + encodeURIComponent(seriesName) + "; path=/";
        location.reload();
    }};
    """
    driver.execute_script(js)

def get_selected_series_cookie(driver):
    for cookie in driver.get_cookies():
        if cookie['name'] == 'bw_series':
            return cookie['value']
    return None

def delete_series_cookie(driver):
    driver.delete_cookie('bw_series')

# === MAIN EXECUTION ===
def main():
    driver = start_browser()
    db = load_progress()
    driver.get(START_URL)
    inject_sidebar(driver, db)
    time.sleep(1)

    while True:
        db = load_progress()
        inject_sidebar(driver, db)
        selected = get_selected_series_cookie(driver)
        if selected and selected in db:
            data = db[selected]
            print(f"[✓] User selected: {selected} S{data['season']}E{data['episode']} @ {data['position']}s")
            delete_series_cookie(driver)
            # Jetzt: Lade direkt die gewünschte Serie!
            navigate_to_episode(driver, selected, data['season'], data['episode'], db)
            play_episodes_loop(driver, selected, data['season'], data['episode'], data['position'])
            # Nach Abschluss: zurück zu einer "neutralen" Seite (z.B. Auswahlseite oder about:blank)
            driver.get("about:blank")
            time.sleep(1)
            continue

        input("[>] Select provider, start playback, then press ENTER...")
        series, season, episode = parse_episode_info(driver.current_url)
        if not series:
            print("[!] Could not identify series details. Exiting.")
            return
        play_episodes_loop(driver, series, season, episode)
        driver.get("about:blank")
        time.sleep(1)

if __name__ == "__main__":
    main()