import os
import re
import json
import time
import logging
import requests
import subprocess
import websocket
import threading
import queue
from bs4 import BeautifulSoup, Comment
import google.generativeai as genai
from dotenv import load_dotenv
from flask import Flask, request, jsonify

app = Flask(__name__)

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configure Gemini API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)


class BrowserController:
    def __init__(self):
        self.browser_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        self.port = 9222
        self.process = None
        self.ws = None
        self.response_queue = queue.Queue()
        self.event_queue = queue.Queue()
        self.command_id = 1
        self.current_url = None
        self.page_loaded = threading.Event()

    def start_browser(self):
        """Start Chrome with remote debugging"""
        cmd = [
            self.browser_path,
            f'--remote-debugging-port={self.port}',
            '--user-data-dir=C:\\Temp\\chrome_temp',
            '--no-first-run',
            '--no-default-browser-check',
            '--remote-allow-origins=*',
            '--start-maximized'
        ]

        self.process = subprocess.Popen(cmd)
        time.sleep(3)  # Give browser time to start
        logger.info("Chrome browser started")

    def connect(self):
        """Connect to WebSocket debugger"""
        # Retry connection a few times
        max_retries = 5
        for attempt in range(max_retries):
            try:
                response = requests.get(f'http://localhost:{self.port}/json/list')
                if response.status_code == 200 and response.json():
                    debugger_url = response.json()[0]['webSocketDebuggerUrl']

                    self.ws = websocket.WebSocket()
                    self.ws.connect(debugger_url)

                    # Start message handler thread
                    threading.Thread(target=self._message_handler, daemon=True).start()

                    # Enable necessary domains
                    self.send_command("Page.enable", {})
                    self.send_command("DOM.enable", {})
                    self.send_command("Runtime.enable", {})

                    logger.info("Connected to browser debugger")
                    return True
                else:
                    logger.warning(f"Failed to get debugger URL, attempt {attempt + 1}/{max_retries}")
            except Exception as e:
                logger.error(f"Connection error (attempt {attempt + 1}/{max_retries}): {e}")

            time.sleep(2)

        logger.error("Failed to connect to browser after multiple attempts")
        return False

    def _message_handler(self):
        """Handle WebSocket messages"""
        while True:
            try:
                message = self.ws.recv()
                data = json.loads(message)

                # Handle page load events
                if 'method' in data and data['method'] == 'Page.loadEventFired':
                    self.page_loaded.set()

                # Handle command responses
                if 'id' in data:
                    self.response_queue.put(data)
                # Handle events
                elif 'method' in data:
                    self.event_queue.put(data)

            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                break

    def send_command(self, method, params):
        """Send Chrome DevTools Protocol command"""
        cmd_id = self.command_id
        self.command_id += 1

        command = json.dumps({
            "id": cmd_id,
            "method": method,
            "params": params
        })

        logger.debug(f"Sending command: {command}")
        self.ws.send(command)

        # Wait for response with timeout
        timeout = 10  # seconds
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                response = self.response_queue.get(timeout=1)
                if response['id'] == cmd_id:
                    if 'error' in response:
                        logger.error(f"Command error: {response['error']}")
                        raise Exception(f"Command error: {response['error']}")
                    return response.get('result', {})
            except queue.Empty:
                continue

        raise Exception(f"Command timed out: {method}")

    def navigate(self, url):
        """Navigate to URL and wait for load"""
        # Reset page loaded event
        self.page_loaded.clear()

        # Navigate to URL
        self.send_command("Page.navigate", {"url": url})
        self.current_url = url

        # Wait for page load with timeout
        if not self.page_loaded.wait(timeout=30):
            logger.warning("Page load timeout, continuing anyway")

        # Additional wait for page to stabilize
        time.sleep(3)
        logger.info(f"Navigated to {url}")

    def get_clean_html(self):
        """Get and clean current page HTML"""
        result = self.send_command("Runtime.evaluate", {
            "expression": "document.documentElement.outerHTML",
            "returnByValue": True
        })

        if "result" in result and "value" in result["result"]:
            html = result["result"]["value"]
            soup = BeautifulSoup(html, 'html.parser')

            # Clean HTML
            for tag in soup(['script', 'style', 'noscript', 'meta', 'link', 'svg']):
                tag.decompose()

            # Use string parameter instead of text (addressing the deprecation warning)
            for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
                comment.extract()

            return str(soup)

        return None

    def execute_js(self, script):
        """Execute JavaScript and return result"""
        result = self.send_command("Runtime.evaluate", {
            "expression": script,
            "returnByValue": True,
            "awaitPromise": True
        })

        return result.get("result", {}).get("value")

    def click_element(self, selector):
        """Click element with CDP protocol for more reliable clicks"""
        # First check if element exists
        element_exists = self.execute_js(f"""
            !!document.querySelector('{selector}')
        """)

        if not element_exists:
            logger.error(f"Element not found: {selector}")
            # Try to find a better selector by inspecting the page
            self.find_better_selector(selector)
            return False

        # Get element node ID
        node_id_result = self.send_command("DOM.querySelector", {
            "nodeId": 1,  # Document node
            "selector": selector
        })

        if "nodeId" not in node_id_result or node_id_result["nodeId"] == 0:
            logger.error(f"Could not get nodeId for: {selector}")
            return False

        # Get element box model
        box_model = self.send_command("DOM.getBoxModel", {
            "nodeId": node_id_result["nodeId"]
        })

        if "model" not in box_model:
            logger.error(f"Could not get box model for: {selector}")
            return False

        # Calculate center point
        content = box_model["model"]["content"]
        x = (content[0] + content[2]) / 2
        y = (content[1] + content[5]) / 2

        # Scroll element into view
        self.execute_js(f"""
            document.querySelector('{selector}').scrollIntoView({{
                behavior: 'smooth',
                block: 'center'
            }});
        """)
        time.sleep(1)

        # Perform mouse click using Input domain
        self.send_command("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": x,
            "y": y,
            "button": "left",
            "clickCount": 1
        })

        self.send_command("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": x,
            "y": y,
            "button": "left",
            "clickCount": 1
        })

        # Wait for potential page changes
        time.sleep(2)
        logger.info(f"Clicked element: {selector}")
        return True

    def find_better_selector(self, failed_selector):
        """Try to find a better selector by inspecting the page"""
        # For Google search box specifically
        if 'q' in failed_selector:
            # Try common selectors for search inputs
            potential_selectors = [
                "input[name='q']",
                "input[type='text']",
                "input[title='Search']",
                "input.gLFyf",  # Google's search class
                "input[aria-label='Search']",
                ".search-box",
                "#search-form input",
                "form input[type='text']"
            ]
            
            for selector in potential_selectors:
                exists = self.execute_js(f"return !!document.querySelector('{selector}')")
                if exists:
                    logger.info(f"Found alternative selector: {selector} instead of {failed_selector}")
                    return selector
                    
        # For general elements, log some page info to help debug
        self.execute_js("""
            console.log('Available input elements:');
            document.querySelectorAll('input').forEach((el, i) => {
                console.log(`Input ${i}:`, el.outerHTML);
            });
        """)
        return None

    def type_text(self, selector, text):
        """Type text into input field using CDP Input domain"""
        # Try to find the element with multiple selectors
        if selector == "textarea[name='q']":  # Google search specific
            potential_selectors = [
                "textarea[name='q']",
                "input[name='q']",
                "input[title='Search']",
                "input.gLFyf",
                "input[aria-label='Search']"
            ]
            
            for potential_selector in potential_selectors:
                element_exists = self.execute_js(f"""
                    const el = document.querySelector('{potential_selector}');
                    if (el) {{
                        el.focus();
                        el.value = '';
                        return true;
                    }}
                    return false;
                """)
                
                if element_exists:
                    logger.info(f"Found element with selector: {potential_selector}")
                    selector = potential_selector
                    break
        else:
            # For non-Google search elements
            element_exists = self.execute_js(f"""
                const el = document.querySelector('{selector}');
                if (el) {{
                    el.focus();
                    el.value = '';
                    return true;
                }}
                return false;
            """)
        
        if not element_exists:
            # Try a more direct approach with JavaScript
            logger.warning(f"Element not found for typing with selector: {selector}")
            logger.info("Trying alternative typing method...")
            
            # Try to type directly using JavaScript
            success = self.execute_js(f"""
                // Try to find any input element that's visible
                const inputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type]), textarea'));
                const visibleInput = inputs.find(el => {{
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0 && 
                           window.getComputedStyle(el).display !== 'none' &&
                           window.getComputedStyle(el).visibility !== 'hidden';
                }});
                
                if (visibleInput) {{
                    visibleInput.focus();
                    visibleInput.value = '{text}';
                    visibleInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    visibleInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return true;
                }}
                return false;
            """)
            
            if success:
                logger.info(f"Typed text using alternative method: {text}")
                return True
            else:
                logger.error("Failed to find any input element for typing")
                return False

        # Type each character with proper events
        for char in text:
            # Use Input.insertText for more reliable typing
            self.send_command("Input.insertText", {
                "text": char
            })
            time.sleep(0.05)  # Small delay between characters

        # Trigger input and change events
        self.execute_js(f"""
            const el = document.querySelector('{selector}');
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        """)

        logger.info(f"Typed text: {text}")
        return True

    def press_enter(self, selector=None):
        """Press Enter key using Input domain"""
        # If selector provided, focus that element first
        if selector:
            focused = self.execute_js(f"""
                const el = document.querySelector('{selector}');
                if (el) {{
                    el.focus();
                    return true;
                }}
                return false;
            """)
            
            if not focused:
                # Try to find any focused element
                self.execute_js("""
                    // Try to find the active element and press enter on it
                    if (document.activeElement && 
                        document.activeElement !== document.body) {
                        document.activeElement.focus();
                    }
                """)

        # Dispatch keyDown and keyUp events for Enter
        self.send_command("Input.dispatchKeyEvent", {
            "type": "keyDown",
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13
        })

        self.send_command("Input.dispatchKeyEvent", {
            "type": "keyUp",
            "key": "Enter",
            "code": "Enter",
            "windowsVirtualKeyCode": 13
        })

        # Alternative: try to submit the form if there is one
        self.execute_js("""
            const form = document.querySelector('form');
            if (form) {
                form.submit();
            }
        """)

        time.sleep(2)  # Wait for potential page changes
        logger.info("Pressed Enter key")
        return True

    def close(self):
        """Cleanup browser instance"""
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
        if self.process:
            try:
                self.process.terminate()
            except:
                pass
        logger.info("Browser closed")


