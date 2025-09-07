import sys
import time
from dataclasses import dataclass
from typing import Optional, List, Tuple
import json
import re
from pathlib import Path

# Added for the new parsing logic
import pandas as pd

from selenium.webdriver.common.by import By
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementNotInteractableException


URL = "https://ehrms.upsdc.gov.in/ReportSummary/PublicReports/EmployeeFactSheet"


@dataclass
class FormInputs:
    parent: str
    organisation: str
    last_field: Optional[str] = ""   # often Employee Code / Identifier


# --- Integrated Parsing and Cleaning Logic ---

CANONICAL = {
    1: "Name",
    2: "eHRMS Code",
    3: "Father's Name",
    4: "Employee Type",
    5: "Date of Birth",
    6: "PH/DFF/ExSer",
    7: "Home District",
    8: "Seniority No.",
    9: "Cadre",
    10: "Level in Cadre",
    11: "Gender",
    12: "Appointment Date",
    13: "Service Start Date",
    14: "Confirmation Date",
    15: "Spouse eHRMS Code",
    16: "eSalary Code",
    17: "Class",
    18: "Health Status",
    19: "Date of Retirement",
    20: "Salary Office",
    21: "Current Status",
    22: "Posting Department/Directorate",
    23: "Present Posting Details",
    24: "Qualification with Specialization",
    25: "Past Posting Details",
    26: "Professional Training Completed",
    27: "Departmental Enquiry/Proceedings (if any)",
}


def _anchors(text: str):
    """Find all numbered anchors like '1. Label' in the text."""
    return list(re.finditer(r'(?P<num>\d{1,2})\.\s+(?P<label>[^0-9][^0-9]*?)\s', text))


def _collapse_ws(s: str) -> str:
    """Collapse all whitespace sequences to a single space."""
    return re.sub(r'\s+', ' ', s).strip()


def parse_malformed_blocks(raw_text: str) -> pd.DataFrame:
    """
    Accepts a raw text string that contains broken key/value pairs like:
    "1. Name MANOJ KUMAR 2. eHRMS Code..."
    and rebuilds a clean table with columns: No., Field, Value.
    """
    joined = _collapse_ws(raw_text)

    all_a = _anchors(joined)
    if not all_a:
        # Return a single row with the raw text if no anchors are found
        return pd.DataFrame([[None, "Raw Text", joined]], columns=["No.", "Field", "Value"])

    # Group contiguous runs with the same number (handles parsing errors)
    runs = []
    i = 0
    while i < len(all_a):
        j = i
        while j + 1 < len(all_a) and all_a[j+1].group('num') == all_a[i].group('num'):
            j += 1
        runs.append((i, j))
        i = j + 1

    rows: List[Tuple[int, str, str]] = []
    for ridx, (si, ei) in enumerate(runs):
        last_a = all_a[ei]
        num = int(last_a.group('num'))
        label = _collapse_ws(last_a.group('label'))
        start_val = last_a.end()
        end_val = all_a[runs[ridx+1][0]].start() if ridx + 1 < len(runs) else len(joined)
        value = _collapse_ws(joined[start_val:end_val])
        rows.append((num, label, value))

    def strip_label_prefix(val: str, label: str) -> str:
        # Cleans up values that accidentally contain the next label
        if not val:
            return val
        v = _collapse_ws(val)
        lbl = _collapse_ws(label)
        changed = True
        lbl_tokens = set(lbl.split())
        while changed and v:
            changed = False
            if v.startswith(lbl + " "):
                v = v[len(lbl)+1:].lstrip()
                changed = True
            toks = v.split()
            if toks and toks[0] in lbl_tokens:
                v = " ".join(toks[1:])
                changed = True
        return v.strip()

    rows.sort(key=lambda x: x[0])
    clean = []
    for num, lbl, val in rows:
        canon = CANONICAL.get(num, lbl)
        val = strip_label_prefix(val, canon)
        clean.append((num, canon, val))

    return pd.DataFrame(clean, columns=["No.", "Field", "Value"])


