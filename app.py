import time
import json
import re
from pathlib import Path
from typing import Optional, List, Tuple, Dict

import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from selenium.webdriver.common.by import By
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementNotInteractableException

# -----------------------------
# Constants & URL
# -----------------------------
URL = "https://ehrms.upsdc.gov.in/ReportSummary/PublicReports/EmployeeFactSheet"

# -----------------------------
# Request / Response Models
# -----------------------------
class FormInputs(BaseModel):
    parent: str
    organisation: str
    last_field: Optional[str] = ""
    # Optional: allow adjusting overall wait time in seconds
    wait_timeout: Optional[int] = 25
    # Keep headless true in servers
    headless: Optional[bool] = True
    # Optionally persist a JSON file (disabled by default in stateless containers)
    save_json: Optional[bool] = False
    save_basename: Optional[str] = "employee_fact_sheet"

class ScrapeResponse(BaseModel):
    ok: bool
    fields: Dict[str, Dict[str, str]]
    saved_path: Optional[str] = None
    meta: Dict[str, str] = {}

# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title="UPSDC eHRMS Employee Fact Sheet Scraper",
              version="1.0.0",
              description="Scrapes the Employee Fact Sheet popup and returns parsed JSON.")

# -----------------------------
# Parsing utilities (unchanged core logic)
# -----------------------------
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
    return list(re.finditer(r'(?P<num>\d{1,2})\.\s+(?P<label>[^0-9][^0-9]*?)\s', text))

def _collapse_ws(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()

def parse_malformed_blocks(raw_text: str) -> pd.DataFrame:
    joined = _collapse_ws(raw_text)
    all_a = _anchors(joined)
    if not all_a:
        return pd.DataFrame([[None, "Raw Text", joined]], columns=["No.", "Field", "Value"])

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

# -----------------------------
# Selenium helpers (mostly unchanged)
# -----------------------------
def wait_for_select_to_have_options(select_el, min_options: int = 2, timeout: int = 15):
    end = time.time() + timeout
    while time.time() < end:
        options = select_el.find_elements(By.TAG_NAME, "option")
        if len(options) >= min_options:
            return
        time.sleep(0.25)
    raise TimeoutException("Dropdown didn't populate with enough options in time.")

def find_select_by_label_text(driver, label_text: str):
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
    selects = driver.find_elements(By.TAG_NAME, "select")
    if len(selects) >= 1 and "Parent" in label_text:
        return selects[0]
    if len(selects) >= 2 and "Organisation" in label_text:
        return selects[1]
    raise NoSuchElementException(f"Could not find select for label: {label_text}")

def find_text_input_below_second_select(driver):
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
    candidates = driver.find_elements(By.XPATH, "//button[normalize-space()='View Report' or contains(., 'View Report')] | //input[@type='button' and @value='View Report']")
    if not candidates:
        candidates = driver.find_elements(By.XPATH, "//button | //input[@type='button' or @type='submit']")
    for c in candidates:
        if c.is_displayed() and c.is_enabled():
            c.click()
            return
    raise NoSuchElementException("Couldn't find/click the 'View Report' button.")

def get_popup_root(driver, wait_timeout=20):
    wait = WebDriverWait(driver, wait_timeout)
    dialog = wait.until(EC.presence_of_element_located((
        By.XPATH,
        "//*[contains(@class,'ui-dialog') and .//span[contains(normalize-space(),'Manav Sampada Reports')]]"
    )))
    wait.until(EC.visibility_of(dialog))
    return dialog

def switch_into_report_iframe(dialog_el, driver):
    driver.switch_to.default_content()
    iframes = dialog_el.find_elements(By.XPATH, ".//iframe")
    if iframes:
        driver.switch_to.frame(iframes[0])
        return True
    return False

def scrape_popup_report(driver, save_basename="employee_fact_sheet", wait_timeout=25, save_json=False):
    wait = WebDriverWait(driver, wait_timeout)
    saved_path = None
    try:
        dialog = get_popup_root(driver, wait_timeout=wait_timeout)
        switch_into_report_iframe(dialog, driver)

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
            content = dialog

        report_text = content.text
        df = parse_malformed_blocks(report_text)

        data = {
            str(int(r["No."])): {"Field": str(r["Field"]), "Value": str(r["Value"])}
            for _, r in df.iterrows() if pd.notna(r["No."])
        }

        if save_json:
            out_json = Path(f"{save_basename}.json")
            with out_json.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            saved_path = str(out_json.resolve())

        return data, saved_path
    finally:
        driver.switch_to.default_content()

def build_driver(headless: bool = True) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    if headless:
        # The "new" headless is recommended with recent Chromes
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_argument("--start-maximized")

    # Support for containerized Chromium path (Dockerfile sets these up)
    options.binary_location = "/usr/bin/chromium"

    # service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(options=options)
    return driver

def run_scrape(inputs: FormInputs) -> Dict[str, Dict[str, str]]:
    driver = build_driver(headless=inputs.headless if inputs.headless is not None else True)
    try:
        driver.get(URL)
        wait = WebDriverWait(driver, 30)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "select")))

        parent_select_el = find_select_by_label_text(driver, "Parent :")
        wait_for_select_to_have_options(parent_select_el)
        Select(parent_select_el).select_by_visible_text(inputs.parent)

        org_select_el = find_select_by_label_text(driver, "Organisation :")
        wait_for_select_to_have_options(org_select_el)
        # Ensure the desired option is present before selecting
        wait.until(lambda d: any(opt.text.strip() == inputs.organisation for opt in org_select_el.find_elements(By.TAG_NAME, "option")))
        Select(org_select_el).select_by_visible_text(inputs.organisation)

        if inputs.last_field and str(inputs.last_field).strip():
            text_input = find_text_input_below_second_select(driver)
            try:
                text_input.clear()
            except ElementNotInteractableException:
                pass
            text_input.send_keys(inputs.last_field)

        click_view_report(driver)

        details, saved_path = scrape_popup_report(
            driver,
            save_basename=inputs.save_basename or "employee_fact_sheet",
            wait_timeout=inputs.wait_timeout or 25,
            save_json=bool(inputs.save_json),
        )

        return {
            "ok": True,
            "fields": details,
            "saved_path": saved_path,
            "meta": {
                "url": URL,
                "headless": str(inputs.headless),
            }
        }

    except (TimeoutException, NoSuchElementException) as e:
        raise HTTPException(status_code=504, detail=f"Scrape timed out or element not found: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
    finally:
        try:
            time.sleep(1)  # small buffer
            driver.quit()
        except Exception:
            pass

# -----------------------------
# Routes
# -----------------------------
@app.get("/health")
def health():
    return {"ok": True, "service": "ehrms-scraper", "version": "1.0.0"}

@app.post("/scrape", response_model=ScrapeResponse)
def scrape(inputs: FormInputs):
    result = run_scrape(inputs)
    return ScrapeResponse(
        ok=result["ok"],
        fields=result["fields"],
        saved_path=result.get("saved_path"),
        meta=result.get("meta", {})
    )

@app.get("/")
def root():
    return {"message": "UPSDC eHRMS Employee Fact Sheet Scraper. POST /scrape with JSON body to begin."}
