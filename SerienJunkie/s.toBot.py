import os
import re
import sys
import time
import json
import logging
import threading
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
    InvalidSessionIdException
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.firefox.service import Service

# === CONFIGURATION ===
HEADLESS = False
START_URL = 'https://s.to/'
INTRO_SKIP_SECONDS = 80
MAX_RETRIES = 3
WAIT_TIMEOUT = 15
PROGRESS_SAVE_INTERVAL = 5

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GECKO_DRIVER_PATH = os.path.join(SCRIPT_DIR, 'geckodriver.exe')
PROGRESS_DB_FILE = os.path.join(SCRIPT_DIR, 'progress.json')

# Global state
current_series = None
current_season = None
current_episode = None
is_playing = False
should_quit = False

logging.basicConfig(
    format='[BingeWatcher] %(levelname)s: %(message)s',
    level=logging.INFO
)

class BingeWatcherError(Exception):
    """Custom exception for BingeWatcher errors"""
    pass

# === BROWSER SETUP ===
def start_browser():
    """Initialize and start the Firefox browser with proper error handling"""
    try:
        profile_path = os.path.join(SCRIPT_DIR, "user.BingeWatcher")
        
        # Ensure profile directory exists
        if not os.path.exists(profile_path):
            os.makedirs(profile_path, exist_ok=True)
        
        # Additional preferences for better stability
        options = webdriver.FirefoxOptions()
        options.set_preference("general.useragent.override", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0")
        # Allow scripts to close windows and suppress close/quit warnings
        options.set_preference("dom.allow_scripts_to_close_windows", True)
        options.set_preference("browser.tabs.warnOnClose", False)
        options.set_preference("browser.tabs.warnOnCloseOtherTabs", False)
        options.set_preference("browser.warnOnQuit", False)
        options.set_preference("browser.sessionstore.warnOnQuit", False)
        
        # Set the profile directory using the modern approach
        options.set_preference("profile", profile_path)
        options.profile = profile_path
        
        if HEADLESS:
            options.add_argument("--headless")
        
        # Check if geckodriver exists
        if not os.path.exists(GECKO_DRIVER_PATH):
            raise BingeWatcherError(f"Geckodriver not found at {GECKO_DRIVER_PATH}")
        
        service = Service(executable_path=GECKO_DRIVER_PATH)
        driver = webdriver.Firefox(service=service, options=options)
        
        # Set window size for consistency
        driver.set_window_size(1920, 1080)
        
        logging.info(f"Browser started with profile: {profile_path}")
        return driver
    except Exception as e:
        logging.error(f"Failed to start browser: {e}")
        raise BingeWatcherError(f"Browser startup failed: {e}")

def safe_navigate(driver, url, max_retries=MAX_RETRIES):
    """Safely navigate to a URL with retry logic"""
    for attempt in range(max_retries):
        try:
            driver.get(url)
            return True
        except WebDriverException as e:
            if attempt == max_retries - 1:
                logging.error(f"Failed to navigate to {url} after {max_retries} attempts: {e}")
                return False
            logging.warning(f"Navigation attempt {attempt + 1} failed, retrying...")
            time.sleep(2)
    return False

def navigate_to_episode(driver, series, season, episode, db):
    """Navigate to a specific episode with proper error handling"""
    url = f"{START_URL}serie/stream/{series}/staffel-{season}/episode-{episode}"
    
    if not safe_navigate(driver, url):
        raise BingeWatcherError(f"Failed to navigate to episode {series} S{season}E{episode}")
    
    # Wait for page to load and stabilize
    try:
        WebDriverWait(driver, WAIT_TIMEOUT).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        # Additional wait for dynamic content
        time.sleep(3)
    except TimeoutException:
        logging.warning("Page load timeout, continuing anyway")
    
    # Verify we're on the correct episode
    try:
        current_url = driver.current_url
        if f"episode-{episode}" not in current_url:
            logging.warning(f"Navigation may have failed, expected episode-{episode} in URL, got: {current_url}")
            # Check if we were redirected to a different page
            if "login" in current_url.lower() or "error" in current_url.lower():
                logging.error("Page redirected to login or error page")
                return False
    except Exception as e:
        logging.warning(f"Could not verify URL: {e}")
    
    # Wait a bit more for any dynamic content to load
    time.sleep(2)
    
    inject_sidebar(driver, db)
    return True

def parse_episode_info(url):
    """Parse episode information from URL with improved regex"""
    try:
        m = re.search(r'/serie/stream/([^/]+)/staffel-(\d+)/episode-(\d+)', url)
        if m:
            return (m.group(1), int(m.group(2)), int(m.group(3)))
    except (ValueError, AttributeError) as e:
        logging.warning(f"Failed to parse episode info from URL {url}: {e}")
    return (None, None, None)

def get_cookie(driver, name):
    """Get cookie value with improved error handling"""
    try:
        cookies = driver.get_cookies()
        for cookie in cookies:
            if cookie.get('name') == name:
                return cookie.get('value')
    except (NoSuchWindowException, WebDriverException) as e:
        logging.warning(f"Failed to get cookie {name}: {e}")
        try:
            driver.switch_to.default_content()
            cookies = driver.get_cookies()
            for cookie in cookies:
                if cookie.get('name') == name:
                    return cookie.get('value')
        except Exception:
            pass
    return None

def set_cookie(driver, name, value, path="/"):
    """Set cookie with error handling"""
    try:
        driver.add_cookie({'name': name, 'value': value, 'path': path})
        return True
    except Exception as e:
        logging.warning(f"Failed to set cookie {name}: {e}")
        return False

def delete_cookie(driver, name):
    """Delete cookie with error handling"""
    try:
        driver.delete_cookie(name)
        return True
    except Exception as e:
        logging.warning(f"Failed to delete cookie {name}: {e}")
        return False

# === VIDEO UTILITIES ===
def enable_fullscreen(driver):
    """Enable fullscreen mode, trying current context first, then iframes."""
    try:
        def _request_fullscreen() -> str:
            return driver.execute_script(
                """
                const v = document.querySelector('video');
                if (!v) return 'NOVIDEO';
                try {
                    if (v.requestFullscreen) { v.requestFullscreen(); return 'OK'; }
                    if (v.webkitRequestFullscreen) { v.webkitRequestFullscreen(); return 'OK'; }
                } catch (e) {
                    return 'ERR';
                }
                return 'NOSUP';
                """
            )

        # Try in current context (ideally already inside the iframe)
        result = _request_fullscreen()
        if result == 'OK':
            logging.info("Fullscreen enabled")
            return True

        # Try across iframes
        driver.switch_to.default_content()
        for iframe in driver.find_elements(By.TAG_NAME, 'iframe'):
            try:
                driver.switch_to.frame(iframe)
                result = _request_fullscreen()
                if result == 'OK':
                    logging.info("Fullscreen enabled (via iframe)")
                    return True
            except Exception:
                pass
            finally:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass

        logging.warning("No video element found for fullscreen")
        return False
    except Exception as e:
        logging.warning(f"Failed to enable fullscreen: {e}")
        return False

def exit_fullscreen(driver):
    """Exit fullscreen mode safely"""
    try:
        driver.switch_to.default_content()
        # Try multiple exit methods
        exit_script = """
        if (document.exitFullscreen) {
            document.exitFullscreen();
        } else if (document.webkitExitFullscreen) {
            document.webkitExitFullscreen();
        } else if (document.mozCancelFullScreen) {
            document.mozCancelFullScreen();
        } else if (document.msExitFullscreen) {
            document.msExitFullscreen();
        }
        """
        driver.execute_script(exit_script)
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.5)
    except Exception as e:
        logging.warning(f"Failed to exit fullscreen: {e}")

