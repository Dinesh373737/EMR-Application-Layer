from pathlib import Path
import os
import json
import jwt

import os
import requests
import google.generativeai as genai
from dotenv import load_dotenv

# Load environment variables
# Load environment variables
env_path = list(Path(__file__).resolve().parent.parents)[0] / '.env' # Parent is 'backend', parent.parent is root? No.
# BASE_DIR is .../backend. 
# We want .../.env
# Path(__file__).parent is backend. parent.parent is root.
env_path = Path(__file__).resolve().parent.parent / '.env'
load_dotenv(dotenv_path=env_path)

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI()

DEV = False

# ---------- Paths ----------
BASE_DIR = Path(__file__).resolve().parent

# ---------- External Microservices ----------

# Flask Data Access Service (Microservice 4)
FHIR_BASE_URL = os.getenv("FHIR_BASE_URL", "http://localhost:5004/api/fhir")
AUTH_BASE_URL = os.getenv("AUTH_BASE_URL", "http://localhost:5004/api/fhir/auth")

# Extraction Service (Microservice 1)
EXTRACTION_BASE_URL = os.getenv("EXTRACTION_BASE_URL", "http://localhost:8000")
EXTRACTION_ENDPOINT = os.getenv(
    "EXTRACTION_ENDPOINT",
    f"{EXTRACTION_BASE_URL}/extract"
)

MAPPING_ENDPOINT = "http://localhost:5005"  # Rohan Micro (Service 5)
ACE_ENDPOINT = "http://localhost:5001"      # Chidanad Privacy (Service 1)

import hashlib
TOKEN_COOKIE_NAME = "access_token"
SECRET_KEY = "emr-secure-key-2025"
print(f"DEBUG: App Service Secret Key SHA256: {hashlib.sha256(SECRET_KEY.encode()).hexdigest()}")

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
    token = request.cookies.get(TOKEN_COOKIE_NAME)
    if not token:
        print("DEBUG: No token in cookies")
    return token


def build_auth_headers(token: str | None) -> dict:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def get_current_user(request: Request) -> dict | None:
    token = get_token(request)
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except Exception as e:
        print(f"DEBUG: Token decode failed: {e}")
        return None


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
                  family_token = names[0].get("family", "")
                  given_list = names[0].get("given", [])
                  if given_list and len(given_list) > 0:
                      given_token = given_list[0]

        # DOB
        dob_token = patient_resource.get("birthDate", "")
        if not dob_token and "identifier" in patient_resource:
            for ident in patient_resource["identifier"]:
                if ident.get("system") == "http://privacy.service/dob-token":
                    dob_token = ident.get("value", "")
                    break
        
        # ID / Pseudonym
        pseudonym_id = patient_resource.get("pseudonymId", "")
        # ... (keep existing ID logic if needed, but usually pseudonymId is enough)
        if not pseudonym_id and "identifier" in patient_resource:
             for ident in patient_resource["identifier"]:
                  if ident.get("system") == "http://privacy.service/dob-token": continue
                  if ident.get("value"):
                       pseudonym_id = ident.get("value")
                       patient_resource["pseudonymId"] = pseudonym_id
                       break

        if not (family_token or given_token or dob_token or pseudonym_id):
            return patient_resource
            
        # 2. Build Payload for Re-identify
        payload = {
            "Document_Type": "Medical Report",
            "PII": {
                "GivenName": given_token,
                "FamilyName": family_token,
                "Name": family_token, # Legacy fallback support
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
            # 4. Update Patient Resource
            returned_given = real_pii.get("GivenName", "")
            returned_family = real_pii.get("FamilyName", "")
            
            # Legacy fallback
            returned_name = real_pii.get("Name", "")
            
            if "name" not in patient_resource:
                 patient_resource["name"] = [{"family": "", "given": []}]
            
            if len(patient_resource["name"]) == 0:
                 patient_resource["name"].append({"family": "", "given": []})

            name_entry = patient_resource["name"][0]
            
            if returned_family and not returned_family.lower().startswith("tkn_"):
                name_entry["family"] = returned_family
            
            if returned_given and not returned_given.lower().startswith("tkn_"):
                 # Assuming single given name for now
                 name_entry["given"] = [returned_given]

            # Fallback for legacy data
            if (not returned_family and not returned_given) and returned_name and not returned_name.lower().startswith("tkn_"):
                 parts = returned_name.split(" ")
                 if len(parts) > 1:
                      name_entry["family"] = parts[-1]
                      name_entry["given"] = parts[:-1]
                 else:
                      name_entry["family"] = returned_name

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
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    try:
        resp = requests.post(
            f"{AUTH_BASE_URL}/login",
            json={"username": username, "password": password},
            timeout=5
        )
        
        if resp.status_code == 200:
            data = resp.json()
            # Handle {"tokens": {"access": "..."}} or {"token": "..."}
            token = data.get("tokens", {}).get("access") or data.get("token")
            response = RedirectResponse(url="/dashboard", status_code=302)
            response.set_cookie(
                key=TOKEN_COOKIE_NAME,
                value=token,
                httponly=True,
                max_age=86400  # 1 day
            )
            return response
        else:
            try:
                error = resp.json().get("error", "Invalid credentials")
            except Exception:
                # Fallback if response is not JSON (e.g. 500 HTML page)
                error = f"Login failed ({resp.status_code}): {resp.text[:200]}"
            
    except Exception as e:
        error = f"Login service unavailable: {e}"

    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error}
    )


