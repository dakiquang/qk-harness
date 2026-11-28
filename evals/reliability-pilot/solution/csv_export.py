import csv
import io


def render(records):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("name", "note"))
    writer.writerows((record["name"], record["note"]) for record in records)
    return output.getvalue()