def switch_to_video_frame(driver, timeout=WAIT_TIMEOUT):
    """Switch to video iframe with improved detection and error recovery"""
    try:
        # First, ensure we're on the main page
        driver.switch_to.default_content()
        
        # Wait a moment for page to stabilize
        time.sleep(2)
        
        # Check if we're still on a valid page
        try:
            current_url = driver.current_url
            if not current_url or current_url == "about:blank":
                logging.warning("Browser lost connection or page is blank")
                return False
        except Exception:
            logging.warning("Cannot get current URL, browser may be disconnected")
            return False
        
        # Wait for iframe to be present
        try:
            iframe = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.TAG_NAME, "iframe"))
            )
        except TimeoutException:
            logging.warning("No iframe found within timeout period")
            return False
        
        # Switch to iframe
        try:
            driver.switch_to.frame(iframe)
        except Exception as e:
            logging.warning(f"Failed to switch to iframe: {e}")
            return False
        
        # Verify video element exists
        try:
            video = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.TAG_NAME, "video"))
            )
        except TimeoutException:
            logging.warning("No video element found in iframe")
            return False
        
        return True
        
    except Exception as e:
        logging.warning(f"Failed to switch to video frame: {e}")
        # Try to recover by switching back to default content
        try:
            driver.switch_to.default_content()
        except:
            pass
        return False

