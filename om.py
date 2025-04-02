from flask import Flask, request, jsonify
import logging
import json
import google.generativeai as genai
import os
import time
import requests
import base64
from io import BytesIO
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import WebDriverException, TimeoutException
from flask_cors import CORS

# Initialize Flask app with CORS
app = Flask(__name__)
CORS(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure Gemini API
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY not found in environment variables")
    GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"  # Replace with your key in development
genai.configure(api_key=GEMINI_API_KEY)

### Dynamic Selector Generation
def get_dynamic_selector(driver, action=None, params=None, data_name=None):
    """
    Generate a dynamic CSS selector using JavaScript based on action or data type.
    
    Args:
        driver: Selenium WebDriver instance
        action: Type of action ('click', 'type') for automation
        params: Parameters for the action (e.g., text to type or click)
        data_name: Name of data to extract (e.g., 'headline', 'price')
    
    Returns:
        str: CSS selector or None if generation fails
    """
    js_base = """
    function getSelector(element) {
        if (element.id) return '#' + element.id;
        if (element === document.body) return element.tagName.toLowerCase();
        var ix = 0;
        var siblings = element.parentNode.childNodes;
        for (var i = 0; i < siblings.length; i++) {
            var sibling = siblings[i];
            if (sibling === element) {
                return getSelector(element.parentNode) + ' > ' + element.tagName.toLowerCase() + ':nth-child(' + (ix + 1) + ')';
            }
            if (sibling.nodeType === 1 && sibling.tagName === element.tagName) {
                ix++;
            }
        }
    }
    """

    if action == 'type':
        text = params.get('text', '').lower()
        js_script = js_base + """
        var elements = document.querySelectorAll('input, textarea');
        for (var el of elements) {
            if (el.type !== 'hidden' && el.offsetParent !== null) {
                if ('%s' && (el.placeholder.toLowerCase().includes('%s') || el.name.toLowerCase().includes('search'))) {
                    return getSelector(el);
                }
                return getSelector(el); // Fallback to first visible input
            }
        }
        return null;
        """ % (text, 'search' if 'search' in text else text.split()[0])
    elif action == 'click':
        text = params.get('text', '').lower()
        js_script = js_base + """
        var elements = document.querySelectorAll('button, a, [role="button"]');
        for (var el of elements) {
            if (el.offsetParent !== null) {
                if ('%s' && el.innerText.toLowerCase().includes('%s')) {
                    return getSelector(el);
                }
                return getSelector(el); // Fallback to first clickable
            }
        }
        return null;
        """ % (text, text.split()[0] if text else '')
    elif data_name:  # For extraction
        data_name_lower = data_name.lower()
        if 'headline' in data_name_lower or 'title' in data_name_lower:
            js_script = js_base + """
            var elements = document.querySelectorAll('h1, h2, h3');
            for (var el of elements) {
                if (el.offsetParent !== null && el.innerText.trim()) {
                    return getSelector(el);
                }
            }
            return null;
            """
        elif 'price' in data_name_lower:
            js_script = js_base + """
            var elements = document.querySelectorAll('span, div');
            for (var el of elements) {
                if (el.offsetParent !== null && el.innerText.match(/[0-9]+\\.?[0-9]*\\s*[\\$€£]/)) {
                    return getSelector(el);
                }
            }
            return null;
            """
        else:  # Generic fallback for other data
            js_script = js_base + """
            var elements = document.querySelectorAll('p, span, div');
            for (var el of elements) {
                if (el.offsetParent !== null && el.innerText.trim()) {
                    return getSelector(el);
                }
            }
            return null;
            """
    else:
        return None

    try:
        selector = driver.execute_script(js_script)
        return selector
    except Exception as e:
        logger.error(f"Dynamic selector error: {str(e)}")
        return None

### Automation Instructions Generation
def generate_automation_instructions(command):
    """
    Generate automation steps from a natural language command using Gemini API.
    
    Args:
        command (str): Natural language command (e.g., "Play 'Imagine Dragons' on YouTube")
    
    Returns:
        dict: JSON object with automation steps
    """
    try:
        model = genai.GenerativeModel('gemini-2.0-flash')
        prompt = f"""
        Convert the following natural language command into specific browser automation steps:
        Command: {command}

        Return a JSON object with the following structure:
        {{
            "steps": [
                {{
                    "action": "navigate" | "click" | "type" | "select" | "wait" | "screenshot" | "download" | "extract",
                    "params": {{
                        // Parameters specific to the action
                    }}
                }}
            ]
        }}

        Important guidelines:
        1. For YouTube commands:
           - Search input: "input#search"
           - Search button: "button#search-icon-legacy"
           - Video results: "a#video-title"
           - Play button: "button.ytp-play-button"
        2. For Google searches:
           - Search input: "input[name='q'], textarea[name='q']"
           - Search results: "div.g a" (general results), "a.sGLxge, a.bRMDJf" (image results)
        3. For music playback:
           - Always include a wait after clicking video to allow loading
           - Add an extra wait step (5000ms) after playing video
        4. For general website interaction:
           - Use "button" selectors when possible for click actions
           - Prefer form input names over generic selectors

        Only return the JSON object, nothing else.
        """

        response = model.generate_content(prompt)
        response_text = response.text
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()
        return json.loads(response_text)
    except Exception as e:
        logger.error(f"Error generating instructions: {str(e)}")
        raise

### Extraction Plan Generation
def generate_extraction_plan(command):
    """
    Generate extraction plan from a natural language command using Gemini API.
    
    Args:
        command (str): Natural language extraction command (e.g., "Extract all details from https://example.com")
    
    Returns:
        dict: JSON object with extraction plan
    """
    try:
        model = genai.GenerativeModel('gemini-2.0-flash')
        prompt = f"""
        Convert the following natural language extraction command into a structured extraction plan:
        Command: {command}

        Return a JSON object with the following structure:
        {{
            "url": "website URL to extract from",
            "selectors": {{
                "data_name1": "CSS selector for data type 1",
                "data_name2": "CSS selector for data type 2",
                ...
            }},
            "description": "Brief description of what we're extracting"
        }}

        Important enhancements:
        1. If the command contains a URL, use it directly
        2. For general "extract details" commands:
           - Include headings, paragraphs, images, and links
           - Use broad selectors to capture comprehensive data
        3. For e-commerce product pages:
           - Include product name, price, description, ratings
        4. For news articles:
           - Include title, author, date, content

        Only return the JSON object, nothing else.
        """
        response = model.generate_content(prompt)
        response_text = response.text
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()
        return json.loads(response_text)
    except Exception as e:
        logger.error(f"Error generating extraction plan: {str(e)}")
        raise

### Execute Browser Automation
def execute_browser_automation(instructions, browser_type='chrome'):
    """
    Execute automation steps using Selenium with dynamic selector fallback.
    
    Args:
        instructions (dict): Automation steps from Gemini API
        browser_type (str): Browser to use (default: 'chrome')
    
    Returns:
        dict: Results of automation execution
    """
    driver = None
    results = {"status": "success", "steps_results": [], "message": "Browser remains open for inspection"}

    try:
        if browser_type.lower() == 'chrome':
            options = webdriver.ChromeOptions()
            options.add_argument("--start-maximized")
            driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)
        else:
            return {"status": "error", "message": "Unsupported browser type"}

        driver.implicitly_wait(10)

        for step_index, step in enumerate(instructions.get('steps', [])):
            action = step.get('action')
            params = step.get('params', {})
            logger.info(f"Executing {action}: {params}")
            step_result = {"action": action, "status": "success"}

            try:
                if action == 'navigate':
                    driver.get(params['url'])
                    WebDriverWait(driver, 20).until(
                        lambda d: d.execute_script("return document.readyState") == "complete"
                    )
                    step_result["details"] = f"Navigated to {params['url']}"

                elif action == 'click':
                    selectors = [s.strip() for s in params['selector'].split(',')]
                    element = None
                    selector_used = params['selector']
                    for selector in selectors:
                        try:
                            element = WebDriverWait(driver, 5).until(
                                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                            )
                            break
                        except TimeoutException:
                            continue
                    if not element:
                        dynamic_selector = get_dynamic_selector(driver, 'click', params)
                        if dynamic_selector:
                            element = WebDriverWait(driver, 5).until(
                                EC.element_to_be_clickable((By.CSS_SELECTOR, dynamic_selector))
                            )
                            selector_used = dynamic_selector
                        else:
                            raise Exception("No clickable element found")
                    element.click()
                    step_result["details"] = f"Clicked element with selector: {selector_used}"

                elif action == 'type':
                    selectors = [s.strip() for s in params['selector'].split(',')]
                    text = params['text']
                    element = None
                    selector_used = params['selector']
                    for selector in selectors:
                        try:
                            element = WebDriverWait(driver, 5).until(
                                EC.visibility_of_element_located((By.CSS_SELECTOR, selector))
                            )
                            break
                        except TimeoutException:
                            continue
                    if not element:
                        dynamic_selector = get_dynamic_selector(driver, 'type', params)
                        if dynamic_selector:
                            element = WebDriverWait(driver, 5).until(
                                EC.visibility_of_element_located((By.CSS_SELECTOR, dynamic_selector))
                            )
                            selector_used = dynamic_selector
                        else:
                            raise Exception("No input element found")
                    element.clear()
                    element.send_keys(text)
                    step_result["details"] = f"Typed '{text}' into element with selector: {selector_used}"
                    if params.get('press_enter', False):
                        element.send_keys(Keys.RETURN)
                        WebDriverWait(driver, 20).until(
                            lambda d: d.execute_script("return document.readyState") == "complete"
                        )
                        step_result["details"] += " and pressed Enter"

                elif action == 'wait':
                    if 'time' in params:
                        time.sleep(params['time'] / 1000)
                        step_result["details"] = f"Waited for {params['time']}ms"
                    elif 'selector' in params:
                        WebDriverWait(driver, 20).until(
                            EC.visibility_of_element_located((By.CSS_SELECTOR, params['selector']))
                        )
                        step_result["details"] = f"Waited for element with selector: {params['selector']}"

                elif action == 'screenshot':
                    filename = params.get('filename', f'screenshot_{step_index}.png')
                    driver.save_screenshot(filename)
                    step_result["details"] = f"Took screenshot and saved as {filename}"
                    with open(filename, "rb") as image_file:
                        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                        step_result["image"] = encoded_string

                elif action == 'download':
                    selectors = [s.strip() for s in params['selector'].split(',')]
                    element = None
                    for selector in selectors:
                        try:
                            element = WebDriverWait(driver, 20).until(
                                EC.visibility_of_element_located((By.CSS_SELECTOR, selector)))
                            break
                        except TimeoutException:
                            continue
                    if element:
                        img_src = element.get_attribute('src')
                        if img_src:
                            filename = params.get('filename', f'downloaded_image_{step_index}.jpg')
                            response = requests.get(img_src)
                            if response.status_code == 200:
                                with open(filename, 'wb') as f:
                                    f.write(response.content)
                                encoded_string = base64.b64encode(response.content).decode('utf-8')
                                step_result["image"] = encoded_string
                                step_result["details"] = f"Downloaded image and saved as {filename}"
                            else:
                                raise Exception(f"Failed to download image: HTTP {response.status_code}")
                        else:
                            raise Exception("Image source not found")
                    else:
                        raise Exception(f"Download failed for selectors: {params['selector']}")

                elif action == 'extract':
                    selectors = params.get('selector', '').split(',')
                    data_name = params.get('data_name', f'extracted_data_{step_index}')
                    extracted_data = {}
                    for selector in selectors:
                        selector = selector.strip()
                        elements = driver.find_elements(By.CSS_SELECTOR, selector)
                        if elements:
                            texts = [e.text.strip() for e in elements if e.text.strip()]
                            if texts:
                                extracted_data[selector] = texts
                            else:
                                extracted_data[selector] = [
                                    e.get_attribute('src') or e.get_attribute('href') or
                                    e.get_attribute('alt') or e.get_attribute('title') or
                                    "Element found but no text or common attributes"
                                    for e in elements
                                ]
                    step_result["details"] = f"Extracted data using selectors: {params['selector']}"
                    step_result["extracted_data"] = {data_name: extracted_data}

                results["steps_results"].append(step_result)

            except Exception as e:
                step_result["status"] = "error"
                step_result["error"] = str(e)
                results["steps_results"].append(step_result)
                logger.error(f"Error in step {step_index} ({action}): {str(e)}")

        if any(step["status"] == "error" for step in results["steps_results"]):
            results["status"] = "partial_success"
            results["message"] = "Some steps failed, browser remains open for inspection"

        screenshot_path = "final_state.png"
        driver.save_screenshot(screenshot_path)
        with open(screenshot_path, "rb") as image_file:
            screenshot_base64 = base64.b64encode(image_file.read()).decode('utf-8')
            results["final_screenshot"] = screenshot_base64

        return results

    except Exception as e:
        logger.error(f"Automation error: {str(e)}")
        return {"status": "error", "message": str(e)}

    finally:
        if driver:
            logger.info("Browser remains open - close manually when finished")

