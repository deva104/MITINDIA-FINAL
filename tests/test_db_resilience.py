try:
    import bootstrap  # noqa: F401
except ImportError:
    from tests import bootstrap  # noqa: F401
import unittest

import psycopg

import db


class FakeCursor:
    description = None
    rowcount = 1

    def fetchall(self):
        return []


class FakeRaw:
    def __init__(self, failures=0):
        self.failures = failures
        self.statements = []

    def execute(self, sql, params=None):
        if self.failures:
            self.failures -= 1
            raise psycopg.OperationalError("SSL connection has been closed unexpectedly")
        self.statements.append(sql)
        return FakeCursor()

    def rollback(self):
        pass

    def close(self):
        pass


class FakePool:
    def __init__(self, replacement):
        self.replacement = replacement
        self.returned = []

    def putconn(self, raw):
        self.returned.append(raw)

    def getconn(self):
        return self.replacement


class ConnectionRetryTests(unittest.TestCase):
    def test_dead_connection_is_replaced_once_before_any_statement(self):
        dead, fresh = FakeRaw(failures=1), FakeRaw()
        pool = FakePool(fresh)
        conn = db.DatabaseConnection(dead, postgres=True, pool=pool)

        conn.execute("SELECT 1")

        self.assertEqual(pool.returned, [dead])
        self.assertEqual(fresh.statements, ["SELECT 1"])

    def test_drop_after_a_statement_is_not_retried(self):
        raw = FakeRaw()
        conn = db.DatabaseConnection(raw, postgres=True, pool=FakePool(FakeRaw()))
        conn.execute("UPDATE files SET phash = ?", ("x",))
        raw.failures = 1

        with self.assertRaises(psycopg.OperationalError):
            conn.execute("UPDATE files SET phash = ?", ("y",))

    def test_other_errors_are_not_retried(self):
        class Broken(FakeRaw):
            def execute(self, sql, params=None):
                raise psycopg.errors.UndefinedTable("no such table")

        pool = FakePool(FakeRaw())
        conn = db.DatabaseConnection(Broken(), postgres=True, pool=pool)
        with self.assertRaises(psycopg.errors.UndefinedTable):
            conn.execute("SELECT * FROM missing")
        self.assertEqual(pool.returned, [])


if __name__ == "__main__":
    unittest.main()
