import base64
import requests
from bs4 import BeautifulSoup
from bs4 import Comment
import logging
import json
import google.generativeai as genai
import os
import time
import subprocess
import websocket
import threading
import queue
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Flask app and logging
app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configure Gemini API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY not found in environment variables")
genai.configure(api_key=GEMINI_API_KEY)

class BrowserController:
    def __init__(self, browser_type='chrome', start_port=9222, max_attempts=5):
        """Initialize the BrowserController with browser type and port settings."""
        self.browser_type = browser_type.lower()
        if self.browser_type not in ['chrome', 'firefox']:
            raise ValueError("Only Chrome and Firefox are supported")
        self.start_port = start_port
        self.max_attempts = max_attempts
        self.port = None
        self.process = None
        self.ws = None
        self.response_queue = queue.Queue()
        self.event_queue = queue.Queue()
        self.lock = threading.Lock()
        self.command_id = 1
        self.browser_path = (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe" if self.browser_type == 'chrome'
            else r"C:\Program Files\Mozilla Firefox\firefox.exe"
        )
        self.current_url = None

    def start_browser(self):
        """Start the browser with remote debugging enabled."""
        for attempt in range(self.max_attempts):
            port = self.start_port + attempt
            if self.browser_type == 'chrome':
                cmd = [
                    self.browser_path,
                    f'--remote-debugging-port={port}',
                    f'--user-data-dir=C:\\Temp\\chrome_debug_{port}',
                    '--no-first-run',
                    '--no-default-browser-check',
                    '--remote-allow-origins=*'
                ]
            else:  # firefox
                cmd = [
                    self.browser_path,
                    f'--remote-debugging-port={port}',
                    f'--profile=C:\\Temp\\firefox_debug_{port}'
                ]
            try:
                self.process = subprocess.Popen(cmd)
                time.sleep(3)  # Wait for browser to start
                response = requests.get(f'http://localhost:{port}/json/list', timeout=5)
                if response.status_code == 200:
                    self.port = port
                    logger.info(f"{self.browser_type.capitalize()} started on port {port}")
                    return
            except (FileNotFoundError, requests.RequestException):
                if self.process:
                    self.process.kill()
                time.sleep(1)
        raise Exception(f"Failed to start {self.browser_type} on any port after {self.max_attempts} attempts")

    def connect(self):
        """Connect to the browser's WebSocket debugging endpoint."""
        response = requests.get(f'http://localhost:{self.port}/json/list')
        pages = response.json()
        self.ws_url = pages[0]['webSocketDebuggerUrl']
        self.ws = websocket.WebSocket()
        self.ws.connect(self.ws_url)
        threading.Thread(target=self.message_handler, daemon=True).start()

    def message_handler(self):
        """Handle incoming WebSocket messages from the browser."""
        while True:
            try:
                message = self.ws.recv()
                if not message:
                    logger.info("WebSocket connection closed")
                    break
                data = json.loads(message)
                if 'id' in data:
                    self.response_queue.put(data)
                elif 'method' in data:
                    self.event_queue.put(data)
            except Exception as e:
                logger.error(f"Error in message handler: {e}")
                break

    def send_command(self, method, params):
        """Send a command to the browser and return the response."""
        with self.lock:
            command_id = self.command_id
            self.command_id += 1
            command = {'id': command_id, 'method': method, 'params': params}
            self.ws.send(json.dumps(command))
            while True:
                response = self.response_queue.get()
                if response['id'] == command_id:
                    if 'error' in response:
                        raise Exception(f"Browser error: {response['error'].get('message', str(response['error']))}")
                    return response.get('result', {})

    def navigate(self, url):
        """Navigate to a specified URL and wait for the page to load."""
        self.send_command('Page.enable', {})
        self.send_command('Page.navigate', {'url': url})
        self.current_url = url
        
        # Wait for navigation to complete
        timeout = 30  # seconds
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                event = self.event_queue.get(timeout=1)
                if event['method'] == 'Page.loadEventFired':
                    break
            except queue.Empty:
                continue
        
        # Wait for dynamic content to load
        time.sleep(3)
        
        # Check if we're on YouTube and wait for specific elements
        if "youtube.com" in url:
            self.wait_for_youtube_elements()

    def wait_for_youtube_elements(self):
        """Wait for YouTube-specific elements to load."""
        selectors = [
            "input#search", 
            "ytd-video-renderer", 
            "#content"
        ]
        
        for selector in selectors:
            try:
                self.wait_for_selector(selector, timeout=10)
                logger.info(f"Found YouTube element: {selector}")
            except Exception as e:
                logger.warning(f"Could not find YouTube element {selector}: {e}")

    def execute_script(self, script):
        """Execute JavaScript in the browser and return the result."""
        result = self.send_command('Runtime.evaluate', {
            'expression': script,
            'returnByValue': True,
            'awaitPromise': True
        })
        if 'result' in result:
            if 'value' in result['result']:
                return result['result']['value']
            elif 'description' in result['result']:
                return result['result']['description']
        return None

    def get_current_html(self):
        """Get the current page's HTML content."""
        result = self.send_command('Runtime.evaluate', {'expression': 'document.documentElement.outerHTML'})
        if 'result' in result and 'value' in result['result']:
            html = result['result']['value']
            # Clean the HTML using BeautifulSoup
            soup = BeautifulSoup(html, 'html.parser')
            for tag in soup(['script', 'style', 'noscript', 'meta', 'link', 'svg', 'img']):
                tag.decompose()
            for comment in soup.find_all(text=lambda text: isinstance(text, Comment)):
                comment.extract()
            return str(soup)
        return None

    def wait_for_selector(self, selector, timeout=20):
        """Wait for a visible element matching the selector to appear."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            result = self.execute_script(f'''
                try {{
                    var elements = document.querySelectorAll("{selector}");
                    for (var i = 0; i < elements.length; i++) {{
                        var style = window.getComputedStyle(elements[i]);
                        if (style.display !== "none" && style.visibility !== "hidden") {{
                            return true;
                        }}
                    }}
                    return false;
                }} catch (e) {{
                    console.error("Error in wait_for_selector:", e);
                    return false;
                }}
            ''')
            if result:
                return True
            time.sleep(0.5)
        raise Exception(f"No visible element found for selector '{selector}' within {timeout} seconds")

    def click(self, selector):
        """Click the first visible element matching the selector."""
        try:
            self.wait_for_selector(selector)
            # First try to scroll the element into view
            self.execute_script(f'''
                try {{
                    var elements = document.querySelectorAll("{selector}");
                    for (var i = 0; i < elements.length; i++) {{
                        var element = elements[i];
                        var style = window.getComputedStyle(element);
                        if (style.display !== "none" && style.visibility !== "hidden") {{
                            element.scrollIntoView({{behavior: "smooth", block: "center"}});
                            return true;
                        }}
                    }}
                    return false;
                }} catch (e) {{
                    console.error("Error scrolling to element:", e);
                    return false;
                }}
            ''')
            
            # Wait a moment for the scroll to complete
            time.sleep(1)
            
            # Now try to click using JavaScript
            result = self.execute_script(f'''
                try {{
                    var elements = document.querySelectorAll("{selector}");
                    for (var i = 0; i < elements.length; i++) {{
                        var element = elements[i];
                        var style = window.getComputedStyle(element);
                        if (style.display !== "none" && style.visibility !== "hidden") {{
                            // Create and dispatch mouse events for more reliable clicking
                            var rect = element.getBoundingClientRect();
                            var x = rect.left + rect.width / 2;
                            var y = rect.top + rect.height / 2;
                            
                            ['mousedown', 'mouseup', 'click'].forEach(function(eventType) {{
                                var clickEvent = new MouseEvent(eventType, {{
                                    view: window,
                                    bubbles: true,
                                    cancelable: true,
                                    clientX: x,
                                    clientY: y
                                }});
                                element.dispatchEvent(clickEvent);
                            }});
                            
                            return true;
                        }}
                    }}
                    return false;
                }} catch (e) {{
                    console.error("Error clicking element:", e);
                    return false;
                }}
            ''')
            
            if not result:
                # If JavaScript click fails, try using CDP to click
                elements = self.send_command('DOM.querySelectorAll', {
                    'nodeId': 1,  # Document node
                    'selector': selector
                })
                
                if 'nodeIds' in elements and len(elements['nodeIds']) > 0:
                    node_id = elements['nodeIds'][0]
                    box_model = self.send_command('DOM.getBoxModel', {'nodeId': node_id})
                    
                    if 'model' in box_model and 'content' in box_model['model']:
                        content = box_model['model']['content']
                        x = (content[0] + content[2]) / 2
                        y = (content[1] + content[5]) / 2
                        
                        self.send_command('Input.dispatchMouseEvent', {
                            'type': 'mousePressed',
                            'x': x,
                            'y': y,
                            'button': 'left',
                            'clickCount': 1
                        })
                        
                        self.send_command('Input.dispatchMouseEvent', {
                            'type': 'mouseReleased',
                            'x': x,
                            'y': y,
                            'button': 'left',
                            'clickCount': 1
                        })
                    else:
                        raise Exception("Could not get element position for clicking")
                else:
                    raise Exception(f"No elements found for selector: {selector}")
            
            # Wait after clicking
            time.sleep(2)
            
        except Exception as e:
            logger.error(f"Error clicking element with selector '{selector}': {str(e)}")
            # Try YouTube-specific selectors if we're on YouTube
            if "youtube.com" in self.current_url:
                self.handle_youtube_specific_click(selector)

    def handle_youtube_specific_click(self, original_selector):
        """Handle YouTube-specific clicking logic."""
        try:
            # If trying to click search box
            if "search" in original_selector:
                youtube_search_selectors = [
                    "input#search", 
                    "input[name='search_query']",
                    "#search-input input",
                    "input[aria-label='Search']"
                ]
                for selector in youtube_search_selectors:
                    try:
                        logger.info(f"Trying YouTube search selector: {selector}")
                        self.wait_for_selector(selector, timeout=5)
                        self.execute_script(f'''
                            var el = document.querySelector("{selector}");
                            if (el) {{
                                el.focus();
                                el.click();
                            }}
                        ''')
                        return
                    except Exception:
                        continue
            
            # If trying to click a video
            elif "video" in original_selector:
                youtube_video_selectors = [
                    "ytd-video-renderer", 
                    "#contents ytd-video-renderer",
                    "a.yt-simple-endpoint.ytd-video-renderer",
                    "#video-title"
                ]
                for selector in youtube_video_selectors:
                    try:
                        logger.info(f"Trying YouTube video selector: {selector}")
                        self.wait_for_selector(selector, timeout=5)
                        self.execute_script(f'''
                            var videos = document.querySelectorAll("{selector}");
                            if (videos && videos.length > 0) {{
                                // Click the first video
                                var firstVideo = videos[0];
                                
                                // If this is a container, find the actual link inside
                                var link = firstVideo.querySelector("a") || firstVideo;
                                
                                // Scroll into view
                                link.scrollIntoView({{behavior: "smooth", block: "center"}});
                                setTimeout(function() {{
                                    // Create and dispatch click events
                                    var rect = link.getBoundingClientRect();
                                    var x = rect.left + rect.width / 2;
                                    var y = rect.top + rect.height / 2;
                                    
                                    ['mousedown', 'mouseup', 'click'].forEach(function(eventType) {{
                                        var clickEvent = new MouseEvent(eventType, {{
                                            view: window,
                                            bubbles: true,
                                            cancelable: true,
                                            clientX: x,
                                            clientY: y
                                        }});
                                        link.dispatchEvent(clickEvent);
                                    }});
                                    
                                    // As a fallback, also try navigating directly
                                    if (link.href) {{
                                        window.location.href = link.href;
                                    }}
                                }}, 500);
                            }}
                        ''')
                        time.sleep(3)  # Wait longer for video to load
                        return
                    except Exception as e:
                        logger.error(f"Error with video selector {selector}: {e}")
                        continue
            
            # If trying to click a play button
            elif "play" in original_selector:
                youtube_play_selectors = [
                    ".ytp-play-button", 
                    "button.ytp-play-button",
                    ".html5-video-player"
                ]
                for selector in youtube_play_selectors:
                    try:
                        logger.info(f"Trying YouTube play selector: {selector}")
                        self.wait_for_selector(selector, timeout=5)
                        
                        # For video player, we can just click it or try to play directly
                        if selector == ".html5-video-player":
                            self.execute_script('''
                                var video = document.querySelector("video");
                                if (video) {
                                    video.play();
                                }
                                
                                var player = document.querySelector(".html5-video-player");
                                if (player) {
                                    player.click();
                                }
                            ''')
                        else:
                            self.execute_script(f'''
                                var button = document.querySelector("{selector}");
                                if (button) {{
                                    button.click();
                                }}
                            ''')
                        return
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"Error with YouTube-specific handling: {str(e)}")

    def type(self, selector, text):
        """Type text into the first visible input/textarea matching the selector."""
        try:
            self.wait_for_selector(selector)
            # First focus the element
            self.execute_script(f'''
                try {{
                    var element = document.querySelector("{selector}");
                    if (element) {{
                        element.focus();
                        return true;
                    }}
                    return false;
                }} catch (e) {{
                    console.error("Error focusing element:", e);
                    return false;
                }}
            ''')
            
            time.sleep(0.5)
            
            # Clear existing text
            self.execute_script(f'''
                try {{
                    var element = document.querySelector("{selector}");
                    if (element) {{
                        element.value = "";
                        element.dispatchEvent(new Event("input", {{ bubbles: true }}));
                        return true;
                    }}
                    return false;
                }} catch (e) {{
                    console.error("Error clearing element:", e);
                    return false;
                }}
            ''')
            
            time.sleep(0.5)
            
            # Type the text character by character for more reliability
            for char in text:
                self.execute_script(f'''
                    try {{
                        var element = document.querySelector("{selector}");
                        if (element) {{
                            element.value += "{char}";
                            element.dispatchEvent(new Event("input", {{ bubbles: true }}));
                            return true;
                        }}
                        return false;
                    }} catch (e) {{
                        console.error("Error typing character:", e);
                        return false;
                    }}
                ''')
                time.sleep(0.05)  # Small delay between characters
            
            # Final input and change events
            self.execute_script(f'''
                try {{
                    var element = document.querySelector("{selector}");
                    if (element) {{
                        element.dispatchEvent(new Event("input", {{ bubbles: true }}));
                        element.dispatchEvent(new Event("change", {{ bubbles: true }}));
                        return true;
                    }}
                    return false;
                }} catch (e) {{
                    console.error("Error dispatching final events:", e);
                    return false;
                }}
            ''')
            
        except Exception as e:
            logger.error(f"Error typing in element with selector '{selector}': {str(e)}")
            # Try YouTube-specific selectors if we're on YouTube
            if "youtube.com" in self.current_url:
                youtube_selectors = [
                    "input#search", 
                    "input[name='search_query']",
                    "#search-input input",
                    "input[aria-label='Search']"
                ]
                for alt_selector in youtube_selectors:
                    logger.info(f"Trying YouTube search selector for typing: {alt_selector}")
                    try:
                        self.wait_for_selector(alt_selector, timeout=5)
                        self.execute_script(f'''
                            var el = document.querySelector("{alt_selector}");
                            if (el) {{
                                el.focus();
                                el.value = "{text}";
                                el.dispatchEvent(new Event("input", {{ bubbles: true }}));
                                el.dispatchEvent(new Event("change", {{ bubbles: true }}));
                            }}
                        ''')
                        return
                    except Exception:
                        continue

    def press_enter(self, selector):
        """Press Enter key on the specified element."""
        try:
            # First make sure the element is focused
            self.execute_script(f'''
                try {{
                    var element = document.querySelector("{selector}");
                    if (element) {{
                        element.focus();
                        return true;
                    }}
                    return false;
                }} catch (e) {{
                    console.error("Error focusing element:", e);
                    return false;
                }}
            ''')
            
            time.sleep(0.5)
            
            # Try multiple approaches to press Enter
            result = self.execute_script(f'''
                try {{
                    var element = document.querySelector("{selector}");
                    if (element) {{
                        // Method 1: KeyboardEvent
                        element.dispatchEvent(new KeyboardEvent("keydown", {{
                            key: "Enter",
                            code: "Enter",
                            keyCode: 13,
                            which: 13,
                            bubbles: true
                        }}));
                        
                        element.dispatchEvent(new KeyboardEvent("keypress", {{
                            key: "Enter",
                            code: "Enter",
                            keyCode: 13,
                            which: 13,
                            bubbles: true
                        }}));
                        
                        element.dispatchEvent(new KeyboardEvent("keyup", {{
                            key: "Enter",
                            code: "Enter",
                            keyCode: 13,
                            which: 13,
                            bubbles: true
                        }}));
                        
                        // Method 2: Form submission if element is in a form
                        if (element.form) {{
                            element.form.submit();
                        }}
                        
                        return true;
                    }}
                    return false;
                }} catch (e) {{
                    console.error("Error pressing Enter:", e);
                    return false;
                }}
            ''')
            
            if not result:
                # Try using CDP to send Enter key
                self.send_command('Input.dispatchKeyEvent', {
                    'type': 'keyDown',
                    'key': 'Enter',
                    'code': 'Enter',
                    'windowsVirtualKeyCode': 13
                })
                
                self.send_command('Input.dispatchKeyEvent', {
                    'type': 'keyUp',
                    'key': 'Enter',
                    'code': 'Enter',
                    'windowsVirtualKeyCode': 13
                })
            
            # Wait after pressing Enter
            time.sleep(2)
            
        except Exception as e:
            logger.error(f"Error pressing Enter on element with selector '{selector}': {str(e)}")
            # Try YouTube-specific handling
            if "youtube.com" in self.current_url:
                try:
                    # Try clicking the search button instead
                    self.execute_script('''
                        var searchButton = document.querySelector("#search-icon-legacy") || 
                                           document.querySelector("button[aria-label='Search']");
                        if (searchButton) {
                            searchButton.click();
                            return;
                        }
                        
                        // Try submitting the search form
                        var searchForm = document.querySelector("form#search-form");
                        if (searchForm) {
                            searchForm.submit();
                            return;
                        }
                        
                        // Last resort: try to press Enter on the search input again
                        var searchInput = document.querySelector("input#search") || 
                                          document.querySelector("input[name='search_query']");
                        if (searchInput) {
                            searchInput.dispatchEvent(new KeyboardEvent("keydown", {
                                key: "Enter",
                                code: "Enter",
                                keyCode: 13,
                                which: 13,
                                bubbles: true
                            }));
                        }
                    ''')
                    time.sleep(2)
                except Exception as e2:
                    logger.error(f"Error with YouTube-specific Enter handling: {str(e2)}")

    def close(self):
        """Close the WebSocket connection and browser."""
        if self.ws:
            try:
                self.ws.close()
            except Exception as e:
                logger.error(f"Error closing WebSocket: {str(e)}")
        
        if self.process:
            try:
                self.process.terminate()
                time.sleep(1)
                if self.process.poll() is None:
                    self.process.kill()
            except Exception as e:
                logger.error(f"Error closing browser process: {str(e)}")


def generate_youtube_selectors(step_description):
    """Generate YouTube-specific selectors based on the step description."""
    if "search" in step_description.lower():
        return "input#search, input[name='search_query']"
    elif "click" in step_description.lower() and "video" in step_description.lower():
        return "ytd-video-renderer, .ytd-video-renderer, a.yt-simple-endpoint, #video-title"
    elif "play" in step_description.lower():
        return ".ytp-play-button, button.ytp-play-button, .html5-video-player"
    return None


def parse_natural_language_command(command):
    """Parse a natural language command into a sequence of steps."""
    try:
        model = genai.GenerativeModel('gemini-2.0-flash')
        prompt = f"""
        Break down this browser automation command into sequential steps:

        "{command}"

        Return a JSON array of steps, where each step has:
        1. "action": The action to perform (navigate, search, click, type, wait, press_enter)
        2. "description": A detailed description of what to do
        3. "target": The target website or element (if applicable)
        4. "value": Any value to input (if applicable)

        Example:
        For "open YouTube and search for angry prash and play the first video":

        [
            {{
                "action": "navigate",
                "description": "Navigate to YouTube homepage",
                "target": "https://www.youtube.com/",
                "value": null
            }},
            {{
                "action": "click",
                "description": "Click on the search input field",
                "target": "search field",
                "value": null
            }},
            {{
                "action": "type",
                "description": "Type 'angry prash' in the search field",
                "target": "search field",
                "value": "angry prash"
            }},
            {{
                "action": "press_enter",
                "description": "Press Enter to submit the search",
                "target": "search field",
                "value": null
            }},
            {{
                "action": "wait",
                "description": "Wait for search results to load",
                "target": null,
                "value": 2
            }},
            {{
                "action": "click",
                "description": "Click on the first video in search results",
                "target": "first video thumbnail",
                "value": null
            }}
        ]

        Return ONLY valid JSON, no additional text.
        """
        response = model.generate_content(prompt)
        response_text = response.text
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()
        steps = json.loads(response_text)
        return steps
    except Exception as e:
        logger.error(f"Error parsing command: {str(e)}")
        if "youtube" in command.lower() and "search" in command.lower():
            search_term = command.lower().split("search for")[1].split("and")[0].strip() if "search for" in command.lower() else "example video"
            return [
                {
                    "action": "navigate",
                    "description": "Navigate to YouTube homepage",
                    "target": "https://www.youtube.com/",
                    "value": None
                },
                {
                    "action": "click",
                    "description": "Click on the search input field",
                    "target": "search field",
                    "value": None
                },
                {
                    "action": "type",
                    "description": "Type search term in the search field",
                    "target": "search field",
                    "value": search_term
                },
                {
                    "action": "press_enter",
                    "description": "Press Enter to submit the search",
                    "target": "search field",
                    "value": None
                },
                {
                    "action": "wait",
                    "description": "Wait for search results to load",
                    "target": None,
                    "value": 3
                },
                {
                    "action": "click",
                    "description": "Click on the first video in search results",
                    "target": "first video thumbnail",
                    "value": None
                }
            ]
        raise


def execute_natural_language_command(command, browser_type='chrome'):
    """Execute a natural language command by breaking it down into steps and executing each step."""
    controller = BrowserController(browser_type)
    try:
        controller.start_browser()
        controller.connect()
        steps = parse_natural_language_command(command)
        logger.info(f"Parsed command into {len(steps)} steps")
        
        for i, step in enumerate(steps):
            action = step.get('action')
            description = step.get('description')
            target = step.get('target')
            value = step.get('value')
            logger.info(f"Step {i + 1}: {description}")
            
            if action == 'navigate':
                if target and isinstance(target, str) and target.startswith(('http://', 'https://')):
                    controller.navigate(target)
                else:
                    if target and isinstance(target, str) and 'youtube' in target.lower():
                        controller.navigate('https://www.youtube.com/')
                    elif target and isinstance(target, str) and 'google' in target.lower():
                        controller.navigate('https://www.google.com/')
                    else:
                        controller.navigate(f'https://{target}')
                logger.info(f"Navigated to {controller.current_url}")
                
            elif action == 'search' or (action == 'type' and target and 'search' in str(target).lower()):
                # For YouTube, use predefined selectors
                if "youtube.com" in controller.current_url:
                    search_selector = "input#search, input[name='search_query']"
                else:
                    html = controller.get_current_html()
                    search_selector = generate_youtube_selectors(description) or "input[type='search'], input[name*='search'], input[placeholder*='Search']"
                
                logger.info(f"Using search selector: {search_selector}")
                controller.click(search_selector)
                controller.type(search_selector, value)
                controller.press_enter(search_selector)
                time.sleep(3)  # Wait longer for search results
                
            elif action == 'click':
                # For YouTube, use predefined selectors based on context
                if "youtube.com" in controller.current_url:
                    if "video" in description.lower():
                        click_selector = "ytd-video-renderer, #video-title"
                    elif "search" in description.lower():
                        click_selector = "input#search, input[name='search_query']"
                    elif "play" in description.lower():
                        click_selector = ".ytp-play-button, button.ytp-play-button"
                    else:
                        click_selector = generate_youtube_selectors(description) or "button, a, [role='button']"
                else:
                    html = controller.get_current_html()
                    click_selector = "button, a, [role='button']"
                
                logger.info(f"Using click selector: {click_selector}")
                controller.click(click_selector)
                time.sleep(3)  # Wait longer after clicking
                
            elif action == 'type':
                if "youtube.com" in controller.current_url:
                    input_selector = "input#search, input[name='search_query']"
                else:
                    html = controller.get_current_html()
                    input_selector = "input, textarea"
                
                logger.info(f"Using input selector: {input_selector}")
                controller.type(input_selector, value)
                
            elif action == 'press_enter':
                if "youtube.com" in controller.current_url:
                    enter_selector = "input#search, input[name='search_query']"
                else:
                    html = controller.get_current_html()
                    enter_selector = "input, textarea"
                
                logger.info(f"Using enter selector: {enter_selector}")
                controller.press_enter(enter_selector)
                time.sleep(3)  # Wait longer after pressing enter
                
            elif action == 'wait':
                wait_time = int(value) if value and isinstance(value, (int, float, str)) and str(value).isdigit() else 3
                logger.info(f"Waiting for {wait_time} seconds")
                time.sleep(wait_time)
        
        return {"status": "success", "message": f"Successfully executed command: {command}"}
    except Exception as e:
        logger.error(f"Error executing command: {str(e)}")
        return {"status": "error", "message": str(e)}
    finally:
        controller.close()


@app.route('/execute', methods=['POST'])
def execute():
    """Handle POST requests to execute natural language commands."""
    data = request.json
    if not data or 'command' not in data:
        return jsonify({"error": "Missing command"}), 400
    
    browser_type = data.get('browser', 'chrome').lower()
    if browser_type not in ['chrome', 'firefox']:
        return jsonify({"error": "Unsupported browser type. Use 'chrome' or 'firefox'"}), 400
    
    try:
        result = execute_natural_language_command(data['command'], browser_type)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error in execute endpoint: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

# # Test the script
# print("Browser Controller script is ready. Run this file to start the Flask server.")
# print("Then use the React frontend to send commands to the browser.")