### Execute Extraction
def execute_extraction(extraction_plan, browser_type='chrome'):
    """
    Execute data extraction using Selenium with dynamic selector fallback.
    
    Args:
        extraction_plan (dict): Extraction plan from Gemini API or manual input
        browser_type (str): Browser to use (default: 'chrome')
    
    Returns:
        dict: Results of extraction execution
    """
    driver = None
    try:
        if browser_type.lower() == 'chrome':
            options = webdriver.ChromeOptions()
            options.add_argument("--start-maximized")
            driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)
        else:
            return {"status": "error", "message": "Unsupported browser type"}

        url = extraction_plan.get('url')
        selectors = extraction_plan.get('selectors', {})
        description = extraction_plan.get('description', 'Data extraction')

        logger.info(f"Executing extraction: {description} from {url}")

        driver.get(url)
        WebDriverWait(driver, 20).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(3)  # For dynamic content

        extracted_data = {}
        for data_name, selector in selectors.items():
            selector_list = [s.strip() for s in selector.split(',')]
            data_items = []

            # Try predefined selectors
            for single_selector in selector_list:
                elements = driver.find_elements(By.CSS_SELECTOR, single_selector)
                if elements:
                    texts = [e.text.strip() for e in elements if e.text.strip()]
                    if texts:
                        data_items.extend(texts)
                    else:
                        for element in elements:
                            attr_value = (
                                element.get_attribute('src') or
                                element.get_attribute('href') or
                                element.get_attribute('alt') or
                                element.get_attribute('title')
                            )
                            if attr_value:
                                data_items.append(attr_value)

            # Fallback to dynamic selector if no data found
            if not data_items:
                dynamic_selector = get_dynamic_selector(driver, data_name=data_name)
                if dynamic_selector:
                    elements = driver.find_elements(By.CSS_SELECTOR, dynamic_selector)
                    texts = [e.text.strip() for e in elements if e.text.strip()]
                    if texts:
                        data_items.extend(texts)
                    else:
                        for element in elements:
                            attr_value = (
                                element.get_attribute('src') or
                                element.get_attribute('href') or
                                element.get_attribute('alt') or
                                element.get_attribute('title')
                            )
                            if attr_value:
                                data_items.append(attr_value)

            # Remove duplicates
            seen = set()
            extracted_data[data_name] = [x for x in data_items if not (x in seen or seen.add(x))]

        screenshot_path = "extraction_verification.png"
        driver.save_screenshot(screenshot_path)
        with open(screenshot_path, "rb") as image_file:
            screenshot_base64 = base64.b64encode(image_file.read()).decode('utf-8')

        return {
            "status": "success",
            "description": description,
            "url": url,
            "data": extracted_data,
            "screenshot": screenshot_base64
        }

    except Exception as e:
        logger.error(f"Extraction error: {str(e)}")
        return {"status": "error", "message": str(e)}

    finally:
        if driver:
            logger.info("Browser remains open with extracted data")