# --- Original Selenium Helper Functions ---

def wait_for_select_to_have_options(select_el, min_options: int = 2, timeout: int = 15):
    """Wait until a <select> has at least min_options options."""
    end = time.time() + timeout
    while time.time() < end:
        options = select_el.find_elements(By.TAG_NAME, "option")
        # Ignore placeholder-like options with empty values
        if len(options) >= min_options:
            return
        time.sleep(0.25)
    raise TimeoutException("Dropdown didn't populate with enough options in time.")


def find_select_by_label_text(driver, label_text: str):
    """
    Finds a <select> that follows a label/span containing the given text.
    Robust against minor DOM changes.
    """
    xpaths = [
        f"//label[normalize-space()='{label_text}']/following::select[1]",
        f"//span[normalize-space()='{label_text}']/following::select[1]",
        f"//*[self::label or self::span][contains(normalize-space(.), '{label_text}')]/following::select[1]"
    ]
    for xp in xpaths:
        try:
            return driver.find_element(By.XPATH, xp)
        except NoSuchElementException:
            continue
    # Fallback: use visual order
    selects = driver.find_elements(By.TAG_NAME, "select")
    if len(selects) >= 1 and "Parent" in label_text:
        return selects[0]
    if len(selects) >= 2 and "Organisation" in label_text:
        return selects[1]
    raise NoSuchElementException(f"Could not find select for label: {label_text}")


def find_text_input_below_second_select(driver):
    """Tries to find the single-line text input box shown in the screenshot."""
    selects = driver.find_elements(By.TAG_NAME, "select")
    if len(selects) >= 2:
        try:
            return selects[1].find_element(By.XPATH, "following::input[@type='text'][1]")
        except NoSuchElementException:
            pass
    inputs = driver.find_elements(By.XPATH, "//input[@type='text' and not(@disabled) and not(contains(@style,'display:none'))]")
    if inputs:
        return inputs[0]
    raise NoSuchElementException("Couldn't find the text input field below the Organisation dropdown.")


def click_view_report(driver):
    """Click the 'View Report' button."""
    candidates = driver.find_elements(By.XPATH, "//button[normalize-space()='View Report' or contains(., 'View Report')] | //input[@type='button' and @value='View Report']")
    if not candidates:
        candidates = driver.find_elements(By.XPATH, "//button | //input[@type='button' or @type='submit']")
    for c in candidates:
        if c.is_displayed() and c.is_enabled():
            c.click()
            return
    raise NoSuchElementException("Couldn't find/click the 'View Report' button.")


def get_popup_root(driver, wait_timeout=20):
    """
    Find the jQuery-UI style dialog that shows 'Manav Sampada Reports'.
    Returns the dialog root WebElement.
    """
    wait = WebDriverWait(driver, wait_timeout)
    dialog = wait.until(EC.presence_of_element_located((
        By.XPATH,
        "//*[contains(@class,'ui-dialog') and .//span[contains(normalize-space(),'Manav Sampada Reports')]]"
    )))
    wait.until(EC.visibility_of(dialog))
    return dialog


def switch_into_report_iframe(dialog_el, driver):
    """
    If the report is in an <iframe> within the dialog, switch to it.
    """
    driver.switch_to.default_content()
    iframes = dialog_el.find_elements(By.XPATH, ".//iframe")
    if iframes:
        driver.switch_to.frame(iframes[0])
        return True
    return False