@app.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(TOKEN_COOKIE_NAME, path="/")
    return response


# ---------- Admin Routes ----------

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)
        
    users = []
    error = None
    try:
        token = get_token(request)
        headers = build_auth_headers(token)
        resp = requests.get(f"{AUTH_BASE_URL}/users", headers=headers, timeout=5)
        if resp.status_code == 200:
            users = resp.json()
        else:
            error = "Failed to fetch users"
    except Exception as e:
        error = f"Error: {e}"

    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "users": users, "error": error}
    )

@app.post("/admin/create-user", response_class=HTMLResponse)
async def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    can_upload: bool = Form(False)
):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)

    try:
        token = get_token(request)
        headers = build_auth_headers(token)
        payload = {
            "username": username,
            "password": password,
            "role": role,
            "can_upload": can_upload
        }
        
        resp = requests.post(
            f"{AUTH_BASE_URL}/users",
            json=payload,
            headers=headers,
            timeout=5
        )
        
        if resp.status_code == 201:
            return RedirectResponse(url="/admin", status_code=302)
        else:
            error = resp.json().get("error", "Failed to create user")
            
    except Exception as e:
        error = f"Error: {e}"

    # Verify if we need to pass existing users back on error or redirect
    return RedirectResponse(url=f"/admin?error={error}", status_code=302)

@app.post("/admin/delete-user")
async def delete_user(request: Request, user_id: int = Form(...)):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
         return RedirectResponse(url="/dashboard", status_code=302)
         
    try:
        token = get_token(request)
        headers = build_auth_headers(token)
        
        # Check if target user is 'admin'
        # We need to fetch the user list or specific user to check username. 
        # Since we only have ID, let's fetch list. Ideally we'd have GET /users/{id}
        # But 'admin' usually has ID 1 or we can forbid ID 1 blindly? 
        # Better: Fetch user by ID if endpoint exists, or just forbid ID 1 as standard.
        # Let's assume ID 1 is the main admin as seeded.
        if user_id == 1:
             return RedirectResponse(url="/admin?error=Cannot delete main admin", status_code=302)

        requests.delete(f"{AUTH_BASE_URL}/users/{user_id}", headers=headers)
    except:
        pass
        
    return RedirectResponse(url="/admin", status_code=302)

# ---------- Dashboard ----------




# ---------- Dashboard: Patient List ----------

@app.get("/dashboard", response_class=HTMLResponse)

