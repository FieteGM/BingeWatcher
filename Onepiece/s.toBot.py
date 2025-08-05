import os
import sys
import time
import re
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# === KONFIGURATION ===

PROFILE_PATH = r'C:\Users\Ulrike N5\AppData\Roaming\Mozilla\Firefox\Profiles\t4rbj1mq.onepiece'
HEADLESS = False
START_URL = 'https://s.to/serie/stream/one-piece/staffel-1/episode-1'

INTRO_SKIP_SECONDS = 320

# automatisch im gleichen Ordner wie dieses Skript
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GECKO_DRIVER_PATH = os.path.join(SCRIPT_DIR, 'geckodriver.exe')

# === FUNKTIONEN ===

def starte_browser():
    options = webdriver.FirefoxOptions()
    if HEADLESS:
        options.add_argument("--headless")
    # Profil laden
    options.profile = webdriver.FirefoxProfile(PROFILE_PATH)
    # page_load_strategy auf 'eager' für schnellere Navigation
    options.set_capability("pageLoadStrategy", "eager")
    service = Service(GECKO_DRIVER_PATH)
    return webdriver.Firefox(service=service, options=options)

# — rest unverändert —
def verlasse_vollbild(driver):
    try:
        driver.switch_to.default_content()
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(1)
    except:
        pass

def switch_to_video_frame(driver):
    try:
        iframe = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "iframe"))
        )
        driver.switch_to.frame(iframe)
        print("🧭 In Videoplayer-Frame gewechselt.")
        return True
    except:
        print("❌ Kein iframe mit Video gefunden.")
        return False

def video_laeuft(driver):
    try:
        return driver.execute_script("return !!(document.querySelector('video') && !document.querySelector('video').paused)")
    except:
        return False

def starte_video(driver):
    try:
        print("🎯 Versuche, Video zu starten...")
        video = WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.TAG_NAME, "video"))
        )
        ActionChains(driver).move_to_element(video).click().perform()
        print("✅ Video per Klick gestartet.")
    except:
        print("⚠️ Klick auf Video fehlgeschlagen.")

def aktiviere_vollbild(driver):
    try:
        print("🖥️ Versuche JavaScript-Vollbild...")
        js = """
            const video = document.querySelector('video');
            if (video?.requestFullscreen) { video.requestFullscreen(); return true; }
            if (video?.webkitRequestFullscreen) { video.webkitRequestFullscreen(); return true; }
            return false;
        """
        success = driver.execute_script(js)
        if success:
            print("✅ Vollbild per JS aktiviert.")
        else:
            print("❌ JS-Vollbild nicht unterstützt.")
    except Exception as e:
        print("❌ Fehler beim Vollbild per JS:", e)

def parse_url_info(driver):
    try:
        url = driver.current_url
        print(f"🌐 Aktuelle URL: {url}")
        match = re.search(r'/serie/stream/([^/]+)/staffel-(\d+)/episode-(\d+)', url)
        if match:
            return match.group(1), int(match.group(2)), int(match.group(3))
    except:
        pass
    return None, None, None

def springe_zu_folge(driver, serienname, staffelnummer, ziel_folgenummer):
    try:
        ziel_url = f"https://s.to/serie/stream/{serienname}/staffel-{staffelnummer}/episode-{ziel_folgenummer}"
        print(f"➡️ Nächste Folge: {ziel_url}")
        driver.switch_to.default_content()
        driver.get(ziel_url)
        WebDriverWait(driver, 10).until(lambda d: f"episode-{ziel_folgenummer}" in d.current_url)
        return True
    except:
        return False

def warte_bis_video_fast_zu_ende(driver):
    try:
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return !!document.querySelector('video') && document.querySelector('video').readyState > 0")
        )
        print("🎥 Video erkannt – Laufzeit wird überwacht...")

        while True:
            position = driver.execute_script("return document.querySelector('video')?.currentTime || 0")
            duration = driver.execute_script("return document.querySelector('video')?.duration || 0")
            remaining = duration - position

            print(f"⏳ Noch {int(remaining)} Sek. | {int(position)} / {int(duration)}", end='\r')

            if remaining <= 3:
                print("\n🛑 Video fast zu Ende – springe weiter.")
                break
            time.sleep(2)
    except Exception as e:
        print("❌ Fehler beim Überwachen:", e)
        time.sleep(60)

def folgenschleife(driver, serienname, staffelnummer, startfolge):
    aktuelle = startfolge
    while True:
        print(f"\n▶️ {serienname} – Staffel {staffelnummer}, Folge {aktuelle}")

        if switch_to_video_frame(driver):
            if not video_laeuft(driver):
                starte_video(driver)
            
            skip_intro(driver, INTRO_SKIP_SECONDS)
            aktiviere_vollbild(driver)
            warte_bis_video_fast_zu_ende(driver)
        else:
            print("❌ Kein Video gefunden – Abbruch.")
            break

        aktuelle += 1
        verlasse_vollbild(driver)
        time.sleep(2)
        if not springe_zu_folge(driver, serienname, staffelnummer, aktuelle):
            print("❌ Keine weitere Folge gefunden.")
            break
        time.sleep(3)

def main():
    try:
        driver = starte_browser()
        driver.get(START_URL)
        input("🎬 Starte Folge 1 selbst (Hoster wählen, evtl. Play klicken) und drücke ENTER...")

        serienname, staffelnummer, folgennummer = parse_url_info(driver)
        if not serienname or not staffelnummer or not folgennummer:
            print("❌ Serieninfo konnte nicht erkannt werden.")
            driver.quit()
            return

        folgenschleife(driver, serienname, staffelnummer, folgennummer)
        driver.quit()
    except Exception as e:
        print("❌ Schwerer Fehler:", e)

if __name__ == "__main__":
    main()
