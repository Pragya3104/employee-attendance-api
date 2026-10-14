import os
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

db.employees.create_index("emp_code", unique=True)
db.employees.create_index([("department", 1), ("emp_code", 1)])
db.employees.create_index("joined_on")
db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
db.attendance_logs.create_index([("date", 1), ("status", 1)])
db.attendance_logs.create_index([("date", 1), ("late_minutes", -1), ("emp_code", 1)])
db.attendance_logs.create_index([("punch_in", -1), ("punch_out", 1), ("emp_code", 1)])

IST = timezone(timedelta(hours=5, minutes=30))
MIN_TS = 100000000000
MAX_TS = 4102444800000
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(422, "invalid date")


def parse_month(value: str) -> date:
    try:
        if len(value) != 7 or value[4] != "-":
            raise ValueError
        return datetime.strptime(value + "-01", "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(422, "invalid month")


def parse_epoch(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    if type(value) is not int or value < MIN_TS or value > MAX_TS:
        raise HTTPException(422, "invalid timestamp")
    return datetime.fromtimestamp(value / 1000, timezone.utc).astimezone(IST).replace(microsecond=0)


def round_half_up(value: float, places: int) -> float:
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def shift_time(value: str):
    try:
        h, m = value.split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return h, m
    except (ValueError, AttributeError):
        raise HTTPException(422, "invalid shift time")


def attendance_date(ts: datetime, emp: dict) -> str:
    sh, sm = shift_time(emp["shift_start"])
    eh, em = shift_time(emp["shift_end"])
    local = ts.astimezone(IST)
    if (eh, em) <= (sh, sm) and (local.hour, local.minute) < (eh, em):
        local -= timedelta(days=1)
    return local.date().isoformat()


def shift_end_dt(record_date: str, emp: dict) -> datetime:
    d = parse_date(record_date)
    sh, sm = shift_time(emp["shift_start"])
    eh, em = shift_time(emp["shift_end"])
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if (eh, em) <= (sh, sm):
        end += timedelta(days=1)
    return end


def compute_late_minutes(punch_in: datetime, shift_start: str, record_date: str) -> int:
    d = parse_date(record_date)
    h, m = shift_time(shift_start)
    start = datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
    minutes = int((punch_in - start).total_seconds() // 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round_half_up((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, emp: dict, record_date: str) -> int:
    end = shift_end_dt(record_date, emp)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def validate_page(page: int, page_size: int):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "invalid pagination")


def attendance_response(doc: dict) -> dict:
    def ts(value):
        return int(value.timestamp() * 1000) if isinstance(value, datetime) else value

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": ts(doc.get("punch_in")),
        "punch_out": ts(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [
            {
                "at": ts(h.get("at")),
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": {
                    k: {"from": ts(v.get("from")), "to": ts(v.get("to"))}
                    for k, v in h.get("changes", {}).items()
                }
            }
            for h in doc.get("history", [])
        ]
    }


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        parse_date(value)
        return value

    @field_validator("shift_end")
    @classmethod
    def valid_shift(cls, value, info):
        if info.data.get("shift_start") == value:
            raise ValueError("shift_start and shift_end must differ")
        return value


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, value):
        if value is not None and (type(value) is not int or value < MIN_TS or value > MAX_TS):
            raise ValueError("invalid timestamp")
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value not in PRESENCE:
            raise ValueError("invalid status")
        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, value):
        if value is not None and (type(value) is not int or value < MIN_TS or value > MAX_TS):
            raise ValueError("invalid timestamp")
        return value


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value is not None and value not in STATUSES:
            raise ValueError("invalid status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ts(cls, value):
        if value is not None and (type(value) is not int or value < MIN_TS or value > MAX_TS):
            raise ValueError("invalid timestamp")
        return value


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(IST).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = int(doc["created_at"].timestamp() * 1000)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    validate_page(page, page_size)
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    for item in items:
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = int(item["created_at"].timestamp() * 1000)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = parse_epoch(body.punched_at) if body.punched_at is not None else datetime.now(IST).replace(microsecond=0)
    d = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_response(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20
):
    validate_page(page, page_size)
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    if status and status not in STATUSES:
        raise HTTPException(422, "invalid status")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [attendance_response(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = parse_epoch(body.punched_at) if body.punched_at is not None else datetime.now(IST).replace(microsecond=0)
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None}, sort=[("punch_in", -1)])

    if not record:
        closed = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
        if closed and closed.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")
        raise HTTPException(404, "no punch-in found")

    punch_in_time = record["punch_in"]
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=IST)
    if ts <= punch_in_time:
        raise HTTPException(422, "punch_out must be after punch_in")
    if ts - punch_in_time > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    work_hours = compute_work_hours(punch_in_time, ts)
    overtime_minutes = compute_overtime(ts, emp, record["date"])
    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": work_hours, "overtime_minutes": overtime_minutes, "half_day": half_day}}
    )
    if result.modified_count != 1:
        raise HTTPException(409, "already punched out")

    record.update({"punch_in": punch_in_time, "punch_out": ts, "work_hours": work_hours, "overtime_minutes": overtime_minutes, "half_day": half_day})
    return attendance_response(record)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")

    old_status = record.get("status")
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")
    new_status = body.status if body.status is not None else old_status
    new_in = parse_epoch(body.punch_in) if body.punch_in is not None else old_in
    new_out = parse_epoch(body.punch_out) if body.punch_out is not None else old_out

    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT or LEAVE cannot have punch times")
        new_in = None
        new_out = None
    else:
        if new_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date(new_in, emp) != date:
            raise HTTPException(422, "punch_in must stay on attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    new_late = compute_late_minutes(new_in, emp["shift_start"], date) if new_in else 0
    if new_in and new_out:
        new_work = compute_work_hours(new_in, new_out)
        new_overtime = compute_overtime(new_out, emp, date)
        new_half = new_work < 4.50
    else:
        new_work = None
        new_overtime = 0
        new_half = False

    changes = {}
    for field, old, new in [
        ("status", old_status, new_status),
        ("punch_in", old_in, new_in),
        ("punch_out", old_out, new_out),
        ("work_hours", record.get("work_hours"), new_work),
        ("late_minutes", record.get("late_minutes", 0), new_late),
        ("overtime_minutes", record.get("overtime_minutes", 0), new_overtime),
        ("half_day", record.get("half_day", False), new_half)
    ]:
        old_value = int(old.timestamp() * 1000) if isinstance(old, datetime) else old
        new_value = int(new.timestamp() * 1000) if isinstance(new, datetime) else new
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}

    if not changes:
        raise HTTPException(422, "no changes supplied")

    history_entry = {"at": datetime.now(IST).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    result = db.attendance_logs.update_one(
        {"_id": record["_id"]},
        {"$set": {"status": new_status, "punch_in": new_in, "punch_out": new_out, "work_hours": new_work, "late_minutes": new_late, "overtime_minutes": new_overtime, "half_day": new_half}, "$push": {"history": history_entry}}
    )
    if result.modified_count != 1:
        raise HTTPException(409, "attendance was changed by another request")

    record.update({"status": new_status, "punch_in": new_in, "punch_out": new_out, "work_hours": new_work, "late_minutes": new_late, "overtime_minutes": new_overtime, "half_day": new_half})
    record["history"] = record.get("history", []) + [history_entry]
    return attendance_response(record)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")

    start = parse_month(month)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    pipeline = [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "pipeline": [{"$match": {"date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()}}}], "as": "logs"}},
        {"$set": {"start_day": {"$cond": [{"$gte": [{"$dateFromString": {"dateString": "$joined_on"}}, datetime(next_month.year, next_month.month, 1)]}, datetime(next_month.year, next_month.month, 1), {"$max": [{"$dateFromString": {"dateString": "$joined_on"}}, datetime(start.year, start.month, 1)]}]}, "end_day": datetime(next_month.year, next_month.month, 1)}},
        {"$set": {
            "working_days": {"$size": {"$filter": {"input": {"$map": {"input": {"$range": [0, {"$dateDiff": {"startDate": "$start_day", "endDate": "$end_day", "unit": "day"}}]}, "as": "n", "in": {"$dateAdd": {"startDate": "$start_day", "unit": "day", "amount": "$$n"}}}}, "as": "d", "cond": {"$in": [{"$dayOfWeek": "$$d"}, [2, 3, 4, 5, 6]]}}}},
            "present_days": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$cond": [{"$and": [{"$in": ["$$l.status", PRESENCE]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, [2, 3, 4, 5, 6]]}]}, {"$cond": [{"$eq": [{"$ifNull": ["$$l.half_day", False]}, True]}, 0.5, 1]}, 0]}}}},
            "leave_days": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}}}},
            "total_overtime_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.overtime_minutes", 0]}}}}
        }},
        {"$project": {"_id": 0, "emp_code": 1, "month": month, "working_days": 1, "present_days": 1, "leave_days": 1, "late_count": 1, "total_late_minutes": 1, "total_overtime_minutes": 1, "attendance_pct": {"$cond": [{"$eq": ["$working_days", 0]}, None, {"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}]}}}
    ]
    result = list(db.employees.aggregate(pipeline))[0]
    if result["attendance_pct"] is not None:
        result["attendance_pct"] = round_half_up(result["attendance_pct"], 4)
    return result


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    start = parse_month(month)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    match = {"joined_on": {"$lt": next_month.isoformat()}}
    if department:
        match["department"] = department

    pipeline = [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "pipeline": [{"$match": {"date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {"present": {"$in": ["$logs.status", PRESENCE]}, "weekday": {"and": []}}}
    ]
    pipeline[-1]["$set"]["weekday"] = {"$and": [{"$ne": [{"$ifNull": ["$logs.date", None]}, None]}, {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}]}
    pipeline += [
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "present_days": {"$sum": {"$cond": [{"$and": ["$present", "$weekday"]}, {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}}, "work_sum": {"$sum": {"$cond": [{"$and": ["$present", {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, "$logs.work_hours", 0]}}, "work_count": {"$sum": {"$cond": [{"$and": ["$present", {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, 1, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}}, "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}}, "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "avg_work_hours": {"$cond": [{"$gt": ["$work_count", 0]}, {"$divide": ["$work_sum", "$work_count"]}, None]}, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]
    items = list(db.employees.aggregate(pipeline))
    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start = parse_month(month)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    start = parse_date(from_)
    end = parse_date(to)
    if end < start:
        raise HTTPException(422, "to must not be before from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range must not exceed 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")

    start_dt = datetime(start.year, start.month, start.day)
    end_dt = datetime(end.year, end.month, end.day)
    days = (end - start).days + 1

    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": from_, "$lte": to}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$logs.date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$logs.status", PRESENCE]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                0
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}}
        }},
        {"$group": {
            "_id": None,
            "daily": {"$push": {"date": "$_id", "present_count": "$present_count", "late_count": "$late_count"}}
        }},
        {"$set": {"daily": {"$filter": {"input": "$daily", "as": "d", "cond": {"$ne": ["$$d.date", None]}}}}},
        {"$project": {
            "_id": 0,
            "daily": 1,
            "dates": {
                "$map": {
                    "input": {"$range": [0, days]},
                    "as": "n",
                    "in": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$n"}}
                }
            }
        }},
        {"$unwind": "$dates"},
        {"$set": {
            "date": "$dates",
            "daily_row": {
                "$arrayElemAt": [
                    {"$filter": {
                        "input": "$daily",
                        "as": "d",
                        "cond": {"$eq": ["$$d.date", {"$dateToString": {"format": "%Y-%m-%d", "date": "$dates"}}]}
                    }},
                    0
                ]
            }
        }},
        {"$set": {
            "present_count": {"$ifNull": ["$daily_row.present_count", 0]},
            "late_count": {"$ifNull": ["$daily_row.late_count", 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$date"}, [2, 3, 4, 5, 6]]}
        }},
        {"$lookup": {
            "from": "employees",
            "pipeline": [
                {"$match": {"department": department}},
                {"$project": {"_id": 0, "joined_on": 1}}
            ],
            "as": "department_employees"
        }},
        {"$set": {
            "headcount": {
                "$size": {
                    "$filter": {
                        "input": "$department_employees",
                        "as": "e",
                        "cond": {"$lte": ["$$e.joined_on", {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}}]}
                    }
                }
            }
        }},
        {"$set": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 100]},
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}
        }},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1
        }},
        {"$sort": {"date": 1}}
    ]

    items = list(db.employees.aggregate(pipeline))
    for item in items:
        if item["attendance_rate"] is not None:
            item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        if item["moving_avg_7d"] is not None:
            item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint:path}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if endpoint not in {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}:
        raise HTTPException(422, "invalid endpoint")

    if endpoint == "attendance_list":
        validate_page(page, page_size)
        if date_from:
            parse_date(date_from)
        if date_to:
            parse_date(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            if status not in STATUSES:
                raise HTTPException(422, "invalid status")
            q["status"] = status
        explain = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        start = parse_month(month)
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()}}}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        start = parse_month(month)
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        match = {"joined_on": {"$lt": next_month.isoformat()}}
        if department:
            match["department"] = department
        pipeline = [{"$match": match}]
        explain = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        start = parse_month(month)
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        pipeline = [{"$match": {"date": {"$gte": start.isoformat(), "$lt": next_month.isoformat()}, "late_minutes": {"$gt": 0}}}]
        if department:
            pipeline += [{"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}, {"$match": {"employee.department": department}}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if not department or not from_ or not to:
        raise HTTPException(422, "department, from and to are required")
    start = parse_date(from_)
    end = parse_date(to)
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "invalid date range")
    pipeline = [{"$match": {"department": department}}, {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "pipeline": [{"$match": {"date": {"$gte": from_, "$lte": to}}}], "as": "logs"}}]
    explain = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": "employees", "explain": explain}