def parse_command(current_html, remaining_command, previous_actions=[]):
    """Use Gemini to generate next step based on current page state"""
    model = genai.GenerativeModel('gemini-2.0-flash')

    prompt = f"""
    Given the current web page state and user command, determine the next action.

    Previous actions:
    {json.dumps(previous_actions, indent=2) if previous_actions else "None"}

    Remaining command: "{remaining_command}"

    Current page HTML (important elements only):
    {current_html[:15000]}

    Response format:
    {{
        "action": "navigate|click|type|press_enter|wait|complete",
        "description": "Step description",
        "target": "URL or selector",
        "value": "Input text or seconds",
        "remaining_command": "Updated remaining command text",
        "completed": boolean
    }}

    Rules:
    1. If page needs navigation, use "navigate"
    2. For interactive elements, use precise CSS selectors
    3. For search results, select first relevant element
    4. Mark "completed": true when command is fulfilled
    5. Update remaining_command by removing completed parts
    6. For Google search, use "input[name='q']" as the selector for the search box

    Examples:
    1. For initial command "search google for ai tools":
    {{
        "action": "navigate",
        "description": "Go to Google",
        "target": "https://google.com",
        "value": null,
        "remaining_command": "search for ai tools",
        "completed": false
    }}

    2. On Google's homepage with remaining command "search for ai tools":
    {{
        "action": "type",
        "description": "Enter search term",
        "target": "input[name='q']",
        "value": "ai tools",
        "remaining_command": "submit search",
        "completed": false
    }}
    """

    response = model.generate_content(prompt)
    cleaned = re.sub(r'```json|```', '', response.text).strip()
    return json.loads(cleaned)


