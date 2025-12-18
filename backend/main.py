from pathlib import Path
import os
import json

import requests
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI()

# ---------- Paths ----------
BASE_DIR = Path(__file__).resolve().parent

# ---------- External Microservices ----------

# Flask Data Access Service (Microservice 4)
FHIR_BASE_URL = os.getenv("FHIR_BASE_URL", "http://localhost:5004/api/fhir")
AUTH_BASE_URL = os.getenv("AUTH_BASE_URL", "http://localhost:5004/api/auth")

# Extraction Service (Microservice 1) - TODO: adjust this URL + path
EXTRACTION_BASE_URL = os.getenv("EXTRACTION_BASE_URL", "http://localhost:8001")
EXTRACTION_ENDPOINT = os.getenv(
    "EXTRACTION_ENDPOINT",
    f"{EXTRACTION_BASE_URL}/extract"  # change to the actual route used in MS-1
)

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
    if token:
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
    if not token:
        return RedirectResponse(url="/login", status_code=302)

    patients = []
    error = None

    try:
        resp = requests.get(
            f"{FHIR_BASE_URL}/Patient",
            headers=build_auth_headers(token),
            timeout=5,
        )
        if resp.status_code == 200:
            body = resp.json()
            # routes.py returns {"resources": [...], "total": ..., ...}
            patients = body.get("resources", [])
        else:
            try:
                error = resp.json().get("error", f"Failed to load patients (status {resp.status_code})")
            except Exception:
                error = f"Failed to load patients (status {resp.status_code})"
    except Exception:
        error = "Could not connect to Data Access Service. Is Microservice-4 running?"

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
    if not token:
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
    if not token:
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

@app.get("/upload-report", response_class=HTMLResponse)
async def upload_report_page(request: Request):
    """Show upload form for a report."""
    token = get_token(request)
    if not token:
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
    patient_id: str = Form(...),
    file: UploadFile = File(...)
):
    """
    Send report file to Extraction microservice (MS-1).
    For now we just show the extracted JSON as preview.
    Later we can send it onward to MS-4 as FHIR resources.
    """
    token = get_token(request)
    if not token:
        return RedirectResponse(url="/login", status_code=302)

    error = None
    result_json = None

    try:
        file_bytes = await file.read()
        files = {
            "file": (file.filename, file_bytes, file.content_type or "application/octet-stream")
        }

        # NOTE: Adjust EXTRACTION_ENDPOINT to match actual MS-1 route
        resp = requests.post(EXTRACTION_ENDPOINT, files=files, timeout=60)

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

    return templates.TemplateResponse(
        "upload.html",
        {
            "request": request,
            "error": error,
            "result_json": result_json,
            "patient_id": patient_id,
        },
    )
@app.post("/save-extracted")
async def save_extracted(request: Request, patient_data: str = Form(...)):

    token = get_token(request)
    if not token:
        return RedirectResponse(url="/login", status_code=303)

    data = json.loads(patient_data)
    headers = build_auth_headers(token)

    patient_payload = {
        "resourceType": "Patient",
        "id": data["PII"]["Patient_ID"],
        "name": [{"text": data["PII"]["Patient_Name"]}],
        "birthDate": data["PII"]["DOB"]
    }

    condition_payload = {
        "resourceType": "Condition",
        "subject": {"reference": f"Patient/{data['PII']['Patient_ID']}"},
        "code": {"text": data["Admission_Reason"]}
    }

    requests.post(
        f"{FHIR_BASE_URL}/Patient",
        headers=headers,
        json=patient_payload,
        timeout=5
    )

    requests.post(
        f"{FHIR_BASE_URL}/Condition",
        headers=headers,
        json=condition_payload,
        timeout=5
    )

    return RedirectResponse(url="/dashboard", status_code=303)

