# REVIEW.md

| # | Where | What is wrong | How you'd notice it | How you fixed it |
|---|---|---|---|---|
| 1 | `create_employee` | A pre-insert duplicate check is unsafe when two requests arrive together. | Concurrent creates with the same `emp_code`. | Enforce uniqueness in MongoDB and translate `DuplicateKeyError` to 409. |
| 2 | `list_employees` | The starter used the wrong page offset and did not count the filtered set. | Request page 1 or filter by department. | Use `(page - 1) * page_size` and filtered `count_documents`. |
| 3 | `list_employees` | Results were not deterministically sorted as required. | Insert employees in a different order and list them. | Sort by `emp_code` ascending. |
| 4 | `punch_in` | Unknown employees were not rejected correctly. | Punch in with an unknown `emp_code`. | Check the employee first and return 404. |
| 5 | `punch_in` | Epoch timestamps were not handled with the required validation and IST conversion. | Send a valid epoch value and compare the stored instant/date. | Validate epoch milliseconds and convert through UTC to IST. |
| 6 | `punch_in` | Sub-second input could affect stored values and calculations. | Send an epoch value containing milliseconds below one second. | Truncate timestamps to whole seconds before use/storage. |
| 7 | `punch_in` | Overnight shifts need a different attendance-date rule after midnight. | Use a 22:00-06:00 shift and punch in after midnight. | Derive the attendance date from the employee shift. |
| 8 | `punch_in` | `LEAVE` and `ABSENT` are not valid punch-in statuses. | Send either status to the punch-in endpoint. | Validate against `PRESENT`, `WFH`, and `ON_DUTY`. |
| 9 | `punch_in` | Check-then-insert alone cannot guarantee one record under concurrency. | Send simultaneous punch-ins for the same employee/date. | Enforce unique `(emp_code, date)` and handle duplicate insertion as 409. |
| 10 | `punch_out` | Punch-out was incomplete/missing. | Call `POST /attendance/punch-out`. | Implement selection of the open record and all required calculations. |
| 11 | `punch_out` | The most recent open punch-in at or before the supplied instant must be selected. | Use multiple records or an overnight shift. | Query the latest matching open record. |
| 12 | `punch_out` | Invalid punch-out ordering and >24-hour intervals must be rejected. | Punch out before punch-in or more than 24 hours later. | Validate both conditions and return 422. |
| 13 | `punch_out` | Two simultaneous punch-outs must not both succeed. | Send two requests against the same open record. | Use a conditional update requiring `punch_out` to still be null. |
| 14 | `regularize_attendance` | Manual regularization was missing/incomplete. | Call the PATCH endpoint. | Implement the endpoint with contract validation and recalculation. |
| 15 | `regularize_attendance` | `ABSENT` and `LEAVE` must clear punch times and cannot be sent with punch times. | Change a record to either status while supplying a punch time. | Reject conflicting input and clear the stored times for the status. |
| 16 | `regularize_attendance` | A presence status requires a punch-in on the addressed attendance date. | Remove punch-in or move it to another attendance date. | Validate the final punch-in and attendance date. |
| 17 | `regularize_attendance` | Derived fields must be recomputed after a correction. | Change punch-in/out and inspect derived values. | Recalculate late, work hours, overtime and half-day from final times. |
| 18 | `regularize_attendance` | Correction history must append rather than overwrite previous entries. | Perform multiple corrections and inspect `history`. | Use `$push` and record only fields whose values changed. |
| 19 | `list_attendance` | The starter did not keep filtering, sorting and pagination in MongoDB. | Test with a large attendance collection. | Apply filters, sort and pagination in the database and add supporting indexes. |
| 20 | Analytics | Required analytics endpoints were not implemented. | Call the `/analytics/...` routes. | Added MongoDB aggregation pipelines for monthly, department, leaderboard and trend analytics. |
| 21 | Employee monthly analytics | Monthly calculations must handle weekdays, mid-month joining, weekends and half-days. | Test a mid-month joiner and weekend/half-day records. | Calculate the metrics in MongoDB and use the stored derived fields. |
| 22 | Department summary | Headcount must count employees, not attendance-log rows, and must include employees with zero logs. | Add an employee with no logs and compare headcount with employees having several logs. | Start from employees, look up logs, calculate each employee's metrics first, then group once per employee. |
| 23 | Late leaderboard | Ranking is standard competition ranking and ties at the cutoff must be retained. | Create a tie at the requested limit. | Use `$setWindowFields` with `$rank` and filter by rank rather than array position. |
| 24 | Department trend | The result must contain every calendar day, including weekends and days with no records. | Request a range containing missing attendance days. | Generate the date range inside the MongoDB aggregation and fill missing daily values. |
| 25 | `/admin/explain/{endpoint}` | Explain must represent the same main query/pipeline as the endpoint being explained. | Compare an explain request with the corresponding analytics implementation. | Reuse the same pipeline-builder functions for the endpoint and its `executionStats` explain. |

## Checked and kept

- `health` correctly returns 200 when MongoDB responds to `ping` and 503 otherwise.
- `MONGO_URI` and `MONGO_DB` remain environment-driven.
- Late minutes retain the strict 10-minute grace period and count whole minutes from shift start.
- Work hours use elapsed time and half-up rounding to two decimals.
- Overtime uses the shift end and only counts when the overtime interval reaches 30 minutes.
- The unique attendance key enforces one record per employee and attendance date.
- Analytics read stored `work_hours`, `late_minutes`, `overtime_minutes` and `half_day` rather than recomputing them from punch timestamps.