async def dashboard(request: Request):
    current_user_obj = get_current_user(request)
    print(f"DEBUG: Dashboard User: {current_user_obj}")
    if not current_user_obj and not DEV:
        return RedirectResponse(url="/login", status_code=302)
    
    token = get_token(request)
    
    # We already have user, we can pass it down later
    # ... logic continues ...

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
            "query": None,
            "user": current_user_obj
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
        error = f"Error communicating with Data Access Service: {e}"

    # --- Generate Timeline Events ---
    timeline_events = []
    
    for c in conditions:
        # Try to find a date
        date_str = c.get("onsetDateTime") or c.get("recordedDate") or "Unknown Date"
        status = c.get("clinicalStatus", {}).get("coding", [{}])[0].get("code", "active")
        
        # Robust Title Extraction: Prefer Coding Display, then Text
        code_obj = c.get("code", {})
        title = "Unknown Condition"
        
        # 1. Try standardized coding display (best source)
        codings = code_obj.get("coding", [])
        if codings and codings[0].get("display"):
             title = codings[0].get("display")
        # 2. Fallback to free text if reasonable
        elif code_obj.get("text"):
             title = code_obj.get("text")
        # 3. Fallback to raw code
        elif codings and codings[0].get("code"):
             title = f"Condition Code: {codings[0].get('code')}"

        timeline_events.append({
            "type": "Condition",
            "date": date_str,
            "display_date": date_str[:10] if len(date_str) >= 10 else date_str,
            "title": title,
            "details": f"Status: {status}",
            "icon": "🩺", # Stethoscope
            "color": "#e3f2fd", # Light Blue
            "text_color": "#0d47a1"
        })

    for o in observations:
        date_str = o.get("effectiveDateTime") or o.get("issued") or "Unknown Date"
        
        val = "N/A"
        if "valueQuantity" in o:
             q = o["valueQuantity"]
             val = f"{q.get('value')} {q.get('unit', '')}"
        elif "valueString" in o:
             val = o["valueString"]
        elif "component" in o:
            # BP is often components
            comps = []
            for comp in o["component"]:
                code = comp.get("code", {}).get("text", "")
                q = comp.get("valueQuantity", {})
                v = f"{q.get('value', '')}{q.get('unit', '')}"
                comps.append(f"{code}: {v}")
            val = ", ".join(comps)

        timeline_events.append({
            "type": "Observation",
            "date": date_str,
            "display_date": date_str[:10] if len(date_str) >= 10 else date_str,
            "title": o.get("code", {}).get("text", "Unknown Observation"),
            "details": f"Value: {val}",
            "icon": "🔬", # Microscope
            "color": "#f3e5f5", # Light Purple
            "text_color": "#4a148c"
        })

    # Sort descending (newest first)
    timeline_events.sort(key=lambda x: x["date"] if x["date"] != "Unknown Date" else "0000", reverse=True)
    
    # Fix display dates after sorting
    for t in timeline_events:
        if t["date"] == "Unknown Date":
            t["display_date"] = "Date Not Recorded"
        else:
            # Try to format nice date YYYY-MM-DD
            t["display_date"] = t["date"][:10]

    return templates.TemplateResponse(
        "emr_summary.html",
        {
            "request": request,
            "patient": patient,
            "conditions": conditions,
            "observations": observations,
            "timeline": timeline_events,
            "error": error
        }
    )

@app.get("/api/emr/{patient_id}/graph")
def get_patient_graph(request: Request, patient_id: str):
    # Fetch data (re-using logic or making fresh calls)
    # Ideally should share logic, but for now we duplicate or separate extraction
    
    # 1. Fetch Conditions
    token = request.cookies.get(TOKEN_COOKIE_NAME)
    if not token:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
        
    headers = {"Authorization": f"Bearer {token}"}
    
    conditions = []
    try:
        c_resp = requests.get(
            f"{FHIR_SERVICE_URL}/condition",
            params={"patient": patient_id},
            headers=headers,
            timeout=5
        )
        if c_resp.status_code == 200:
             body = c_resp.json()
             if "entry" in body:
                 conditions = [e["resource"] for e in body["entry"]]
             elif "resources" in body:
                 conditions = body.get("resources", [])
    except:
        pass

    observations = []
    try:
        o_resp = requests.get(
            f"{FHIR_SERVICE_URL}/observation",
            params={"patient": patient_id},
            headers=headers,
            timeout=5
        )
        if o_resp.status_code == 200:
             body = o_resp.json()
             if "entry" in body:
                 observations = [e["resource"] for e in body["entry"]]
             elif "resources" in body:
                 observations = body.get("resources", [])
    except:
        pass

    # --- Generate Knowledge Graph Data ---
    graph_nodes = []
    graph_edges = []
    
    # Central Patient Node
    graph_nodes.append({
        "id": "patient",
        "label": "Patient",
        "shape": "box",
        "font": {"size": 20, "color": "#ffffff"},
        "color": {"background": "#2196f3", "border": "#1976d2", "highlight": "#1e88e5"}
    })

    # Conditions
    for i, c in enumerate(conditions):
        code_obj = c.get("code", {})
        title = "Unknown"
        codings = code_obj.get("coding", [])
        if codings and codings[0].get("display"):
             title = codings[0].get("display")
        elif code_obj.get("text"):
             title = code_obj.get("text")
        elif codings and codings[0].get("code"):
             title = f"Condition {codings[0].get('code')}"
        
        node_id = f"c_{i}"
        graph_nodes.append({
            "id": node_id,
            "label": title,
            "group": "condition",
            "shape": "dot",
            "color": {"background": "#ffebee", "border": "#f44336"}
        })
        graph_edges.append({
            "from": "patient",
            "to": node_id,
            "label": "Diagnosed",
            "arrows": "to",
            "color": {"color": "#ef9a9a"}
        })

    # Observations
    for i, o in enumerate(observations):
        code_obj = o.get("code", {})
        title = "Unknown"
        codings = code_obj.get("coding", [])
        if codings and codings[0].get("display"):
             title = codings[0].get("display")
        elif code_obj.get("text"):
             title = code_obj.get("text")
        
        val = ""
        if "valueQuantity" in o:
             q = o["valueQuantity"]
             val = f"{q.get('value')} {q.get('unit', '')}"
        elif "valueString" in o:
             val = o["valueString"]
        elif "component" in o:
             if o["component"]:
                 q = o["component"][0].get("valueQuantity", {})
                 val = f"{q.get('value', '')}"
        
        label = f"{title}\n{val}" if val else title

        node_id = f"o_{i}"
        graph_nodes.append({
            "id": node_id,
            "label": label,
            "group": "observation",
            "shape": "dot",
            "color": {"background": "#f3e5f5", "border": "#9c27b0"}
        })
        graph_edges.append({
            "from": "patient",
            "to": node_id,
            "label": "Measured",
            "arrows": "to",
            "color": {"color": "#ce93d8"}
        })

    return {"nodes": graph_nodes, "edges": graph_edges}

