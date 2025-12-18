# EMR Application Layer

This repository contains the **Application Layer** of a microservices-based
Electronic Medical Record (EMR) system.

The Application Layer acts as the **user-facing service** that connects
authentication, data access, and extraction services into a single workflow.

---

## Architecture Role

This service:
- Handles user login and session management
- Displays dashboards and EMR summaries
- Sends medical reports for extraction
- Converts verified data into FHIR-compatible requests
- Communicates with other backend microservices via REST APIs

---

## Connected Microservices

| Service | Purpose |
|------|------|
| Data Access Service | FHIR storage & retrieval |
| Extraction Service | OCR + NLP entity extraction |
| Authentication Service | User login & role validation |

---

## Tech Stack

- FastAPI
- Jinja2 Templates
- HTML, CSS, JavaScript
- REST API communication

---

## Running the Service

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn backend.main:app --reload

