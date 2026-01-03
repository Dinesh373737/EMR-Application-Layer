.\appenv\Scripts\Activate.ps1
# if ($LASTEXITCODE -eq 0) {
uvicorn backend.main:app --port 3000 --reload
# }