# Silence Chrome Devtools 404
@app.get("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools_silence():
    return {}


# ---------- Report Upload → Extraction Service ----------

@app.get("/search", response_class=HTMLResponse)
async def global_search(
    request: Request,
    query: str = None
):
    current_user_obj = get_current_user(request)
    if not current_user_obj and not DEV:
        return RedirectResponse(url="/login", status_code=302)

    token = get_token(request) # Still needed for headers

    headers = build_auth_headers(token)
    patients_map = {} # Use dict to deduplicate by ID
    error = None
    
    if not query:
        return RedirectResponse(url="/dashboard", status_code=302)

    try:
        search_lower = query.lower()

        # --- 0. SEARCH BY PATIENT ID (Direct Lookup) ---
        # If the query looks like a UUID or ID, try fetching directly
        try:
             # Try direct fetch
             direct_p = requests.get(
                 f"{FHIR_BASE_URL}/Patient/{query}",
                 headers=headers,
                 timeout=5
             )
             if direct_p.status_code == 200:
                  body = direct_p.json()
                  # Check if it's actually a patient resource (and not an error/OperationOutcome)
                  if body.get("resourceType") == "Patient":
                       patients_map[body["id"]] = body
        except Exception:
             pass

        # --- 1. SEARCH BY NAME (Tokenized) ---
        # Get token for the query string to search in DB
        # We need to ask ACE to tokenize the query.
        
        # Split query into potential name parts
        # Try multiple casings to ensure we match how it was stored (e.g. "Rahul" vs "rahul")
        variations = {query, query.lower(), query.title()}
        parts = query.split()
        if len(parts) > 1:
            for part in parts:
                variations.add(part)
                variations.add(part.lower())
                variations.add(part.title())
        
        phrases_to_tokenize = list(variations)
        
        search_tokens = set()
        
        for phrase in phrases_to_tokenize:
            if not phrase.strip(): continue
            try:
                 deid_resp = requests.post(
                     f"{ACE_ENDPOINT}/deidentify",
                     json={
                         "Document_Type": "Medical Report",
                         "PII": {"GivenName": phrase} 
                     },
                     timeout=2
                 )
                 if deid_resp.status_code == 200:
                      tok = deid_resp.json().get("PII", {}).get("GivenName")
                      if tok:
                          search_tokens.add(tok)
            except Exception as e:
                print(f"Error getting token for phrase '{phrase}': {e}")

        # Fetch all patients to filter
        if search_tokens:
            p_resp = requests.get(
                f"{FHIR_BASE_URL}/Patient",
                params={"_count": 1000}, # Fetch enough to scan
                headers=headers,
                timeout=5
            )
            
            if p_resp.status_code == 200:
                all_patients = p_resp.json().get("resources", [])
                for p in all_patients:
                    matched = False
                    
                    # Collect patient tokens
                    p_tokens = set()
                    if "name" in p:
                        for n in p["name"]:
                            if n.get("family"): p_tokens.add(n["family"])
                            p_tokens.update(n.get("given", []))
                            if n.get("text"): p_tokens.add(n["text"])
                    
                    # Check intersection
                    if not search_tokens.isdisjoint(p_tokens):
                        matched = True
                    
                    # If matched by token
                    if matched:
                        patients_map[p["id"]] = p

        # --- 2. SEARCH BY DISEASE (New DB Logic) ---
        try:
            # Call server-side search (avoid fetching all)
            resp = requests.get(
                f"{FHIR_BASE_URL}/Condition",
                params={"name": query, "_count": 100},
                headers=headers,
                timeout=5
            )

            if resp.status_code == 200:
                body = resp.json()
                matching_conditions = body.get("resources", [])
                
                patient_ids_from_cond = {
                    c.get("subject", {})
                     .get("reference", "")
                     .replace("Patient/", "")
                    for c in matching_conditions
                }
            else:
                 patient_ids_from_cond = set()

        except Exception as e:
            print(f"Error searching conditions: {e}")
            patient_ids_from_cond = set()

        for pid in patient_ids_from_cond:
            if pid and pid not in patients_map:
                 # Fetch if not already found via name
                try:
                    p = requests.get(
                        f"{FHIR_BASE_URL}/Patient/{pid}",
                        headers=headers,
                        timeout=5
                    )
                    if p.status_code == 200:
                        body = p.json()
                        if body.get("resourceType") == "Bundle":
                            for entry in body.get("entry", []):
                                res = entry.get("resource", {})
                                if res.get("resourceType") == "Patient":
                                    patients_map[res["id"]] = res
                                    break
                        else:
                             patients_map[body["id"]] = body
                except Exception:
                    pass

        patients = list(patients_map.values())
        
        # Re-identify all found patients
        for i, p in enumerate(patients):
            reidentify_patient(p)

    except Exception as e:
        error = f"Service error: {e}"

    print(f"DEBUG: Pre-Render User: {current_user_obj}")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "patients": patients,
            "error": error,
            "query": query,
            "user": current_user_obj # Use the user object defined at the start of the function
        }
    )