def play_video(driver, max_retries=3):
    """Start video playback with retry logic"""
    for attempt in range(max_retries):
        try:
            driver.switch_to.default_content()
            if not switch_to_video_frame(driver):
                return False
            
            # Wait for video to be clickable
            video = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.TAG_NAME, "video"))
            )
            
            # Try multiple click methods
            try:
                video.click()
            except ElementClickInterceptedException:
                ActionChains(driver).move_to_element(video).click().perform()
            
            # Verify video is playing
            time.sleep(1)
            if is_video_playing(driver):
                logging.info("Video started successfully")
                return True
                
        except Exception as e:
            if attempt == max_retries - 1:
                logging.error(f"Failed to start video after {max_retries} attempts: {e}")
                return False
            logging.warning(f"Video start attempt {attempt + 1} failed, retrying...")
            time.sleep(2)
    
    return False

def is_video_playing(driver):
    """Check if video is currently playing"""
    try:
        return driver.execute_script("""
            const v = document.querySelector('video');
            return v && !v.paused && v.readyState >= 2 && v.currentTime > 0;
        """)
    except Exception:
        return False

def skip_intro(driver, seconds):
    """Skip intro with proper video state checking"""
    try:
        # Wait for video to be ready
        WebDriverWait(driver, 15).until(lambda d: d.execute_script(
            "return document.querySelector('video')?.readyState >= 2;"))
        
        # Set current time
        driver.execute_script(f"document.querySelector('video').currentTime = {seconds};")
        logging.info(f"Skipped to {seconds}s")
    except Exception as e:
        logging.warning(f"Failed to skip intro: {e}")

def get_current_position(driver):
    """Get current video position with error handling"""
    try:
        return driver.execute_script("""
            const v = document.querySelector('video');
            return v ? v.currentTime : 0;
        """)
    except Exception:
        return 0

def get_video_duration(driver):
    """Get video duration with error handling"""
    try:
        return driver.execute_script("""
            const v = document.querySelector('video');
            return v ? v.duration : 0;
        """)
    except Exception:
        return 0

def get_remaining_time(driver):
    """Get remaining video time with error handling"""
    try:
        return driver.execute_script("""
            const v = document.querySelector('video');
            if (!v) return 0;
            return v.duration - v.currentTime;
        """)
    except Exception:
        return 0

def is_browser_responsive(driver):
    """Check if browser is still responsive and connected"""
    try:
        # Try to get current URL as a simple test
        current_url = driver.current_url
        return current_url is not None and current_url != "about:blank"
    except Exception:
        return False

def refresh_page_if_needed(driver, series, season, episode):
    """Refresh the page if browser seems unresponsive"""
    try:
        if not is_browser_responsive(driver):
            logging.info("Browser unresponsive, attempting to refresh page")
            driver.refresh()
            time.sleep(5)  # Wait for page to reload
            return True
    except Exception as e:
        logging.warning(f"Failed to refresh page: {e}")
    return False

