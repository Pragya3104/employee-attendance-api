# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for employee attendance, manual regularization and analytics.

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Set `MONGO_URI` and `MONGO_DB` in the environment before starting the application. MongoDB indexes are created automatically when the application starts.

Swagger UI is available at `/docs`.

The application code is contained in `app/main.py` as required by the assignment. Analytics are implemented as MongoDB aggregation pipelines, and `/admin/explain/{endpoint}` exposes `executionStats` for the corresponding query or pipeline.