@app.get("/upload-report", response_class=HTMLResponse)
async def upload_report_page(request: Request):
    """Show upload form for a report."""
    user = get_current_user(request)
    if not user and not DEV:
        return RedirectResponse(url="/login", status_code=302)

    token = get_token(request)

    user = get_current_user(request)
    if user and not (user.get("can_upload") or user.get("role") == "admin"):
        return templates.TemplateResponse(
            "upload.html",
            {
                "request": request,
                "error": "You do not have permission to upload reports.",
                "result_json": None,
                "patient_id": "",
                "disabled": True # Add this to template to disable inputs? Or just error
            },
        )

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
    file: UploadFile = File(...),
    handwritten: bool = Form(False)
):
    """
    Send report file to Extraction microservice (MS-1).
    For now we just show the extracted JSON as preview.
    Later we can send it onward to MS-4 as FHIR resources.
    """
    token = get_token(request)
    if not token and not DEV:
        return RedirectResponse(url="/login", status_code=302)

    user = get_current_user(request)
    # Allow if user has can_upload OR if user is admin
    if user and not (user.get("can_upload") or user.get("role") == "admin"):
         return templates.TemplateResponse(
            "upload.html",
             {
                "request": request,
                "error": "You do not have permission to upload reports.",
                "result_json": None,
                "patient_id": ""
             }
         )

    error = None
    result_json = None

    try:
        file_bytes = await file.read()
        files = {
            "file": (file.filename, file_bytes, file.content_type or "application/octet-stream")
        }
        
        # Determine strict mode
        is_handwritten_str = "true" if handwritten else "false"
        
        data = {
            "use_gemini": "true",
            "use_ollama": "false",
            "is_handwritten": is_handwritten_str
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
    pii_given_name: str = Form(None),
    pii_family_name: str = Form(None),
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

    user = get_current_user(request)
    if user and not user.get("can_upload"):
        return templates.TemplateResponse(
            "review.html", 
            {"request": request, "error": "You do not have permission to save reports.", "data": {}, "image_url": None, "filename": ""}
        )

    # Reconstruct data from form or use raw_json
    try:
        data = json.loads(raw_json)
        
        # Overlay form updates (if user used the form fields)
        if "PII" not in data: data["PII"] = {}
        # Overlay form updates (if user used the form fields)
        if "PII" not in data: data["PII"] = {}
        if pii_given_name: data["PII"]["GivenName"] = pii_given_name
        if pii_family_name: data["PII"]["FamilyName"] = pii_family_name
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
                    "message": "Medical records confirmed and saved successfully!",
                    "pii": {} # result_json is not available here, handled by success page logic or removed
                }
            )
        else:
            return HTMLResponse(content=f"Error saving data: {resp.text}", status_code=500)
            
    except Exception as e:
        return HTMLResponse(content=f"Failed to process request: {str(e)}", status_code=500)


