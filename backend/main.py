from pathlib import Path
import os
import json

import requests
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI()

DEV = True

# ---------- Paths ----------
BASE_DIR = Path(__file__).resolve().parent

# ---------- External Microservices ----------

# Flask Data Access Service (Microservice 4)
FHIR_BASE_URL = os.getenv("FHIR_BASE_URL", "http://localhost:5004/api/fhir")
AUTH_BASE_URL = os.getenv("AUTH_BASE_URL", "http://localhost:5004/api/auth")

# Extraction Service (Microservice 1)
EXTRACTION_BASE_URL = os.getenv("EXTRACTION_BASE_URL", "http://localhost:8000")
EXTRACTION_ENDPOINT = os.getenv(
    "EXTRACTION_ENDPOINT",
    f"{EXTRACTION_BASE_URL}/extract"
)

MAPPING_ENDPOINT = "http://localhost:5000"  # Rohan Micro (Service 5)
ACE_ENDPOINT = "http://localhost:8001"      # Chidanad Privacy (Service 1)

TOKEN_COOKIE_NAME = "access_token"

# ---------- Templates & Static ----------
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---------- Helper Functions ----------

def get_token(request: Request) -> str | None:
    """Read JWT access token from cookies."""
    return request.cookies.get(TOKEN_COOKIE_NAME)


def build_auth_headers(token: str | None) -> dict:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


