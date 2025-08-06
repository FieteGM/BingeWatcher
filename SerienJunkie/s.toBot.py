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
START_URL = 'https://s.to/'
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
    current_episode = episode

    while True:
        # Ganz oben immer das aktuelle progress.json einlesen
        db = load_progress()

        # 1) Close?
        if get_cookie(driver, 'bw_quit') == '1':
            driver.delete_cookie('bw_quit')
            return

        # 2) Delete?
        if get_cookie(driver, 'bw_delete') == '1':
            name = get_cookie(driver, 'bw_delete')
            driver.delete_cookie('bw_delete')
            if name in db:
                del db[name]
                with open(PROGRESS_DB_FILE, 'w') as f:
                    json.dump(db, f, indent=2)
            return  # oder break, je nachdem was du willst

        print(f"\n[▶] Playing {series} S{season}E{current_episode}")

        # Navigiere **nur einmal** pro Folge
        navigate_to_episode(driver, series, season, current_episode, db)
        inject_sidebar(driver, db)

        if not switch_to_video_frame(driver):
            break

        if not is_video_playing(driver):
            play_video(driver)

        skip_intro(driver, position or INTRO_SKIP_SECONDS)
        enable_fullscreen(driver)

        # Haupt‐Playback‐Loop
        while True:
            if get_cookie(driver, 'bw_quit') == '1':
                driver.delete_cookie('bw_quit')
                return

            # hier auch auf delete prüfen, wenn gewünscht…

            remaining = driver.execute_script(
                "const v = document.querySelector('video'); return v.duration - v.currentTime;"
            )
            current_pos = get_current_position(driver)
            save_progress(series, season, current_episode, int(current_pos))

            print(f"[>] Remaining {int(remaining)}s", end="\r")
            if remaining <= 3:
                break
            time.sleep(2)

        # am Ende einer Episode
        exit_fullscreen(driver)
        time.sleep(1)
        current_episode += 1
        db = load_progress() 

        try:
            navigate_to_episode(driver, series, season, current_episode)
        except:
            print("[!] Next episode unavailable. Exiting.")
            break
        time.sleep(2)

def delete_series_cookie(driver):
    driver.delete_cookie('bw_series')
    
def get_cookie(driver, name):
    for c in driver.get_cookies():
        if c['name'] == name:
            return c['value']
    return None

def inject_sidebar(driver, db):
    # 1) always come out of any iframe
    driver.switch_to.default_content()

    # 2) build list‐item HTML
    entries = []
    for series, data in db.items():
        safe = series.replace("'", "\\'")
        entries.append(f'''
            <li data-series="{safe}" 
                data-season="{data["season"]}" 
                data-episode="{data["episode"]}"
                style="
                    display:flex; justify-content:space-between;
                    padding:8px 12px; cursor:pointer; border-bottom:1px solid #444;
                ">
              <span class="bw-select"><b>{series}</b> 
                 S{data["season"]}E{data["episode"]} 
                 <small style="color:#aaa;">@ {data["position"]}s</small>
              </span>
              <span class="bw-delete" data-series="{safe}"
                    style="color:#a33;cursor:pointer;font-weight:700;">
                ✕
              </span>
            </li>
        ''')
    inner_ul = "\n".join(entries)

    # 3) inject the container
    js = f"""
    (function waitForBodyAndInject() {{
      if (!document.body) {{
        return window.requestAnimationFrame(waitForBodyAndInject);
      }}
      // altes weg
      let old = document.getElementById('bingeSidebar');
      if (old) old.remove();

      // neues Sidebar
      let d = document.createElement('div');
      d.id = 'bingeSidebar';
      Object.assign(d.style, {{
        position:'fixed', left:'0', top:'0',
        width:'260px', height:'100vh',
        background:'#222', color:'#eee',
        zIndex:'999999', fontFamily:'Segoe UI,Arial,sans-serif',
        boxShadow:'2px 0 16px #000a', overflowY:'auto'
      }});
      d.innerHTML = `
        <div style="display:flex;justify-content:space-between;
                    align-items:center;padding:10px;border-bottom:1px solid #444;">
          <button id="bwSkip">Skip ▶</button>
          <span style="font-size:16px;font-weight:700;">BingeWatcher</span>
          <button id="bwQuit">Close ✕</button>
        </div>
        <ul style="list-style:none;margin:0;padding:0;">
          {inner_ul}
        </ul>
      `;
      document.body.appendChild(d);

      // Skip
      document.getElementById('bwSkip')
        .addEventListener('click', () => {{
          let v = document.querySelector('video');
          if (v) v.currentTime = v.duration - 1;
        }});

      // Close: set cookie + reload, damit Python es sieht
      document.getElementById('bwQuit')
        .addEventListener('click', () => {{
          document.cookie = "bw_quit=1; path=/";
          location.reload();
        }});

      // Auswahl: direkte Navigation
      d.querySelectorAll('.bw-select').forEach(el =>
        el.addEventListener('click', () => {{
          const li = el.parentElement;
          const series = li.dataset.series;
          const season = li.dataset.season;
          const episode = li.dataset.episode;
          location.href =
            '/serie/stream/' + series +
            '/staffel-' + season +
            '/episode-' + episode;
        }})
      );

      // Löschen: set cookie + reload
      d.querySelectorAll('.bw-delete').forEach(el =>
        el.addEventListener('click', e => {{
          let name = el.dataset.series;
          document.cookie = "bw_delete=" + encodeURIComponent(name) + "; path=/";
          location.reload();
        }})
      );
    }})();"""
    driver.execute_script(js)

def main():
    driver = start_browser()
    driver.get(START_URL)

    while True:
        db = load_progress()
        inject_sidebar(driver, db)
        time.sleep(0.5)

        # 1) Close?
        if get_cookie(driver,'bw_quit')=='1':
            driver.delete_cookie('bw_quit')
            driver.quit()
            return

        # 2) Delete?
        to_del = get_cookie(driver,'bw_delete')
        if to_del and to_del in db:
            # a) JSON updaten
            del db[to_del]
            with open(PROGRESS_DB_FILE,'w') as f:
                json.dump(db,f,indent=2)
            driver.delete_cookie('bw_delete')

            # b) Befinde ich mich noch auf der Seite der gelöschten Serie?
            cur = driver.current_url
            ser,_,_ = parse_episode_info(cur)
            if ser == to_del:
                driver.get(START_URL)
            # ansonsten: einfach in der nächsten Loop-Iteration die Sidebar neu bauen
            continue

        # 3) Auswahl?
        sel = get_cookie(driver,'bw_series')
        if sel and sel in db:
            driver.delete_cookie('bw_series')
            s,se,ep = sel, db[sel]['season'], db[sel]['episode']
            navigate_to_episode(driver,s,se,ep,db)
            play_episodes_loop(driver,s,se,ep,db[sel]['position'])
            driver.get(START_URL)
            continue

        # 4) Auto-Erkennung?
        ser,se,ep = parse_episode_info(driver.current_url)
        if ser:
            pos = db.get(ser,{}).get('position',0)
            play_episodes_loop(driver,ser,se,ep,pos)
            driver.get(START_URL)
            continue

        time.sleep(1)

if __name__ == "__main__":
    main()