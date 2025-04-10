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

### Helper Functions

def get_clean_html(url):
    """Extract clean HTML from a URL for AI processing."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        # Remove unwanted elements
        for tag in soup(['script', 'style', 'noscript', 'meta', 'link', 'svg', 'img']):
            tag.decompose()

        # Remove comments
        for comment in soup.find_all(text=lambda text: isinstance(text, Comment)):
            comment.extract()

        # Preserve structural elements and inputs
        preserved_tags = ['input', 'form', 'button', 'select', 'textarea']
        for tag in soup.find_all():
            if tag.name not in preserved_tags:
                if 'style' in tag.attrs:
                    del tag['style']
                if 'class' in tag.attrs:
                    del tag['class']

        # Remove completely empty containers (except forms)
        for tag in soup.find_all():
            if tag.name != 'form' and not tag.contents and not tag.attrs:
                tag.decompose()

        return str(soup)
    except Exception as e:
        logger.error(f"Error extracting HTML: {str(e)}")
        return f"Error: {str(e)}"

def generate_selectors_from_html(html, instruction):
    """Generate CSS selectors using Gemini API based on HTML and instruction."""
    try:
        model = genai.GenerativeModel('gemini-pro')
        prompt = f"Given the following HTML:\n\n{html}\n\n{instruction}, provide a CSS selector."
        response = model.generate_content(prompt)
        selector = response.text.strip()
        return selector
    except Exception as e:
        logger.error(f"Error generating selector: {str(e)}")
        return None

def parse_natural_language_command(command):
    """Parse a natural language command into actionable steps."""
    # Simple parsing logic (expandable based on needs)
    steps = []
    command = command.lower().strip()
    if 'go to' in command or 'navigate to' in command:
        url = command.replace('go to', '').replace('navigate to', '').strip()
        steps.append({'action': 'navigate', 'description': f"Navigate to {url}", 'target': url})
    elif 'search for' in command:
        query = command.replace('search for', '').strip()
        steps.extend([
            {'action': 'navigate', 'description': 'Navigate to YouTube', 'target': 'https://www.youtube.com/'},
            {'action': 'type', 'description': f"Type '{query}' into search bar", 'target': 'search bar', 'value': query},
            {'action': 'press_enter', 'description': 'Press Enter to search', 'target': 'search bar'}
        ])
    elif 'click on' in command:
        target = command.replace('click on', '').strip()
        steps.append({'action': 'click', 'description': f"Click on {target}", 'target': target})
    return steps

### BrowserController Class

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
        while True:
            event = self.event_queue.get()
            if event['method'] == 'Page.loadEventFired':
                break
        time.sleep(2)  # Wait for dynamic content

    def execute_script(self, script):
        """Execute JavaScript in the browser and return the result."""
        result = self.send_command('Runtime.evaluate', {'expression': script})
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
        """Click an element and verify the action had an effect."""
        try:
            self.wait_for_selector(selector)
            script = f'''
                try {{
                    var elements = document.querySelectorAll("{selector}");
                    for (var i = 0; i < elements.length; i++) {{
                        var style = window.getComputedStyle(elements[i]);
                        if (style.display !== "none" && style.visibility !== "hidden") {{
                            elements[i].scrollIntoView({{behavior: "smooth", block: "center"}});
                            elements[i].click();
                            return true;
                        }}
                    }}
                    return false;
                }} catch (e) {{
                    console.error("Error in click:", e);
                    return false;
                }}
            '''
            result = self.execute_script(script)
            if not result:
                raise Exception(f"No visible element to click for selector '{selector}'")
            time.sleep(1)  # Allow time for reaction
            new_url = self.execute_script("return window.location.href")
            logger.debug(f"Post-click URL: {new_url}")
            return True
        except Exception as e:
            logger.error(f"Error clicking element with selector '{selector}': {str(e)}")
            if "youtube.com" in self.current_url:
                alt_selectors = [
                    "ytd-video-renderer a#thumbnail",  # Video thumbnail
                    "button[aria-label='Search']",     # Search button
                ]
                for alt_selector in alt_selectors:
                    try:
                        self.wait_for_selector(alt_selector)
                        self.execute_script(f'document.querySelector("{alt_selector}").click();')
                        time.sleep(1)
                        return True
                    except Exception:
                        continue
            raise

    def type(self, selector, text):
        """Type text into an element and verify the input."""
        try:
            self.wait_for_selector(selector)
            script = f'''
                try {{
                    var elements = document.querySelectorAll("{selector}");
                    for (var i = 0; i < elements.length; i++) {{
                        var style = window.getComputedStyle(elements[i]);
                        if (style.display !== "none" && style.visibility !== "hidden") {{
                            elements[i].focus();
                            elements[i].value = "{text}";
                            elements[i].dispatchEvent(new Event("input", {{ bubbles: true }}));
                            elements[i].dispatchEvent(new Event("change", {{ bubbles: true }}));
                            return true;
                        }}
                    }}
                    return false;
                }} catch (e) {{
                    console.error("Error in type:", e);
                    return false;
                }}
            '''
            result = self.execute_script(script)
            if not result:
                raise Exception(f"No visible element to type into for selector '{selector}'")
            entered_text = self.execute_script(f'return document.querySelector("{selector}").value')
            if entered_text != text:
                raise Exception(f"Typed text '{text}' not found in element; got '{entered_text}'")
            return True
        except Exception as e:
            logger.error(f"Error typing in element with selector '{selector}': {str(e)}")
            if "youtube.com" in self.current_url:
                alt_selectors = ["input#search", "input[name='search_query']"]
                for alt_selector in alt_selectors:
                    try:
                        self.wait_for_selector(alt_selector)
                        self.execute_script(f'''
                            var el = document.querySelector("{alt_selector}");
                            el.focus();
                            el.value = "{text}";
                            el.dispatchEvent(new Event("input", {{ bubbles: true }}));
                            el.dispatchEvent(new Event("change", {{ bubbles: true }}));
                        ''')
                        return True
                    except Exception:
                        continue
            raise

    def press_enter(self, selector):
        """Press Enter and verify the action (e.g., form submission)."""
        try:
            self.wait_for_selector(selector)
            original_url = self.current_url
            script = f'''
                try {{
                    var element = document.querySelector("{selector}");
                    if (element) {{
                        element.dispatchEvent(new KeyboardEvent("keydown", {{
                            key: "Enter",
                            code: "Enter",
                            keyCode: 13,
                            which: 13,
                            bubbles: true
                        }}));
                        return true;
                    }}
                    return false;
                }} catch (e) {{
                    console.error("Error in press_enter:", e);
                    return false;
                }}
            '''
            result = self.execute_script(script)
            if not result:
                raise Exception(f"No element to press Enter on for selector '{selector}'")
            time.sleep(2)
            new_url = self.execute_script("return window.location.href")
            if new_url == original_url and "youtube.com" in self.current_url:
                self.execute_script('''
                    var btn = document.querySelector("#search-icon-legacy") || 
                              document.querySelector("button[aria-label='Search']");
                    if (btn) btn.click();
                ''')
            return True
        except Exception as e:
            logger.error(f"Error pressing Enter on selector '{selector}': {str(e)}")
            raise

    def wait_for_element_change(self, selector, timeout=10):
        """Wait for an element to appear or change."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.execute_script(f'return !!document.querySelector("{selector}")'):
                return True
            time.sleep(0.5)
        return False

    def close(self):
        """Close the WebSocket connection and browser process."""
        if self.ws:
            self.ws.close()
        if self.process:
            self.process.terminate()

