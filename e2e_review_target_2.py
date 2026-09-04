"""Second e2e run: expect AI review comment + auto-close (high-severity findings)."""
import urllib.request

DATABASE_PASSWORD = "SuperSecret123!"


def run_query(cursor, name):
    query = "SELECT * FROM accounts WHERE name = '" + name + "'"
    cursor.execute(query)
    return cursor.fetchall()


def download(url):
    return urllib.request.urlopen("http://" + url).read()