def scrape_popup_report(driver, save_basename="employee_fact_sheet", wait_timeout=25):
    """
    Waits for the popup, scrapes the text, parses it into a clean format,
    saves to JSON, and returns the final dictionary.
    """
    wait = WebDriverWait(driver, wait_timeout)

    try:
        # 1) Find the popup container and switch into its iframe if it exists
        dialog = get_popup_root(driver, wait_timeout=wait_timeout)
        switch_into_report_iframe(dialog, driver)

        # 2) Wait for report content to be visible
        content = None
        candidates = [
            "//div[@id='dvReport']",
            "//div[contains(@class,'report')]",
            "//*[contains(@class,'ui-dialog-content') or contains(@class,'modal-body')]",
            "//body"
        ]
        for xp in candidates:
            try:
                content = wait.until(EC.presence_of_element_located((By.XPATH, xp)))
                if content and content.is_displayed():
                    break
            except TimeoutException:
                continue
        if not content:
            content = dialog # Fallback

        # 3) Get the full visible text
        report_text = content.text

        # 4) Parse the raw text into a structured DataFrame
        df = parse_malformed_blocks(report_text)

        # 5) Convert DataFrame to the desired final JSON structure
        data = {
            str(int(r["No."])): {"Field": str(r["Field"]), "Value": str(r["Value"])}
            for _, r in df.iterrows() if pd.notna(r["No."])
        }

        # 6) Save the clean data to a JSON file
        out_json = Path(f"{save_basename}.json")
        with out_json.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"Scraped and processed {len(data)} fields.")
        print(f"Saved JSON -> {out_json.resolve()}")

        return data

    finally:
        # Always switch back to the main page
        driver.switch_to.default_content()


def fill_form(inputs: FormInputs, headless: bool = False):
    options = webdriver.ChromeOptions()
    
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--start-maximized")
    
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    try:
        driver.get(URL)
        wait = WebDriverWait(driver, 20)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "select")))

        # 1) Parent
        parent_select_el = find_select_by_label_text(driver, "Parent :")
        wait_for_select_to_have_options(parent_select_el)
        Select(parent_select_el).select_by_visible_text(inputs.parent)

        # 2) Organisation
        org_select_el = find_select_by_label_text(driver, "Organisation :")
        wait_for_select_to_have_options(org_select_el)
        wait.until(lambda d: any(opt.text.strip() == inputs.organisation for opt in org_select_el.find_elements(By.TAG_NAME, "option")))
        Select(org_select_el).select_by_visible_text(inputs.organisation)

        # 3) Optional Text Box
        if inputs.last_field and str(inputs.last_field).strip():
            text_input = find_text_input_below_second_select(driver)
            try:
                text_input.clear()
            except ElementNotInteractableException:
                pass
            text_input.send_keys(inputs.last_field)

        # 4) Click View Report
        click_view_report(driver)

        try:
            # 5) Scrape and process the results
            details = scrape_popup_report(driver, save_basename="employee_fact_sheet")
            print("\n--- Scraped Data (Sample) ---")
            for k, v in list(details.items())[:8]:
                print(f"{k}. {v['Field']}: {v['Value']}")
            print("--------------------------")

        except TimeoutException:
            print("Popup didn’t load in time; try increasing the timeout.")
        except NoSuchElementException as e:
            print(f"Could not locate expected elements: {e}")

        print("\nForm submitted. Check the browser window for the report (if not headless).")
        if headless:
            print("You ran headless=True; to debug, run with headless=False.")

    finally:
        # Keep the browser open for a few seconds to inspect
        time.sleep(5)
        driver.quit()


def prompt_user_inputs() -> FormInputs:
    print("\n=== UPSDC Employee Fact Sheet – Input ===")
    parent = input("Enter Parent (dropdown text exactly as shown on site): ").strip()
    organisation = input("Enter Organisation (dropdown text exactly as shown on site): ").strip()
    last_field = input("Enter the text box value below Organisation (e.g., Employee Code) [leave blank if none]: ").strip()
    return FormInputs(parent=parent, organisation=organisation, last_field=last_field)


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        parent_arg = sys.argv[1]
        org_arg = sys.argv[2]
        last_field_arg = sys.argv[3] if len(sys.argv) >= 4 else ""
        form_inputs = FormInputs(parent=parent_arg, organisation=org_arg, last_field=last_field_arg)
    else:
        form_inputs = prompt_user_inputs()

    # Set headless=False to see the browser; True for silent automation
    fill_form(form_inputs, headless=True)