import timeit
import moz_sql_parser
import sqlglot
import sqlparse

n = 100

long = """
SELECT
  "e"."employee_id" AS "Employee #",
  "e"."first_name" || ' ' || "e"."last_name" AS "Name",
  "e"."email" AS "Email",
  "e"."phone_number" AS "Phone",
  TO_CHAR("e"."hire_date", 'MM/DD/YYYY') AS "Hire Date",
  TO_CHAR("e"."salary", 'L99G999D99', 'NLS_NUMERIC_CHARACTERS = ''.,'' NLS_CURRENCY = ''$''') AS "Salary",
  "e"."commission_pct" AS "Comission %",
  'works as ' || "j"."job_title" || ' in ' || "d"."department_name" || ' department (manager: ' || "dm"."first_name" || ' ' || "dm"."last_name" || ') and immediate supervisor: ' || "m"."first_name" || ' ' || "m"."last_name" AS "Current Job",
  TO_CHAR("j"."min_salary", 'L99G999D99', 'NLS_NUMERIC_CHARACTERS = ''.,'' NLS_CURRENCY = ''$''') || ' - ' || TO_CHAR("j"."max_salary", 'L99G999D99', 'NLS_NUMERIC_CHARACTERS = ''.,'' NLS_CURRENCY = ''$''') AS "Current Salary",
  "l"."street_address" || ', ' || "l"."postal_code" || ', ' || "l"."city" || ', ' || "l"."state_province" || ', ' || "c"."country_name" || ' (' || "r"."region_name" || ')' AS "Location",
  "jh"."job_id" AS "History Job ID",
  'worked from ' || TO_CHAR("jh"."start_date", 'MM/DD/YYYY') || ' to ' || TO_CHAR("jh"."end_date", 'MM/DD/YYYY') || ' as ' || "jj"."job_title" || ' in ' || "dd"."department_name" || ' department' AS "History Job Title",
  case when 1 then 1 when 2 then 2 when 3 then 3 when 4 then 4 when 5 then 5 else a(b(c + 1 * 3 % 4)) end
FROM "employees" AS e
JOIN "jobs" AS j
  ON "e"."job_id" = "j"."job_id"
LEFT JOIN "employees" AS m
  ON "e"."manager_id" = "m"."employee_id"
LEFT JOIN "departments" AS d
  ON "d"."department_id" = "e"."department_id"
LEFT JOIN "employees" AS dm
  ON "d"."manager_id" = "dm"."employee_id"
LEFT JOIN "locations" AS l
  ON "d"."location_id" = "l"."location_id"
LEFT JOIN "countries" AS c
  ON "l"."country_id" = "c"."country_id"
LEFT JOIN "regions" AS r
  ON "c"."region_id" = "r"."region_id"
LEFT JOIN "job_history" AS jh
  ON "e"."employee_id" = "jh"."employee_id"
LEFT JOIN "jobs" AS jj
  ON "jj"."job_id" = "jh"."job_id"
LEFT JOIN "departments" AS dd
  ON "dd"."department_id" = "jh"."department_id"
ORDER BY
  "e"."employee_id"
"""

short = "select 1 as a, case when 1 then 1 when 2 then 2 else 3 end as b, c from x"

def test_sqlglot(sql):
    sqlglot.parse(sql)


def test_sqlparse(sql):
    sqlparse.parse(sql)


def test_moz_sql(sql):
    moz_sql_parser.parse(sql)

print('sqlglot long', timeit.repeat(lambda: test_sqlglot(long), number=n))
print('sqlparse long', timeit.repeat(lambda: test_sqlparse(long), number=n))
print('moz_sql long', timeit.repeat(lambda: test_moz_sql(long), number=n))
print('sqlglot short', timeit.repeat(lambda: test_sqlglot(short), number=n))
print('sqlparse short', timeit.repeat(lambda: test_sqlparse(short), number=n))
print('moz_sql short', timeit.repeat(lambda: test_moz_sql(short), number=n))