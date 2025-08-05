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
UBLOCK_ORIGIN_PATH = os.path.join(SCRIPT_DIR, 'ublock_origin.xpi')
PROGRESS_FILE = os.path.join(SCRIPT_DIR, 'progress.json')

# === PROGRESS MANAGEMENT ===
def save_progress(series, season, episode, position):
    with open(PROGRESS_FILE, 'w') as f:
        json.dump({
            "series": series,
            "season": season,
            "episode": episode,
            "position": position
        }, f)
    print(f"[✓] Progress saved at episode {episode}, position {position}s.")

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r') as f:
            return json.load(f)
    return None

# === BROWSER SETUP ===
def start_browser():
    options = webdriver.FirefoxOptions()
    if HEADLESS:
        options.add_argument("--headless")

    # Start Firefox in private mode
    options.set_preference("browser.privatebrowsing.autostart", True)
    options.set_preference("dom.disable_open_during_load", True)
    options.set_preference("dom.popup_maximum", 0)

    options.page_load_strategy = 'eager'

    service = Service(executable_path=GECKO_DRIVER_PATH)
    driver = webdriver.Firefox(service=service, options=options)

    # Install uBlock Origin (adblocker)
    driver.install_addon(UBLOCK_ORIGIN_PATH, temporary=True)
    print("[✓] uBlock Origin adblocker installed.")

    return driver

# === UTILITY FUNCTIONS ===
def exit_fullscreen(driver):
    try:
        driver.switch_to.default_content()
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.5)
    except Exception as e:
        print(f"[!] Error exiting fullscreen: {e}")

def switch_to_video_frame(driver):
    try:
        iframe = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "iframe"))
        )
        driver.switch_to.frame(iframe)
        print("[>] Switched to video iframe.")
        return True
    except Exception as e:
        print(f"[!] No iframe found: {e}")
        return False

def is_video_playing(driver):
    try:
        return driver.execute_script("""
            const video = document.querySelector('video');
            return video && !video.paused && video.readyState > 2;
        """)
    except:
        return False

def play_video(driver):
    try:
        print("[>] Starting video playback...")
        video = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.TAG_NAME, "video"))
        )
        ActionChains(driver).move_to_element(video).click().perform()
        print("[✓] Video started.")
    except Exception as e:
        print(f"[!] Could not start video: {e}")

def enable_fullscreen(driver):
    try:
        driver.execute_script("""
            const video = document.querySelector('video');
            if (video.requestFullscreen) video.requestFullscreen();
            else if (video.webkitRequestFullscreen) video.webkitRequestFullscreen();
        """)
        print("[✓] Fullscreen activated.")
    except Exception as e:
        print(f"[!] Error enabling fullscreen: {e}")

def parse_episode_info(url):
    match = re.search(r'/serie/stream/([^/]+)/staffel-(\d+)/episode-(\d+)', url)
    if match:
        return match.group(1), int(match.group(2)), int(match.group(3))
    return None, None, None

def navigate_to_episode(driver, series, season, episode):
    next_url = f"https://s.to/serie/stream/{series}/staffel-{season}/episode-{episode}"
    driver.get(next_url)
    WebDriverWait(driver, 10).until(EC.url_contains(f"episode-{episode}"))

def wait_until_video_ends(driver, buffer_seconds=3):
    WebDriverWait(driver, 15).until(
        lambda d: d.execute_script("return document.querySelector('video') && document.querySelector('video').readyState > 0;")
    )
    print("[>] Video playback in progress...")
    while True:
        remaining_time = driver.execute_script("""
            const vid = document.querySelector('video');
            return vid.duration - vid.currentTime;
        """)
        print(f"[>] Remaining: {int(remaining_time)} sec.", end="\r")
        if remaining_time <= buffer_seconds:
            print("\n[>] Episode ending soon, proceeding...")
            break
        time.sleep(2)

def skip_intro(driver, seconds):
    try:
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.querySelector('video')?.readyState > 0;")
        )
        driver.execute_script(f"document.querySelector('video').currentTime = {seconds};")
        print(f"[✓] Intro skipped to {seconds}s.")
    except Exception as e:
        print(f"[!] Error skipping intro: {e}")

def get_current_position(driver):
    return driver.execute_script("return document.querySelector('video').currentTime || 0;")

def play_episodes_loop(driver, series, season, episode, position=0):
    current_episode = episode
    while True:
        print(f"\n[▶] Playing {series.capitalize()} – Season {season}, Episode {current_episode}")

        if not switch_to_video_frame(driver):
            print("[!] Video frame not found. Exiting.")
            break

        if not is_video_playing(driver):
            play_video(driver)

        if position > 0:
            skip_intro(driver, position)
            position = 0
        else:
            skip_intro(driver, INTRO_SKIP_SECONDS)

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
                print("\n[>] Episode ending, next episode...")
                break
            time.sleep(2)

        current_episode += 1
        exit_fullscreen(driver)
        time.sleep(1)

        try:
            navigate_to_episode(driver, series, season, current_episode)
        except:
            print("[!] Next episode unavailable. Exiting.")
            break
        time.sleep(2)

# === MAIN EXECUTION ===
def main():
    progress = load_progress()
    driver = start_browser()

    try:
        if progress:
            resume = input(f"[?] Resume from {progress['series'].capitalize()} S{progress['season']}E{progress['episode']} at {progress['position']}s? (Y/n): ")
            if resume.lower() != 'n':
                navigate_to_episode(driver, progress['series'], progress['season'], progress['episode'])
                play_episodes_loop(driver, progress['series'], progress['season'], progress['episode'], progress['position'])
                return

        driver.get(START_URL)
        input("[>] Select provider and start playback, then press ENTER...")

        series, season, episode = parse_episode_info(driver.current_url)
        if not series:
            print("[!] Could not identify series. Exiting.")
            return

        play_episodes_loop(driver, series, season, episode)

    except KeyboardInterrupt:
        current_pos = get_current_position(driver)
        save_progress(series, season, episode, int(current_pos))
        print("\n[!] Interrupted by user, progress saved.")

    except Exception as e:
        print(f"[!] Critical error: {e}")

    finally:
        driver.quit()
        print("[✓] Browser closed.")

if __name__ == "__main__":
    main()