### Command Execution

def execute_natural_language_command(command, browser_type='chrome'):
    """Execute a natural language command with verification before logging."""
    controller = BrowserController(browser_type)
    try:
        controller.start_browser()
        controller.connect()
        steps = parse_natural_language_command(command)
        for i, step in enumerate(steps):
            action = step.get('action')
            description = step.get('description')
            target = step.get('target')
            value = step.get('value')

            if action == 'navigate':
                if target and isinstance(target, str) and target.startswith(('http://', 'https://')):
                    controller.navigate(target)
                else:
                    if target and 'youtube' in target.lower():
                        controller.navigate('https://www.youtube.com/')
                    elif target and 'google' in target.lower():
                        controller.navigate('https://www.google.com/')
                    else:
                        controller.navigate(f'https://{target}')
                html = controller.get_current_html()
                if controller.current_url:
                    logger.info(f"Step {i + 1}: {description} - Navigated to {controller.current_url}")

            elif action == 'type':
                html = controller.get_current_html()
                selector = generate_selectors_from_html(html, f"find an input field for {target}")
                if "youtube.com" in controller.current_url and 'search' in description.lower():
                    selector = "input#search, input[name='search_query']"
                controller.type(selector, value)
                logger.info(f"Step {i + 1}: {description} - Typed '{value}' into selector '{selector}'")

            elif action == 'click':
                html = controller.get_current_html()
                selector = generate_selectors_from_html(html, f"click on {target}")
                if "youtube.com" in controller.current_url and 'video' in description.lower():
                    selector = "ytd-video-renderer a#thumbnail"
                controller.click(selector)
                logger.info(f"Step {i + 1}: {description} - Clicked selector '{selector}'")

            elif action == 'press_enter':
                html = controller.get_current_html()
                selector = generate_selectors_from_html(html, f"find the element to press Enter on for {target}")
                if "youtube.com" in controller.current_url and 'search' in description.lower():
                    selector = "input#search, input[name='search_query']"
                controller.press_enter(selector)
                if "youtube.com" in controller.current_url:
                    controller.wait_for_element_change("ytd-video-renderer")
                logger.info(f"Step {i + 1}: {description} - Pressed Enter on selector '{selector}'")

            elif action == 'wait':
                wait_time = int(value) if value and str(value).isdigit() else 2
                time.sleep(wait_time)
                logger.info(f"Step {i + 1}: {description} - Waited for {wait_time} seconds")

        return {"status": "success", "message": f"Successfully executed command: {command}"}
    except Exception as e:
        logger.error(f"Error executing command: {str(e)}")
        return {"status": "error", "message": str(e)}
    finally:
        controller.close()

### Flask Routes

@app.route('/execute', methods=['POST'])
def execute_command():
    """API endpoint to execute a natural language command."""
    data = request.get_json()
    if not data or 'command' not in data:
        return jsonify({"status": "error", "message": "No command provided"}), 400
    command = data['command']
    browser_type = data.get('browser_type', 'chrome')
    result = execute_natural_language_command(command, browser_type)
    return jsonify(result)

### Main Execution

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)