# -------------------------------------------------------------------------
# NEW: AI Summary & Reporting Endpoints
# -------------------------------------------------------------------------

@app.post("/api/emr/{patient_id}/summary")
async def generate_clinical_summary(patient_id: str, request: Request):
    """
    Generates a clinical summary for the patient using available data via Gemini AI.
    """
    token = request.cookies.get(TOKEN_COOKIE_NAME)
    headers = build_auth_headers(token)
    
    # 1. Fetch Data
    patient_data = {}
    
    try:
        # Fetch Patient
        p_resp = requests.get(f"{FHIR_BASE_URL}/Patient/{patient_id}", headers=headers, timeout=5)
        if p_resp.status_code == 200:
            patient = p_resp.json()
            reidentify_patient(patient)
            patient_data["overview"] = patient
        else:
            return {"summary": "Error: Patient not found"}
            
        # Fetch Conditions
        c_resp = requests.get(f"{FHIR_BASE_URL}/Condition", params={"patient": patient_id}, headers=headers, timeout=5)
        if c_resp.status_code == 200:
            c_body = c_resp.json()
            patient_data["conditions"] = [e["resource"] for e in c_body.get("entry", [])]
            
        # Fetch Observations
        o_resp = requests.get(f"{FHIR_BASE_URL}/Observation", params={"patient": patient_id}, headers=headers, timeout=5)
        if o_resp.status_code == 200:
            o_body = o_resp.json()
            patient_data["observations"] = [e["resource"] for e in o_body.get("entry", [])]
            
    except Exception as e:
        return {"summary": f"Error fetching data for summary: {str(e)}"}

    # 2. Call Gemini
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {"summary": "Error: GEMINI_API_KEY not configured on server."}
        
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        # Construct Prompt
        data_str = json.dumps(patient_data, default=str)
        prompt = f"""
        You are an expert clinical assistant. Summarize the following patient record for a doctor.
        Focus on:
        1. Patient demographics.
        2. Active clinical conditions and their onset.
        3. Key recent observations/vitals.
        4. Brief assessment/plan recommendation.
        
        Keep it professional, concise, and structured. Use Markdown formatting.
        
        Patient Data (FHIR JSON):
        {data_str}
        """
        
        response = model.generate_content(prompt)
        final_summary = response.text
        return {"summary": final_summary}
        
    except Exception as e:
        return {"summary": f"Error generating summary with AI: {str(e)}"}

@app.get("/emr/{patient_id}/print")
async def print_clinical_summary(patient_id: str, request: Request):
    """
    Renders a print-friendly page for the patient summary.
    """
    token = request.cookies.get(TOKEN_COOKIE_NAME)
    headers = build_auth_headers(token)
    
    patient = None
    conditions = []
    
    try:
        p_resp = requests.get(f"{FHIR_BASE_URL}/Patient/{patient_id}", headers=headers, timeout=5)
        if p_resp.status_code == 200:
            patient = p_resp.json()
            reidentify_patient(patient)
            
        c_resp = requests.get(f"{FHIR_BASE_URL}/Condition", params={"patient": patient_id}, headers=headers, timeout=5)
        if c_resp.status_code == 200:
            c_body = c_resp.json()
            conditions = [e["resource"] for e in c_body.get("entry", [])]
            
    except:
        pass
        
    return templates.TemplateResponse("emr_print.html", {
        "request": request,
        "patient": patient,
        "conditions": conditions,
        "generated_summary": "Please generate a fresh summary from the dashboard."
    })