# ---------- Root ----------

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Redirect root to login or dashboard depending on token."""
    token = get_token(request)
    if token or DEV:
        return RedirectResponse(url="/dashboard", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


# ---------- Auth: Login / Logout ----------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None}
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    """Send credentials to Microservice-4 /api/auth/login."""
    try:
        resp = requests.post(
            f"{AUTH_BASE_URL}/login",
            json={"username": username, "password": password},
            timeout=5
        )
    except Exception:
        # Data Service not reachable
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Cannot reach Data Access Service. Please check if Microservice-4 is running.",
            },
            status_code=500,
        )

    if resp.status_code != 200:
        # Invalid credentials or error
        try:
            msg = resp.json().get("error", "Login failed")
        except Exception:
            msg = "Login failed"
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": msg},
            status_code=resp.status_code,
        )

    data = resp.json()
    token = data.get("tokens", {}).get("access")

    if not token:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "No access token received from auth service."},
            status_code=500,
        )

    # Set token in cookie and redirect to dashboard
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(
        key=TOKEN_COOKIE_NAME,
        value=token,
        httponly=True,  # JS cannot read (better security)
        max_age=60 * 60 * 4,  # 4 hours
    )
    return response


@app.get("/logout")
async def logout(request: Request):
    """Clear cookie and go to login page."""
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(TOKEN_COOKIE_NAME)
    return response


# ---------- Dashboard: Patient List ----------

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    token = get_token(request)
    if not token and not DEV:
        return RedirectResponse(url="/login", status_code=302)

    patients = []
    error = None

    try:
        # Use specific resource endpoint which is standard FHIR
        resp = requests.get(
            f"{FHIR_BASE_URL}/Patient",
            params={"_count": 50},
            headers=build_auth_headers(token),
            timeout=5,
        )
        # Fallback if 405 (in case data-access wasn't updated) -> Try /search
        if resp.status_code == 405:
             resp = requests.get(
                f"{FHIR_BASE_URL}/search",
                params={"type": "Patient", "limit": 50},
                headers=build_auth_headers(token),
                timeout=5,
             )

        if resp.status_code == 200:
            body = resp.json()
            if "entry" in body:
                patients = [e["resource"] for e in body["entry"]]
            elif "resources" in body:
                patients = body.get("resources", [])
            else:
                patients = []
        else:
            patients = []
            try:
                error = resp.json().get("error", f"Error {resp.status_code}")
            except:
                error = f"Error {resp.status_code}"

    except Exception as e:
        error = f"Connection failed: {e}"

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "patients": patients,
            "error": error,
        },
    )


# ---------- Patient EMR View ----------

@app.get("/patient/{patient_id}", response_class=HTMLResponse)
async def patient_detail(request: Request, patient_id: str):
    token = get_token(request)
    if not token and not DEV:
        return RedirectResponse(url="/login", status_code=302)

    headers = build_auth_headers(token)

    patient = None
    conditions = []
    observations = []
    error = None

    try:
        # Get core patient resource
        p_resp = requests.get(f"{FHIR_BASE_URL}/Patient/{patient_id}", headers=headers, timeout=5)
        if p_resp.status_code == 200:
            patient = p_resp.json()
        else:
            try:
                error = p_resp.json().get("error", f"Failed to get patient ({p_resp.status_code})")
            except Exception:
                error = f"Failed to get patient ({p_resp.status_code})"

        # Get conditions
        c_resp = requests.get(
            f"{FHIR_BASE_URL}/Condition",
            params={"patient": patient_id},
            headers=headers,
            timeout=5
        )
        if c_resp.status_code == 200:
            conditions = c_resp.json().get("resources", [])

        # Get observations (vitals)
        o_resp = requests.get(
            f"{FHIR_BASE_URL}/Observation",
            params={"patient": patient_id},
            headers=headers,
            timeout=5
        )
        if o_resp.status_code == 200:
            observations = o_resp.json().get("resources", [])

    except Exception:
        error = "Error communicating with Data Access Service."

    return templates.TemplateResponse(
        "patient.html",
        {
            "request": request,
            "patient": patient,
            "conditions": conditions,
            "observations": observations,
            "error": error,
        },
    )


@app.get("/emr/{patient_id}", response_class=HTMLResponse)
async def emr_summary(request: Request, patient_id: str):
    token = get_token(request)
    if not token and not DEV:
        return RedirectResponse(url="/login", status_code=302)

    headers = build_auth_headers(token)

    patient = None
    conditions = []
    observations = []
    error = None

    try:
        # Patient details
        p_resp = requests.get(
            f"{FHIR_BASE_URL}/Patient/{patient_id}",
            headers=headers,
            timeout=5
        )
        if p_resp.status_code == 200:
            patient = p_resp.json()
        else:
            error = "Unable to fetch patient details"

        # Active problems (Conditions)
        c_resp = requests.get(
            f"{FHIR_BASE_URL}/Condition",
            params={"patient": patient_id},
            headers=headers,
            timeout=5
        )
        if c_resp.status_code == 200:
            conditions = c_resp.json().get("resources", [])

        # Vitals (Observations)
        o_resp = requests.get(
            f"{FHIR_BASE_URL}/Observation",
            params={"patient": patient_id},
            headers=headers,
            timeout=5
        )
        if o_resp.status_code == 200:
            observations = o_resp.json().get("resources", [])

    except Exception as e:
        error = "Error communicating with Data Access Service"

    return templates.TemplateResponse(
        "emr_summary.html",
        {
            "request": request,
            "patient": patient,
            "conditions": conditions,
            "observations": observations,
            "error": error
        }
    )

    # ---------------- RENDER TEMPLATE ----------------

    return templates.TemplateResponse(
        "emr_summary.html",
        {
            "request": request,
            "patient": patient,
            "conditions": conditions,
            "observations": observations,
            "error": error
        }
    )


# ---------- Report Upload → Extraction Service ----------

@app.get("/search/disease", response_class=HTMLResponse)
async def search_by_disease(
    request: Request,
    icd_code: str = None
):
    token = get_token(request)
    if not token and not DEV:
        return RedirectResponse(url="/login", status_code=302)

    headers = build_auth_headers(token)
    patients = []
    error = None

    if not icd_code:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "patients": [],
                "error": None,
                "query": ""
            }
        )

    try:
        resp = requests.get(
            f"{FHIR_BASE_URL}/Condition",
            params={"code": icd_code},
            headers=headers,
            timeout=5
        )

        if resp.status_code == 200:
            conditions = resp.json().get("resources", [])

            patient_ids = {
                c.get("subject", {})
                 .get("reference", "")
                 .replace("Patient/", "")
                for c in conditions
            }

            for pid in patient_ids:
                p = requests.get(
                    f"{FHIR_BASE_URL}/Patient/{pid}",
                    headers=headers,
                    timeout=5
                )
                if p.status_code == 200:
                    patients.append(p.json())

        else:
            error = "Failed to fetch conditions"

    except Exception as e:
        error = f"Service error: {e}"

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "patients": patients,
            "error": error,
            "query": icd_code
        }
    )



@app.get("/upload-report", response_class=HTMLResponse)
async def upload_report_page(request: Request):
    """Show upload form for a report."""
    token = get_token(request)
    if not token and not DEV:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse(
        "upload.html",
        {
            "request": request,
            "error": None,
            "result_json": None,
            "patient_id": "",
        },
    )


@app.post("/upload-report", response_class=HTMLResponse)
async def upload_report(
    request: Request,
    file: UploadFile = File(...)
):
    """
    Send report file to Extraction microservice (MS-1).
    For now we just show the extracted JSON as preview.
    Later we can send it onward to MS-4 as FHIR resources.
    """
    token = get_token(request)
    if not token and not DEV:
        return RedirectResponse(url="/login", status_code=302)

    error = None
    result_json = None

    try:
        file_bytes = await file.read()
        files = {
            "file": (file.filename, file_bytes, file.content_type or "application/octet-stream")
        }
        data = {
            "use_gemini": "true",
            "use_ollama": "false"
        }

        # NOTE: Adjust EXTRACTION_ENDPOINT to match actual MS-1 route
        resp = requests.post(EXTRACTION_ENDPOINT, files=files, data=data, timeout=120)

        if resp.status_code == 200:
            try:
              extracted = resp.json()
              result_json = extracted   # KEEP AS DICT
            except Exception:
               result_json = None
               error = "Extraction service returned invalid JSON"

        else:
            # Try extracting error message
            try:
                body = resp.json()
                msg = body.get("error", resp.text)
            except Exception:
                msg = resp.text
            error = f"Extraction service returned {resp.status_code}: {msg}"

    except Exception as e:
        error = f"Could not reach Extraction Service: {e}"

    # Success - Render Review Page
    # Save image for preview if possible
    image_url = None
    try:
        if file.filename:
            # Create static/uploads if not exists
            upload_dir = BASE_DIR / "static" / "uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            
            # Save file
            file_path = upload_dir / file.filename
            with open(file_path, "wb") as f:
                # Reset file cursor since we read it above
                await file.seek(0)
                f.write(await file.read())
            
            image_url = f"/static/uploads/{file.filename}"
    except Exception as e:
        print(f"Error saving preview image: {e}")

    return templates.TemplateResponse(
        "review.html",
        {
            "request": request,
            "data": result_json,
            "filename": file.filename,
            "image_url": image_url
        }
    )

@app.post("/save-extracted")
async def save_extracted(
    request: Request, 
    raw_json: str = Form(...),
    pii_name: str = Form(None),
    pii_dob: str = Form(None),
    pii_gender: str = Form(None),
    pii_id: str = Form(None),
    conditions: str = Form(None),
    medications: str = Form(None),
    dosages: str = Form(None)
):

    token = get_token(request)
    if not token and not DEV:
        return RedirectResponse(url="/login", status_code=303)

    # Reconstruct data from form or use raw_json
    try:
        data = json.loads(raw_json)
        
        # Overlay form updates (if user used the form fields)
        if "PII" not in data: data["PII"] = {}
        if pii_name: data["PII"]["Name"] = pii_name
        if pii_dob: data["PII"]["DOB"] = pii_dob
        if pii_gender: data["PII"]["Gender"] = pii_gender
        if pii_id: data["PII"]["ID"] = pii_id
        
        if conditions:
            # Handle user input which might be comma separated or newlines
            data["Disease_disorder"] = [c.strip() for c in conditions.replace('\n', ',').split(',') if c.strip()]
            
        if medications:
            data["Medication"] = [m.strip() for m in medications.replace('\n', ',').split(',') if m.strip()]
            
        if dosages:
            data["Dosage"] = [d.strip() for d in dosages.replace('\n', ',').split(',') if d.strip()]

    except json.JSONDecodeError:
        return templates.TemplateResponse(
            "review.html", 
            {"request": request, "error": "Invalid JSON format in raw data", "data": {}, "image_url": None, "filename": ""}
        )

    headers = build_auth_headers(token)

    deidentified_resp = requests.post(f'{ACE_ENDPOINT}/deidentify', headers=headers, json=data, timeout=60)
    deidentified_data = deidentified_resp.json()

    payload = {
        "document_type": data.get("Document_Type"),
        "data": deidentified_data,
    }
    resp = requests.post(f'{MAPPING_ENDPOINT}/api/v1/map/document', headers=headers, json=payload, timeout=60)
    print(resp.json())

    # Map raw -> FHIR
    bundle_map = resp.json()

    # Harmonize
    resp = requests.post(f'{MAPPING_ENDPOINT}/api/v1/harmonize', headers=headers, json=bundle_map, timeout=60)
    
    harmonized_bundle = resp.json()

    # Inject Pseudonym ID from Deidentification step (Chidanad)
    # Chidanad deidentifies "ID" in "PII" -> This is the pseudonym ID
    pseudonym_id = deidentified_data.get("PII", {}).get("ID")
    print(f"DEBUG: Extracted Pseudonym ID: {pseudonym_id}") # DEBUG LOG
    if pseudonym_id:
        harmonized_bundle["pseudonymId"] = pseudonym_id

    return templates.TemplateResponse(
        "harmonized.html",
        {
            "request": request,
            "data": data,
            "bundle": harmonized_bundle
        }
    )



@app.post("/confirm-save")
async def confirm_save(request: Request, fhir_bundle: str = Form(...)):
    """
    User confirmed the harmonized bundle.
    Send it to DB Service to be saved.
    """
    token = get_token(request)
    if not token and not DEV:
        return RedirectResponse(url="/login", status_code=303)

    try:
        bundle_data = json.loads(fhir_bundle)
        headers = build_auth_headers(token)
        
        # Send to DB Service
        resp = requests.post(
            f"{FHIR_BASE_URL}/confirm",
            headers=headers,
            json=bundle_data,
            timeout=10
        )
        
        if resp.status_code == 201:
            # Success! Redirect to dashboard or show success page
            # For now, let's redirect to dashboard with a success parameter (optional)
             return templates.TemplateResponse(
                "success.html",
                {
                    "request": request,
                    "message": "Medical records confirmed and saved successfully!"
                }
            )
        else:
            return HTMLResponse(content=f"Error saving data: {resp.text}", status_code=500)
            
    except Exception as e:
        return HTMLResponse(content=f"Failed to process request: {str(e)}", status_code=500)
