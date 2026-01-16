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

MAPPING_ENDPOINT = "http://localhost:5005"  # Rohan Micro (Service 5)
ACE_ENDPOINT = "http://localhost:5001"      # Chidanad Privacy (Service 1)

TOKEN_COOKIE_NAME = "access_token"

# ---------- Templates & Static ----------
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Add 'match' test for regex filtering in templates
import re
def filter_match(value, pattern):
    if not isinstance(value, str):
        return False
    return bool(re.search(pattern, value, re.IGNORECASE))

templates.env.tests["match"] = filter_match


# ---------- Helper Functions ----------

def get_token(request: Request) -> str | None:
    """Read JWT access token from cookies."""
    return request.cookies.get(TOKEN_COOKIE_NAME)


def build_auth_headers(token: str | None) -> dict:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def reidentify_patient(patient_resource: dict):
    """
    Helper to call Privacy Service (ACE) to re-identify patient details.
    """
    if not patient_resource:
        return patient_resource

    # 1. Extract potential tokens
    try:
        # Name: family (last name) is often where we store the full name token if single field
        name_token = ""
        if "name" in patient_resource:
             names = patient_resource["name"]
             if isinstance(names, list) and len(names) > 0:
                  # Check family
                  fam = names[0].get("family", "")
                  if fam and fam.lower().startswith("tkn_"):
                      name_token = fam
                  
                  # Check given
                  if not name_token and "given" in names[0]:
                      for g in names[0]["given"]:
                          if g and g.lower().startswith("tkn_"):
                              name_token = g
                              break
                  
                  # Check text
                  if not name_token and "text" in names[0]:
                      txt = names[0]["text"]
                      if txt and txt.lower().startswith("tkn_"):
                          name_token = txt

        # DOB
        dob_token = patient_resource.get("birthDate", "")
        if not dob_token and "identifier" in patient_resource:
            for ident in patient_resource["identifier"]:
                if ident.get("system") == "http://privacy.service/dob-token":
                    dob_token = ident.get("value", "")
                    break

        # ID / Pseudonym
        # We might have injected 'pseudonymId' in DB Service response
        pseudonym_id = patient_resource.get("pseudonymId", "")
        
        # If no explicit pseudonymId, maybe it's in identifier
        if not pseudonym_id and "identifier" in patient_resource:
            for ident in patient_resource["identifier"]:
                 # Skip DOB token identifier
                 if ident.get("system") == "http://privacy.service/dob-token":
                     continue
                 if ident.get("value"):
                     pseudonym_id = ident.get("value")
                     # Inject into resource for UI usage
                     patient_resource["pseudonymId"] = pseudonym_id
                     break

        if not (name_token or dob_token or pseudonym_id):
            return patient_resource
            
        # 2. Build Payload for Re-identify
        payload = {
            "Document_Type": "Medical Report",
            "PII": {
                "Name": name_token,
                "DOB": dob_token,
                "ID": pseudonym_id
            }
        }

        # 3. Call Service
        resp = requests.post(f"{ACE_ENDPOINT}/reidentify", json=payload, timeout=5)
        
        # DEBUG LOGGING start
        try:
            with open("reid_debug.log", "a") as f:
                f.write(f"\n--- Patient ID: {patient_resource.get('id')} ---\n")
                f.write(f"Sent Payload: {json.dumps(payload)}\n")
                if resp.status_code == 200:
                    f.write(f"Received Response: {json.dumps(resp.json())}\n")
                else:
                    f.write(f"Error Response: {resp.status_code} - {resp.text}\n")
        except:
            pass
        # DEBUG LOGGING end

        if resp.status_code == 200:
            real_pii = resp.json().get("PII", {})
            
            # 4. Update Patient Resource
            returned_name = real_pii.get("Name", "")
            if returned_name and not returned_name.lower().startswith("tkn_"):
                if "name" in patient_resource and len(patient_resource["name"]) > 0:
                    parts = returned_name.split(" ")
                    if len(parts) > 1:
                         # Heuristic: Last token is family, rest is given
                         patient_resource["name"][0]["family"] = parts[-1]
                         patient_resource["name"][0]["given"] = parts[:-1]
                    else:
                         patient_resource["name"][0]["family"] = returned_name
                         patient_resource["name"][0]["given"] = []

            returned_dob = real_pii.get("DOB", "")
            if returned_dob and not returned_dob.lower().startswith("tkn_"):
                patient_resource["birthDate"] = returned_dob
            
    except Exception as e:
        print(f"Re-identification warning: {e}")

    return patient_resource


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
            
            # Re-identify all patients
            for i, p in enumerate(patients):
                patients[i] = reidentify_patient(p)
            # print(patients)
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
            reidentify_patient(patient)
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
        # Patient details - db_service returns a Bundle with Patient + related resources
        p_resp = requests.get(
            f"{FHIR_BASE_URL}/Patient/{patient_id}",
            headers=headers,
            timeout=5
        )
        if p_resp.status_code == 200:
            body = p_resp.json()
            
            # Handle both Bundle and direct Resource responses
            if body.get("resourceType") == "Bundle":
                # Extract Patient and Conditions from Bundle entries
                for entry in body.get("entry", []):
                    res = entry.get("resource", {})
                    res_type = res.get("resourceType")
                    if res_type == "Patient":
                        patient = res
                    elif res_type == "Condition":
                        conditions.append(res)
                    elif res_type == "Observation":
                        observations.append(res)
            else:
                # Direct resource response
                patient = body
            
            if patient:
                reidentify_patient(patient)
        else:
            error = "Unable to fetch patient details"

        # If conditions weren't in the bundle, fetch them separately
        if not conditions:
            c_resp = requests.get(
                f"{FHIR_BASE_URL}/Condition",
                params={"patient": patient_id},
                headers=headers,
                timeout=5
            )
            if c_resp.status_code == 200:
                c_body = c_resp.json()
                if "entry" in c_body:
                    conditions = [e["resource"] for e in c_body["entry"]]
                elif "resources" in c_body:
                    conditions = c_body.get("resources", [])

        # If observations weren't in the bundle, fetch them separately
        if not observations:
            o_resp = requests.get(
                f"{FHIR_BASE_URL}/Observation",
                params={"patient": patient_id},
                headers=headers,
                timeout=5
            )
            if o_resp.status_code == 200:
                o_body = o_resp.json()
                if "entry" in o_body:
                    observations = [e["resource"] for e in o_body["entry"]]
                elif "resources" in o_body:
                    observations = o_body.get("resources", [])

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
    icd_code: str = None,
    disease_name: str = None
):
    token = get_token(request)
    if not token and not DEV:
        return RedirectResponse(url="/login", status_code=302)

    headers = build_auth_headers(token)
    patients = []
    error = None
    query = icd_code or disease_name or ""

    # If no search query, redirect to dashboard (show all patients)
    if not icd_code and not disease_name:
        return RedirectResponse(url="/dashboard", status_code=302)

    try:
        # Build search params - search by code or by text in code.text
        search_term = icd_code or disease_name
        
        resp = requests.get(
            f"{FHIR_BASE_URL}/Condition",
            headers=headers,
            timeout=5
        )

        if resp.status_code == 200:
            all_conditions = resp.json().get("resources", [])
            
            # Filter conditions that match the search term (case-insensitive)
            matching_conditions = []
            search_lower = search_term.lower()
            
            for c in all_conditions:
                # Check ICD code
                codings = c.get("code", {}).get("coding", [])
                for coding in codings:
                    if search_lower in coding.get("code", "").lower():
                        matching_conditions.append(c)
                        break
                    if search_lower in coding.get("display", "").lower():
                        matching_conditions.append(c)
                        break
                else:
                    # Check code.text (disease name)
                    code_text = c.get("code", {}).get("text", "")
                    if search_lower in code_text.lower():
                        matching_conditions.append(c)

            patient_ids = {
                c.get("subject", {})
                 .get("reference", "")
                 .replace("Patient/", "")
                for c in matching_conditions
            }

            for pid in patient_ids:
                if pid:
                    p = requests.get(
                        f"{FHIR_BASE_URL}/Patient/{pid}",
                        headers=headers,
                        timeout=5
                    )
                    if p.status_code == 200:
                        body = p.json()
                        # Handle Bundle response
                        if body.get("resourceType") == "Bundle":
                            for entry in body.get("entry", []):
                                res = entry.get("resource", {})
                                if res.get("resourceType") == "Patient":
                                    patients.append(res)
                                    break
                        else:
                            patients.append(body)

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
            "query": query
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
    dosages: str = Form(None),
    doctor: str = Form(None),
    department: str = Form(None),
    admission_reason: str = Form(None),
    outcome: str = Form(None),
    procedures: str = Form(None),
    instructions: str = Form(None)
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
        if pii_dob: 
            data["PII"]["DOB"] = pii_dob
        if pii_gender: data["PII"]["Gender"] = pii_gender
        if pii_id: data["PII"]["ID"] = pii_id
        
        # Simple text fields
        if doctor: data["Doctor"] = doctor
        if department: data["Department"] = department
        if admission_reason: data["Admission_Reason"] = admission_reason
        if outcome: data["Outcome"] = outcome

        # List fields (comma or newline separated)
        if conditions:
            data["Disease_disorder"] = [c.strip() for c in conditions.replace('\n', ',').split(',') if c.strip()]
            
        if medications:
            data["Medication"] = [m.strip() for m in medications.replace('\n', ',').split(',') if m.strip()]
            
        if dosages:
            data["Dosage"] = [d.strip() for d in dosages.replace('\n', ',').split(',') if d.strip()]

        if procedures:
            data["Procedure"] = [p.strip() for p in procedures.replace('\n', ',').split(',') if p.strip()]

        if instructions:
            # Instructions are better split by newline
            data["Instructions"] = [i.strip() for i in instructions.split('\n') if i.strip()]

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

    # Check mapping response
    if resp.status_code != 200:
        error_msg = resp.json().get("error", "Mapping failed")
        return templates.TemplateResponse(
            "review.html", 
            {"request": request, "error": f"Mapping Error: {error_msg}", "data": data, "image_url": None, "filename": ""}
        )

    # Map raw -> FHIR
    bundle_map = resp.json()
    
    # Verify it's a valid bundle before harmonizing
    if bundle_map.get("resourceType") != "Bundle":
        return templates.TemplateResponse(
            "review.html", 
            {"request": request, "error": "Mapping did not return a valid FHIR Bundle", "data": data, "image_url": None, "filename": ""}
        )

    # Harmonize
    resp = requests.post(f'{MAPPING_ENDPOINT}/api/v1/harmonize', headers=headers, json=bundle_map, timeout=60)
    
    # Check harmonization response
    if resp.status_code != 200:
        error_msg = resp.json().get("error", "Harmonization failed")
        return templates.TemplateResponse(
            "review.html", 
            {"request": request, "error": f"Harmonization Error: {error_msg}", "data": data, "image_url": None, "filename": ""}
        )
    
    harmonized_bundle = resp.json()

    # Inject Pseudonym ID from Deidentification step (Chidanad)
    # Chidanad deidentifies "ID" in "PII" -> This is the pseudonym ID
    pseudonym_id = deidentified_data.get("PII", {}).get("ID")
    if pseudonym_id:
        harmonized_bundle["pseudonymId"] = pseudonym_id

    # DO NOT re-identify here. The user needs to verify the DE-IDENTIFIED bundle before saving.
    # If we re-identify here, we risk saving clear text PII to the FHIR store.
    # The dashboard will handle re-identification for display.
    
    # if "entry" in harmonized_bundle:
    #     for entry in harmonized_bundle["entry"]:
    #         res = entry.get("resource", {})
    #         if res.get("resourceType") == "Patient":
    #             # Ensure it has pseudonymId if missing (helper usually looks for it in resource)
    #             if pseudonym_id and "pseudonymId" not in res:
    #                 res["pseudonymId"] = pseudonym_id
    #             
    #             reidentify_patient(res)

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
