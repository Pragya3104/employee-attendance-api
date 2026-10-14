# DECISIONS.md

1. **Indexes.** I created a unique index on `employees.emp_code` for employee identity, `(department, emp_code)` for filtered employee lists, and `joined_on` for period headcount filtering. Attendance uses unique `(emp_code, date)` for the one-record-per-day guarantee, `(date, emp_code)` for the required list sort, `(date, status)` for status filtering, `(date, late_minutes, emp_code)` for late analytics, and `(punch_in, punch_out, emp_code)` for open punch-out lookup. I considered relying only on `_id`, but it would not support these business queries.

2. **Punch-in race.** Two requests can both pass the employee lookup. The unique `(emp_code, date)` index is the final guard. One insert succeeds with 201; the competing insert raises `DuplicateKeyError`, which the API converts to 409.

3. **Ties.** The leaderboard uses MongoDB standard competition ranking. If the requested limit cuts through a tied rank, every employee sharing that rank is returned. The final ordering is total late minutes descending, then `emp_code` ascending.

4. **Headcount.** Department summary starts from employees whose `joined_on` is within the month, looks up that employee's logs, calculates the employee-level metrics, and only then groups by department. Therefore an employee with zero logs contributes exactly one to headcount.

5. **100x data.** I would first benchmark the actual explain plans and query timings, then tune compound indexes around the real filter patterns. If repeated analytics became the bottleneck, I would move the expensive monthly/trend calculations to pre-aggregated reporting data while keeping the source attendance records authoritative.
