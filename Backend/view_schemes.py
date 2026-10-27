import sqlite3

conn = sqlite3.connect("PAANGBA.db")
cursor = conn.cursor()

cursor.execute("SELECT * FROM scheme_records LIMIT 10")

rows = cursor.fetchall()

print("\nScheme Records")
print("========================")

for row in rows:
    print(row)

conn.close()