def execute_command(command):
    """Dynamic execution flow with iterative HTML analysis"""
    controller = BrowserController()
    try:
        controller.start_browser()
        if not controller.connect():
            logger.error("Failed to connect to browser")
            return False

        current_command = command
        previous_steps = []
        completion_status = False
        max_attempts = 3  # Maximum number of retry attempts for each step

        while not completion_status:
            # Get current page state
            current_html = controller.get_clean_html() or ""

            # Generate next step
            step = parse_command(
                current_html=current_html,
                remaining_command=current_command,
                previous_actions=previous_steps
            )

            logger.info(f"Next step: {json.dumps(step, indent=2)}")

            if step['completed']:
                logger.info("Command execution completed!")
                break

            # Execute the generated step with retries
            success = False
            for attempt in range(max_attempts):
                try:
                    if step['action'] == 'navigate':
                        controller.navigate(step['target'])
                        success = True
                    elif step['action'] == 'click':
                        success = controller.click_element(step['target'])
                    elif step['action'] == 'type':
                        success = controller.type_text(step['target'], step['value'])
                    elif step['action'] == 'press_enter':
                        success = controller.press_enter(step.get('target'))
                    elif step['action'] == 'wait':
                        time.sleep(int(step['value']))
                        success = True
                        
                    if success:
                        break
                        
                    logger.warning(f"Attempt {attempt+1}/{max_attempts} failed for step: {step['action']}")
                    time.sleep(1)  # Wait before retry
                    
                except Exception as e:
                    logger.error(f"Error during step execution (attempt {attempt+1}): {str(e)}")
                    time.sleep(1)  # Wait before retry

            if not success:
                logger.warning(f"Step execution failed after {max_attempts} attempts: {step['action']} - {step['description']}")
                # Try to continue with the next step anyway
                
            # Update execution state
            previous_steps.append(step)
            current_command = step['remaining_command']
            completion_status = step.get('completed', False)

            # Add delay for page changes
            time.sleep(2)

        logger.info("Command executed successfully!")
        return True

    except Exception as e:
        logger.error(f"Error during execution: {str(e)}")
        return False
    finally:
        # Don't close the browser automatically
        # This allows the user to see the results
        pass


@app.route('/interact', methods=['POST'])
def handle_command():
    data = request.get_json()

    if not data or 'command' not in data:
        return jsonify({'error': 'No command provided'}), 400

    command = data['command']
    success = execute_command(command)

    return jsonify({
        'status': 'success' if success else 'error',
        'message': f"Command {'completed' if success else 'failed'}: {command}"
    })


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')