### Flask Endpoints
@app.route('/interact', methods=['POST'])
def interact():
    """Handle browser automation requests with support for complex commands."""
    data = request.json
    if not data or 'command' not in data:
        return jsonify({"error": "Missing command"}), 400

    try:
        instructions = generate_automation_instructions(data['command'])
        logger.info(f"Generated instructions: {json.dumps(instructions, indent=2)}")
        result = execute_browser_automation(instructions, data.get('browser', 'chrome'))
        result["original_command"] = data['command']
        result["generated_instructions"] = instructions
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error in interact endpoint: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/extract', methods=['POST'])
def extract():
    """Extract data from webpage using natural language commands or manual selectors."""
    data = request.json
    if not data:
        return jsonify({"error": "Missing request data"}), 400

    if 'command' in data:
        try:
            extraction_plan = generate_extraction_plan(data['command'])
            logger.info(f"Generated extraction plan: {json.dumps(extraction_plan, indent=2)}")
            result = execute_extraction(extraction_plan, data.get('browser', 'chrome'))
            result["original_command"] = data['command']
            result["generated_plan"] = extraction_plan
            return jsonify(result)
        except Exception as e:
            logger.error(f"Error in extract endpoint (command mode): {str(e)}")
            return jsonify({"status": "error", "message": str(e)}), 500

    elif 'url' in data and 'selectors' in data:
        url = data.get('url')
        selectors = data.get('selectors', {})
        extraction_plan = {
            "url": url,
            "selectors": selectors,
            "description": "Manual extraction with provided selectors"
        }
        try:
            result = execute_extraction(extraction_plan, data.get('browser', 'chrome'))
            return jsonify(result)
        except Exception as e:
            logger.error(f"Error in extract endpoint (legacy mode): {str(e)}")
            return jsonify({"status": "error", "message": str(e)}), 500

    else:
        return jsonify({"error": "Missing command or url/selectors"}), 400

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)