@app.get("/api/emr/{patient_id}/graph")
async def get_patient_graph(patient_id: str, request: Request):
    """
    Returns nodes and edges for the Knowledge Graph.
    """
    token = request.cookies.get(TOKEN_COOKIE_NAME)
    headers = build_auth_headers(token)
    
    nodes = []
    edges = []
    
    patient = None
    conditions = []
    observations = []

    # 1. Fetch Data (Try Patient endpoint which might return a Bundle)
    try:
        p_resp = requests.get(f"{FHIR_BASE_URL}/Patient/{patient_id}", headers=headers, timeout=5)
        print(f"Graph Debug: Patient/Bundle Resp: {p_resp.status_code}")
        
        if p_resp.status_code == 200:
            body = p_resp.json()
            if body.get("resourceType") == "Bundle":
                print("Graph Debug: Received Bundle from Patient endpoint")
                for entry in body.get("entry", []):
                    res = entry.get("resource", {})
                    rt = res.get("resourceType")
                    if rt == "Patient":
                        patient = res
                    elif rt == "Condition":
                        conditions.append(res)
                    elif rt == "Observation":
                        observations.append(res)
            else:
                print("Graph Debug: Received direct Patient resource")
                patient = body

        # Fallback Fetch Conditions
        if not conditions:
            print("Graph Debug: Fetching conditions separately")
            c_resp = requests.get(f"{FHIR_BASE_URL}/Condition", params={"patient": patient_id}, headers=headers, timeout=5)
            if c_resp.status_code == 200:
                c_body = c_resp.json()
                if "entry" in c_body:
                    conditions = [e["resource"] for e in c_body.get("entry", [])]
                elif "resources" in c_body:
                    conditions = c_body.get("resources", [])

        # Fallback Fetch Observations
        if not observations:
             print("Graph Debug: Fetching observations separately")
             o_resp = requests.get(f"{FHIR_BASE_URL}/Observation", params={"patient": patient_id}, headers=headers, timeout=5)
             if o_resp.status_code == 200:
                o_body = o_resp.json()
                if "entry" in o_body:
                    observations = [e["resource"] for e in o_body.get("entry", [])]
                elif "resources" in o_body:
                    observations = o_body.get("resources", [])

    except Exception as e:
        print(f"Graph Error: {e}")
        return {"nodes": [], "edges": []}

    # 2. Build Graph
    # Patient Node
    if patient:
        reidentify_patient(patient)
        p_name = patient.get("name", [{'text': 'Patient'}])[0].get('text', 'Patient')
        nodes.append({"id": "Patient", "label": p_name, "group": "patient", "shape": "diamond", "size": 25, "color": "#FFC107"})
    else:
        nodes.append({"id": "Patient", "label": "Unknown", "group": "patient"})

    # Condition Nodes
    print(f"Graph Debug: Processing {len(conditions)} conditions")
    for c in conditions:
        cid = c.get("id")
        name = c.get('code', {}).get('text') or c.get('code', {}).get('coding', [{}])[0].get('display', 'Condition')
        status = c.get('clinicalStatus', {}).get('coding', [{}])[0].get('code', 'active')
        color = "#FF5252" if status == 'active' else "#4CAF50" # Red for active, Green for resolved
        
        nodes.append({"id": cid, "label": name, "group": "condition", "color": color})
        edges.append({"from": "Patient", "to": cid, "label": status})

    # Observation Nodes
    # Sort recent
    observations.sort(key=lambda x: x.get('effectiveDateTime', ''), reverse=True)
    print(f"Graph Debug: Processing {len(observations)} observations (capped at 8)")
    for o in observations[:8]:
        oid = o.get("id")
        name = o.get('code', {}).get('text') or o.get('code', {}).get('coding', [{}])[0].get('display', 'Observation')
        val = o.get('valueQuantity', {}).get('value')
        unit = o.get('valueQuantity', {}).get('unit', '')
        label = f"{name}\n{val} {unit}" if val else name
        
        nodes.append({"id": oid, "label": label, "group": "observation", "color": "#2196F3"})
        edges.append({"from": "Patient", "to": oid})

    return {"nodes": nodes, "edges": edges}

@app.get("/emr/{patient_id}/graph_view")
async def view_graph_page(patient_id: str, request: Request):
    """
    Renders the isolated graph visualizer page.
    """
    return templates.TemplateResponse("graph_view.html", {"request": request, "patient_id": patient_id})