# === PROGRESS MANAGEMENT ===
def save_progress(series, season, episode, position):
    """Save progress with atomic write and backup"""
    try:
        db = load_progress()
        db[series] = {
            "season": season, 
            "episode": episode, 
            "position": position,
            "timestamp": time.time()
        }
        
        # Create backup
        backup_file = PROGRESS_DB_FILE + '.backup'
        if os.path.exists(PROGRESS_DB_FILE):
            import shutil
            shutil.copy2(PROGRESS_DB_FILE, backup_file)
        
        # Write new data
        with open(PROGRESS_DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
        
        return True
    except Exception as e:
        logging.error(f"Failed to save progress: {e}")
        return False

def load_progress():
    """Load progress with error recovery"""
    try:
        if os.path.exists(PROGRESS_DB_FILE):
            with open(PROGRESS_DB_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
                else:
                    logging.warning("Progress file is not a dictionary, using empty dict")
        return {}
    except json.JSONDecodeError as e:
        logging.error(f"Corrupt progress file: {e}")
        # Try to restore from backup
        backup_file = PROGRESS_DB_FILE + '.backup'
        if os.path.exists(backup_file):
            try:
                with open(backup_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        logging.info("Restored progress from backup")
                        return data
            except Exception:
                pass
        return {}
    except Exception as e:
        logging.error(f"Failed to load progress: {e}")
        return {}

def handle_list_item_deletion(name):
    """Handle series deletion from progress"""
    try:
        db = load_progress()
        if name in db:
            del db[name]
            with open(PROGRESS_DB_FILE, 'w', encoding='utf-8') as f:
                json.dump(db, f, indent=2, ensure_ascii=False)
            logging.info(f"Deleted series: {name}")
            return True
    except Exception as e:
        logging.error(f"Failed to delete series {name}: {e}")
    return False

# === SIDEBAR MANAGEMENT ===
def inject_sidebar(driver, db):
    """Inject the sidebar with futuristic Next UI design"""
    try:
        driver.switch_to.default_content()
        
        # Create items HTML with modern design
        items = []
        for series_name, data in db.items():
            try:
                # Calculate progress percentage
                duration = 1200
                position = data.get('position', 0)
                progress_percent = min((position / duration) * 100, 100)
                
                items.append(f'''
                    <div class="bw-series-item" data-series="{series_name}" data-season="{data.get('season', 1)}" data-episode="{data.get('episode', 1)}"
                         style="margin: 8px; padding: 16px; background: linear-gradient(135deg, rgba(255,255,255,0.05) 0%, rgba(255,255,255,0.02) 100%);
                                border: 1px solid rgba(255,255,255,0.1); border-radius: 12px; cursor: pointer;
                                transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
                                backdrop-filter: blur(10px); position: relative; overflow: hidden;">
                        
                        <!-- Glow effect -->
                        <div style="position: absolute; top: 0; left: 0; right: 0; height: 1px; 
                                   background: linear-gradient(90deg, transparent, rgba(59, 130, 246, 0.5), transparent);"></div>
                        
                        <!-- Progress bar -->
                        <div style="position: absolute; bottom: 0; left: 0; height: 2px; width: {progress_percent}%; 
                                   background: linear-gradient(90deg, #3b82f6, #8b5cf6); border-radius: 0 0 12px 12px;"></div>
                        
                        <div class="bw-select" style="display: flex; justify-content: space-between; align-items: center;">
                            <div style="flex: 1;">
                                <div style="font-weight: 600; font-size: 14px; color: #f8fafc; margin-bottom: 4px; 
                                           text-shadow: 0 1px 2px rgba(0,0,0,0.3);">{series_name}</div>
                                <div style="font-size: 12px; color: #94a3b8; display: flex; align-items: center; gap: 8px;">
                                    <span style="background: rgba(59, 130, 246, 0.2); padding: 2px 6px; border-radius: 4px; 
                                               border: 1px solid rgba(59, 130, 246, 0.3);">S{data.get('season', 1)}E{data.get('episode', 1)}</span>
                                    <span style="opacity: 0.7;">{data.get('position', 0)}s</span>
                                </div>
                            </div>
                            <div class="bw-delete" data-series="{series_name}" 
                                 style="color: #ef4444; cursor: pointer; padding: 6px; border-radius: 6px;
                                        background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.2);
                                        transition: all 0.2s; font-size: 12px; font-weight: 500;">✕</div>
                        </div>
                    </div>
                ''')
            except Exception as e:
                logging.warning(f"Failed to create item for {series_name}: {e}")
        
        items_html = "\n".join(items)
        
        # Modern JavaScript with Next UI styling
        js = f"""
        (function() {{
            try {{
                // Remove existing sidebar
                let old = document.getElementById('bingeSidebar');
                if (old) old.remove();
                
                // Create new sidebar
                let d = document.createElement('div');
                d.id = 'bingeSidebar';
                Object.assign(d.style, {{
                    position: 'fixed',
                    left: 0,
                    top: 0,
                    width: '320px',
                    height: '100vh',
                    background: 'linear-gradient(180deg, rgba(15, 23, 42, 0.95) 0%, rgba(30, 41, 59, 0.95) 100%)',
                    color: '#f8fafc',
                    overflowY: 'auto',
                    zIndex: 2147483647,
                    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
                    boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.25), 0 0 0 1px rgba(255, 255, 255, 0.05)',
                    borderRight: '1px solid rgba(255, 255, 255, 0.1)',
                    backdropFilter: 'blur(20px)',
                    WebkitBackdropFilter: 'blur(20px)'
                }});
                
                d.innerHTML = `
                    <!-- Header -->
                    <div style="padding: 20px; border-bottom: 1px solid rgba(255, 255, 255, 0.1); 
                               background: linear-gradient(135deg, rgba(59, 130, 246, 0.1) 0%, rgba(139, 92, 246, 0.1) 100%);">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <div style="width: 8px; height: 8px; background: linear-gradient(135deg, #3b82f6, #8b5cf6); 
                                           border-radius: 50%; animation: pulse 2s infinite;"></div>
                                <span style="font-weight: 700; font-size: 18px; background: linear-gradient(135deg, #3b82f6, #8b5cf6); 
                                           -webkit-background-clip: text; -webkit-text-fill-color: transparent; 
                                           background-clip: text;">BingeWatcher</span>
                            </div>
                            <button id="bwQuit" style="background: rgba(239, 68, 68, 0.1); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.2);
                                                      padding: 8px 12px; border-radius: 8px; cursor: pointer; font-size: 12px; font-weight: 500;
                                                      transition: all 0.2s; backdrop-filter: blur(10px);">Close</button>
                        </div>
                        
                        <!-- Control buttons -->
                        <div style="display: flex; gap: 8px;">
                            <button id="bwSkip" style="flex: 1; background: linear-gradient(135deg, #10b981, #059669); 
                                                      color: white; border: none; padding: 10px 16px; border-radius: 8px; 
                                                      cursor: pointer; font-weight: 600; font-size: 13px; transition: all 0.2s;
                                                      box-shadow: 0 4px 6px -1px rgba(16, 185, 129, 0.2);">Skip to End</button>
                        </div>
                    </div>
                    
                    <!-- Series list -->
                    <div style="padding: 16px;">
                        <div style="font-size: 12px; color: #94a3b8; margin-bottom: 12px; text-transform: uppercase; 
                                   letter-spacing: 0.5px; font-weight: 600;">Your Series</div>
                        <div style="display: flex; flex-direction: column; gap: 4px;">{items_html}</div>
                    </div>
                    
                    <!-- CSS Animations -->
                    <style>
                        @keyframes pulse {{
                            0%, 100% {{ opacity: 1; }}
                            50% {{ opacity: 0.5; }}
                        }}
                        
                        #bingeSidebar::-webkit-scrollbar {{
                            width: 6px;
                        }}
                        
                        #bingeSidebar::-webkit-scrollbar-track {{
                            background: rgba(255, 255, 255, 0.05);
                        }}
                        
                        #bingeSidebar::-webkit-scrollbar-thumb {{
                            background: rgba(255, 255, 255, 0.2);
                            border-radius: 3px;
                        }}
                        
                        #bingeSidebar::-webkit-scrollbar-thumb:hover {{
                            background: rgba(255, 255, 255, 0.3);
                        }}
                    </style>
                `;
                
                document.documentElement.appendChild(d);
                
                // Event handling with improved interactions
                d.addEventListener('click', function(e) {{
                    try {{
                        if (e.target.id === 'bwSkip') {{
                            let v = document.querySelector('video');
                            if (v && v.duration) {{
                                v.currentTime = v.duration - 1;
                                console.log('Skipped to end');
                            }}
                            return;
                        }}
                        
                        if (e.target.id === 'bwQuit') {{
                            document.cookie = 'bw_quit=1;path=/;max-age=3600';
                            try {{ window.top.close(); }} catch (e) {{}}
                            setTimeout(() => {{ location.href = 'about:blank'; }}, 100);
                            return;
                        }}
                        
                        if (e.target.classList.contains('bw-delete')) {{
                            let series = e.target.dataset.series;
                            if (series) {{
                                localStorage.setItem('bw_seriesToDelete', series);
                                location.reload();
                            }}
                            return;
                        }}
                        
                        let sel = e.target.closest('.bw-select');
                        if (sel) {{
                            let li = sel.parentElement;
                            if (li.dataset.series && li.dataset.season && li.dataset.episode) {{
                                let url = `/serie/stream/${{li.dataset.series}}/staffel-${{li.dataset.season}}/episode-${{li.dataset.episode}}`;
                                location.href = url;
                            }}
                        }}
                    }} catch (err) {{
                        console.error('Sidebar click error:', err);
                    }}
                }});
                
                // Enhanced hover effects
                d.addEventListener('mouseover', function(e) {{
                    if (e.target.classList.contains('bw-series-item')) {{
                        e.target.style.transform = 'translateY(-2px)';
                        e.target.style.boxShadow = '0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04)';
                        e.target.style.borderColor = 'rgba(59, 130, 246, 0.3)';
                    }}
                    if (e.target.classList.contains('bw-delete')) {{
                        e.target.style.background = 'rgba(239, 68, 68, 0.2)';
                        e.target.style.borderColor = 'rgba(239, 68, 68, 0.4)';
                    }}
                }});
                
                d.addEventListener('mouseout', function(e) {{
                    if (e.target.classList.contains('bw-series-item')) {{
                        e.target.style.transform = 'translateY(0)';
                        e.target.style.boxShadow = 'none';
                        e.target.style.borderColor = 'rgba(255, 255, 255, 0.1)';
                    }}
                    if (e.target.classList.contains('bw-delete')) {{
                        e.target.style.background = 'rgba(239, 68, 68, 0.1)';
                        e.target.style.borderColor = 'rgba(239, 68, 68, 0.2)';
                    }}
                }});
                
            }} catch (err) {{
                console.error('Sidebar injection error:', err);
            }}
        }})();
        """
        
        driver.execute_script(js)
        return True
        
    except Exception as e:
        logging.error(f"Failed to inject sidebar: {e}")
        return False

# === PLAYBACK LOOP ===
def play_episodes_loop(driver, series, season, episode, position=0):
    """Main playback loop with improved error handling and state management"""
    global current_series, current_season, current_episode, is_playing, should_quit
    
    current_series, current_season, current_episode = series, season, episode
    is_playing = True
    current = episode
    
    db = load_progress()
    last_save_time = time.time()
    
    logging.info(f"Starting playback loop for {series} S{season}E{episode}")
    
    try:
        while is_playing and not should_quit:
            driver.switch_to.default_content()
            
            # Check for manual navigation
            try:
                ser, se, ep = parse_episode_info(driver.current_url)
                if ser and (ser != current_series or se != current_season or ep != current):
                    logging.info(f"Manual navigation detected: {ser} S{se}E{ep}")
                    current_series, current_season, current_episode = ser, se, ep
                    current = ep
                    position = load_progress().get(ser, {}).get('position', 0)
                    inject_sidebar(driver, load_progress())
            except Exception as e:
                logging.warning(f"Failed to check navigation: {e}")
            
            # Check quit signal
            if get_cookie(driver, 'bw_quit') == '1':
                delete_cookie(driver, 'bw_quit')
                logging.info("Quit signal received, closing browser")
                try:
                    driver.quit()
                except Exception:
                    pass
                sys.exit(0)
            
            # Check for series deletion
            try:
                tod = driver.execute_script("""
                    let s = localStorage.getItem('bw_seriesToDelete');
                    if (s) localStorage.removeItem('bw_seriesToDelete');
                    return s;
                """)
                if tod:
                    handle_list_item_deletion(tod)
                    inject_sidebar(driver, load_progress())
                    continue
            except Exception as e:
                logging.warning(f"Failed to check for deletion: {e}")
            
            # Load episode
            logging.info(f"▶ Playing {current_series} S{current_season}E{current}")
            
            # Check if browser is responsive before navigation
            if not is_browser_responsive(driver):
                logging.warning("Browser not responsive, attempting recovery")
                if not refresh_page_if_needed(driver, current_series, current_season, current):
                    logging.error("Failed to recover browser, ending playback")
                    break
            
            try:
                if not navigate_to_episode(driver, current_series, current_season, current, db):
                    logging.error("Navigation failed, ending playback")
                    break
            except Exception as e:
                logging.error(f"Failed to navigate to episode: {e}")
                break
            
            # Check for video iframe with retry
            iframe_found = False
            for attempt in range(3):
                if switch_to_video_frame(driver):
                    iframe_found = True
                    break
                else:
                    logging.warning(f"Iframe not found, attempt {attempt + 1}/3")
                    time.sleep(2)
            
            if not iframe_found:
                logging.info("No video iframe found after retries, ending playback")
                break
            
            # Start video
            if not is_video_playing(driver):
                if not play_video(driver):
                    logging.error("Failed to start video")
                    break
            
            time.sleep(1)
            
            # Enable fullscreen
            enable_fullscreen(driver)
            
            # Skip intro if video is long enough
            try:
                duration = get_video_duration(driver)
                if duration > INTRO_SKIP_SECONDS:
                    skip_intro(driver, INTRO_SKIP_SECONDS)
            except Exception as e:
                logging.warning(f"Failed to skip intro: {e}")
            
            # Playback monitoring loop
            playback_start_time = time.time()
            not_playing_streak = 0
            while is_playing and not should_quit:
                try:
                    driver.switch_to.default_content()
                    
                    # Check for navigation changes during playback
                    ser2, se2, ep2 = parse_episode_info(driver.current_url)
                    if ser2 and (ser2 != current_series or se2 != current_season or ep2 != current):
                        logging.info(f"Navigation change during playback: {ser2} S{se2}E{ep2}")
                        break
                    
                    # Check quit signals
                    if get_cookie(driver, 'bw_quit') == '1':
                        delete_cookie(driver, 'bw_quit')
                        logging.info("Quit signal received during playback, closing browser")
                        try:
                            driver.quit()
                        except Exception:
                            pass
                        sys.exit(0)
                    
                    if get_cookie(driver, 'bw_series'):
                        delete_cookie(driver, 'bw_series')
                        return
                    
                    # Switch to video frame
                    if not switch_to_video_frame(driver):
                        logging.info("Lost video frame during playback")
                        break

                    # Read detailed video state
                    state = driver.execute_script(
                        """
                        const v = document.querySelector('video');
                        if (!v) return { paused: true, ended: false, currentTime: 0, duration: 0, readyState: 0 };
                        return {
                            paused: !!v.paused,
                            ended: !!v.ended,
                            currentTime: v.currentTime || 0,
                            duration: v.duration || 0,
                            readyState: v.readyState || 0
                        };
                        """
                    )

                    rem = max(0, (state.get('duration') or 0) - (state.get('currentTime') or 0))

                    # Get current position and save progress
                    pos = int(state.get('currentTime') or 0)
                    current_time = time.time()
                    
                    if current_time - last_save_time >= PROGRESS_SAVE_INTERVAL:
                        save_progress(current_series, current_season, current, pos)
                        last_save_time = current_time
                    
                    # Display progress
                    print(f"[>] {current_series} S{current_season}E{current} - Remaining: {int(rem)}s", end="\r", flush=True)
                    
                    # Check if episode is ending
                    if rem <= 3 or state.get('ended'):
                        print()
                        break
                    
                    # If user paused the video, do NOT auto-advance
                    if state.get('paused'):
                        not_playing_streak = 0
                        time.sleep(1)
                        continue

                    # If not paused but also not really playing (e.g., buffering), tolerate for a few cycles
                    if not is_video_playing(driver):
                        not_playing_streak += 1
                        if not_playing_streak >= 10:  # ~20s given sleep(2) below
                            logging.warning("Video not progressing, giving up this episode")
                            break
                        time.sleep(2)
                        continue
                    
                    time.sleep(2)
                    
                except Exception as e:
                    logging.warning(f"Playback monitoring error: {e}")
                    time.sleep(1)
            
            # Exit fullscreen and prepare for next episode
            exit_fullscreen(driver)
            current += 1
            
            # Check if next episode exists
            try:
                navigate_to_episode(driver, current_series, current_season, current, db)
                if not switch_to_video_frame(driver):
                    logging.info("No video for next episode, ending series")
                    break
                driver.switch_to.default_content()
            except Exception as e:
                logging.info(f"Next episode not available: {e}")
                break
            
            time.sleep(1)
    
    except Exception as e:
        logging.error(f"Playback loop error: {e}")
    finally:
        is_playing = False
        logging.info("Playback loop ended")

# === MAIN FUNCTION ===
def main():
    """Main function with comprehensive error handling"""
    global should_quit
    
    logging.info("Starting BingeWatcher")
    
    driver = None
    try:
        # Start browser
        driver = start_browser()
        logging.info("Browser started successfully")
        
        # Navigate to start page
        if not safe_navigate(driver, START_URL):
            raise BingeWatcherError("Failed to navigate to start page")
        
        # Main application loop
        while not should_quit:
            try:
                driver.switch_to.default_content()
                
                # Load and display progress
                db = load_progress()
                if not inject_sidebar(driver, db):
                    logging.warning("Failed to inject sidebar")
                
                logging.info(f"Available series: {list(db.keys())}")
                
                # Event handling loop
                while not should_quit:
                    try:
                        driver.switch_to.default_content()
                        
                        # Check quit signal
                        if get_cookie(driver, 'bw_quit') == '1':
                            delete_cookie(driver, 'bw_quit')
                            logging.info("Quit signal received")
                            should_quit = True
                            raise SystemExit
                        
                        # Check for series deletion
                        tod = driver.execute_script("""
                            let s = localStorage.getItem('bw_seriesToDelete');
                            if (s) localStorage.removeItem('bw_seriesToDelete');
                            return s;
                        """)
                        if tod:
                            handle_list_item_deletion(tod)
                            inject_sidebar(driver, load_progress())
                            continue
                        
                        # Check for series selection
                        sel = get_cookie(driver, 'bw_series')
                        if sel and sel in db:
                            delete_cookie(driver, 'bw_series')
                            logging.info(f"User selected: {sel}")
                            
                            series_data = db[sel]
                            url = f"{START_URL}serie/stream/{sel}/staffel-{series_data['season']}/episode-{series_data['episode']}"
                            
                            if safe_navigate(driver, url):
                                play_episodes_loop(driver, sel, series_data['season'], 
                                                 series_data['episode'], series_data.get('position', 0))
                                safe_navigate(driver, START_URL)
                            break
                        
                        # Auto-detect episode
                        ser, se, ep = parse_episode_info(driver.current_url)
                        if ser:
                            logging.info(f"Auto-detected: {ser} S{se}E{ep}")
                            play_episodes_loop(driver, ser, se, ep,
                                             load_progress().get(ser, {}).get('position', 0))
                            safe_navigate(driver, START_URL)
                            break
                        
                        time.sleep(1)
                        
                    except (InvalidSessionIdException, WebDriverException) as e:
                        logging.info("Browser session ended; exiting main loop")
                        should_quit = True
                        raise SystemExit
                    except Exception as e:
                        logging.warning(f"Event handling error: {e}")
                        time.sleep(2)
                
            except SystemExit:
                break
            except (InvalidSessionIdException, WebDriverException):
                logging.info("Browser session ended; stopping")
                break
            except Exception as e:
                logging.error(f"Main loop error: {e}")
                time.sleep(5)
    
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    except Exception as e:
        logging.error(f"Fatal error: {e}")
    finally:
        try:
            if driver:
                driver.quit()
                logging.info("Browser closed")
        except Exception:
            pass
        logging.info("BingeWatcher stopped")

if __name__ == "__main__":
    main()