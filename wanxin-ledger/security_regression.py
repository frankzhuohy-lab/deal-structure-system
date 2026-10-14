import os
import sqlite3
import tempfile
import unittest
import json
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import app as ledger


class SecurityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        ledger.DB_PATH = os.path.join(self.temp_dir.name, "test.db")
        ledger.REPORT_ATTACHMENT_DIR = os.path.join(self.temp_dir.name, "attachments")
        ledger.INITIAL_CREDENTIALS_PATH = os.path.join(self.temp_dir.name, "credentials.txt")
        ledger._db_initialized = False
        ledger._ai_backend_health["unavailable_until"] = 0.0
        ledger._ai_backend_health["last_error"] = ""
        ledger._rate_limit_events.clear()
        ledger._login_failures.clear()
        ledger.ensure_db_initialized()

        conn = sqlite3.connect(ledger.DB_PATH)
        conn.execute(
            "UPDATE roles SET username=?, password_hash=? WHERE role_key='leader'",
            ("leader", generate_password_hash("Leader-Test-Password-2026")),
        )
        conn.execute(
            "UPDATE roles SET username=?, password_hash=? WHERE role_key='staff'",
            ("staff", generate_password_hash("Staff-Test-Password-2026")),
        )
        conn.commit()
        conn.close()

        ledger.app.config.update(TESTING=True)
        self.client = ledger.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def login(self, username, password):
        response = self.client.post(
            "/api/login",
            json={"username": username, "password": password},
        )
        data = response.get_json()
        return response, data

    def test_authentication_csrf_and_role_permissions(self):
        self.assertEqual(self.client.get("/api/customers").status_code, 401)
        self.assertEqual(self.client.get("/api/login/roles").status_code, 401)
        login_page = self.client.get("/login").get_data(as_text=True)
        self.assertIn('id="username"', login_page)
        self.assertNotIn('id="roleSelect"', login_page)

        response, _ = self.login("leader", "wx2026ledger")
        self.assertEqual(response.status_code, 401)

        response, _ = self.login("staff", "Leader-Test-Password-2026")
        self.assertEqual(response.status_code, 401)

        ledger._login_failures.clear()
        response, data = self.login("STAFF", "Staff-Test-Password-2026")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["user"]["username"], "staff")
        self.client.post(
            "/api/logout",
            headers={"X-CSRF-Token": data["csrf_token"]},
        )

        response, data = self.login("staff", "Staff-Test-Password-2026")
        self.assertEqual(response.status_code, 200)
        token = data["csrf_token"]

        # 所有非安全方法都必须携带 CSRF token。
        self.assertEqual(
            self.client.post("/api/calc/repayment", json={
                "principal": 100000,
                "annual_rate": 7.2,
                "months": 12,
                "method": "equal_installment",
            }).status_code,
            403,
        )

        # 非豁免 mutation 端点无 CSRF token → 403
        self.assertEqual(
            self.client.post("/api/customers", json={"name": "CSRF Test"}).status_code,
            403,
        )

        csrf_headers = {"X-CSRF-Token": token}
        self.assertEqual(
            self.client.post("/api/customers", json={"name": "CSRF Test OK"}, headers=csrf_headers).status_code,
            200,
        )

        self.assertEqual(
            self.client.post(
                "/api/calc/repayment",
                json={
                    "principal": 100000,
                    "annual_rate": 7.2,
                    "months": 12,
                    "method": "equal_installment",
                },
                headers=csrf_headers,
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.delete("/api/customers/c001", headers=csrf_headers).status_code,
            403,
        )
        self.assertEqual(
            self.client.put("/api/roles", json={"roles": []}, headers=csrf_headers).status_code,
            403,
        )

    def test_password_reset_invalidates_existing_role_sessions(self):
        staff_client = ledger.app.test_client()
        leader_client = ledger.app.test_client()

        staff_response = staff_client.post(
            "/api/login",
            json={"username": "staff", "password": "Staff-Test-Password-2026"},
        )
        self.assertEqual(staff_response.status_code, 200)

        leader_response = leader_client.post(
            "/api/login",
            json={"username": "leader", "password": "Leader-Test-Password-2026"},
        )
        self.assertEqual(leader_response.status_code, 200)
        leader_token = leader_response.get_json()["csrf_token"]

        new_password = "Staff-New-Password-2026"
        reset_response = leader_client.put(
            "/api/roles/r4/password",
            json={"new_password": new_password},
            headers={"X-CSRF-Token": leader_token},
        )
        self.assertEqual(reset_response.status_code, 200)

        expired_response = staff_client.get("/api/session")
        self.assertEqual(expired_response.status_code, 401)
        self.assertEqual(expired_response.get_json()["code"], "SESSION_EXPIRED")

        relogin_response = staff_client.post(
            "/api/login",
            json={"username": "staff", "password": new_password},
        )
        self.assertEqual(relogin_response.status_code, 200)

    def test_payment_schedule_replacement_rolls_back_and_releases_savepoint(self):
        conn = ledger.get_db()
        try:
            conn.execute(
                "INSERT INTO customers (id, name) VALUES (?, ?)",
                ("c_schedule", "Schedule Test Customer"),
            )
            conn.execute(
                """INSERT INTO projects
                   (id, project_name, customer_id, status)
                   VALUES (?, ?, ?, ?)""",
                ("p_schedule", "Schedule Test Project", "c_schedule", "active"),
            )
            conn.execute(
                """INSERT INTO payment_schedules
                   (id, project_id, period_number, due_date, total_amount, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                ("pay_original", "p_schedule", 1, "2026-01-01", 1234, "pending"),
            )
            conn.commit()

            with patch(
                "app.project_payment_schedule",
                side_effect=RuntimeError("schedule generation failed"),
            ):
                with self.assertRaises(RuntimeError):
                    ledger.replace_project_payment_schedule(
                        conn, "p_schedule", 100000, 0.06, 12,
                        "2026-01-01", "equal_installment",
                    )

            self.assertFalse(conn.in_transaction)
            rows = conn.execute(
                "SELECT id, total_amount FROM payment_schedules WHERE project_id=?",
                ("p_schedule",),
            ).fetchall()
            self.assertEqual(
                [(row["id"], row["total_amount"]) for row in rows],
                [("pay_original", 1234.0)],
            )
        finally:
            conn.close()

    def test_project_and_payment_status_history_guards(self):
        response, data = self.login("leader", "Leader-Test-Password-2026")
        self.assertEqual(response.status_code, 200)
        headers = {"X-CSRF-Token": data["csrf_token"]}

        conn = sqlite3.connect(ledger.DB_PATH)
        conn.execute(
            "INSERT INTO customers (id, name) VALUES (?, ?)",
            ("c_history", "History Guard Customer"),
        )
        conn.execute(
            """INSERT INTO projects
               (id, project_name, customer_id, investment_amount, annual_rate,
                loan_term_months, repayment_method, start_date, end_date, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "p_history", "Settled Project", "c_history", 100000, 0.06,
                12, "等额本息", "2025-01-01", "2026-01-01", "settled",
            ),
        )
        conn.execute(
            """INSERT INTO payment_schedules
               (id, project_id, period_number, due_date, total_amount, status, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("pay_history", "p_history", 1, "2025-02-01", 9000, "paid", "original"),
        )
        conn.commit()
        conn.close()

        base_payload = {
            "project_name": "Settled Project Updated",
            "customer_id": "c_history",
            "investment_amount": 100000,
            "annual_rate": 0.06,
            "loan_term_months": 12,
            "repayment_method": "equal_installment",
            "start_date": "2025-01-01",
            "end_date": "2026-01-01",
            "status": "settled",
            "notes": "metadata correction",
        }
        metadata_response = self.client.put(
            "/api/projects/p_history", json=base_payload, headers=headers
        )
        self.assertEqual(metadata_response.status_code, 200)

        conn = sqlite3.connect(ledger.DB_PATH)
        payment = conn.execute(
            "SELECT id, status, notes FROM payment_schedules WHERE id='pay_history'"
        ).fetchone()
        self.assertEqual(payment, ("pay_history", "paid", "original"))
        conn.close()

        restore_response = self.client.put(
            "/api/projects/p_history",
            json={**base_payload, "status": "active"},
            headers=headers,
        )
        self.assertEqual(restore_response.status_code, 409)
        self.assertEqual(restore_response.get_json()["code"], "SETTLED_IMMUTABLE")

        financial_change_response = self.client.put(
            "/api/projects/p_history",
            json={**base_payload, "annual_rate": 0.07},
            headers=headers,
        )
        self.assertEqual(financial_change_response.status_code, 409)
        self.assertEqual(
            financial_change_response.get_json()["code"], "PAYMENT_HISTORY_LOCKED"
        )

        paid_to_pending = self.client.put(
            "/api/payments/pay_history",
            json={
                "project_id": "p_history",
                "payment_date": "2025-02-01",
                "total_amount": 9000,
                "status": "pending",
                "notes": "should fail",
            },
            headers=headers,
        )
        self.assertEqual(paid_to_pending.status_code, 409)
        self.assertEqual(paid_to_pending.get_json()["code"], "PAID_IMMUTABLE")

        paid_edit = self.client.put(
            "/api/payments/pay_history",
            json={
                "project_id": "p_history",
                "payment_date": "2025-02-01",
                "total_amount": 9000,
                "status": "paid",
                "notes": "corrected note",
            },
            headers=headers,
        )
        self.assertEqual(paid_edit.status_code, 200)

    def test_report_attachment_ownership_and_replacement_cleanup(self):
        os.makedirs(ledger.REPORT_ATTACHMENT_DIR, exist_ok=True)
        first_path = os.path.join(ledger.REPORT_ATTACHMENT_DIR, "first.pdf")
        second_path = os.path.join(ledger.REPORT_ATTACHMENT_DIR, "second.pdf")
        for path in (first_path, second_path):
            with open(path, "wb") as fh:
                fh.write(b"%PDF-1.4\n")

        response, data = self.login("leader", "Leader-Test-Password-2026")
        self.assertEqual(response.status_code, 200)
        headers = {"X-CSRF-Token": data["csrf_token"]}

        create_response = self.client.post(
            "/api/due-diligence-reports",
            json={
                "title": "First report",
                "attachment_name": "first.pdf",
                "attachment_path": "report_attachments/first.pdf",
            },
            headers=headers,
        )
        self.assertEqual(create_response.status_code, 200)
        report_id = create_response.get_json()["id"]

        duplicate_response = self.client.post(
            "/api/due-diligence-reports",
            json={
                "title": "Attachment hijack",
                "attachment_name": "first.pdf",
                "attachment_path": "report_attachments/first.pdf",
            },
            headers=headers,
        )
        self.assertEqual(duplicate_response.status_code, 409)
        self.assertEqual(duplicate_response.get_json()["code"], "ATTACHMENT_IN_USE")

        invalid_response = self.client.post(
            "/api/due-diligence-reports",
            json={
                "title": "Invalid attachment",
                "attachment_name": "outside.pdf",
                "attachment_path": "../outside.pdf",
            },
            headers=headers,
        )
        self.assertEqual(invalid_response.status_code, 400)

        replace_response = self.client.put(
            f"/api/due-diligence-reports/{report_id}",
            json={
                "title": "First report updated",
                "attachment_name": "second.pdf",
                "attachment_path": "report_attachments/second.pdf",
            },
            headers=headers,
        )
        self.assertEqual(replace_response.status_code, 200)
        self.assertFalse(os.path.exists(first_path))
        self.assertTrue(os.path.exists(second_path))

        delete_response = self.client.delete(
            f"/api/due-diligence-reports/{report_id}", headers=headers
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertFalse(os.path.exists(second_path))

    def test_calculator_rejects_invalid_inputs(self):
        response, data = self.login("staff", "Staff-Test-Password-2026")
        self.assertEqual(response.status_code, 200)
        headers = {"X-CSRF-Token": data["csrf_token"]}
        for payload in (
            {"principal": 0, "annual_rate": 7.2, "months": 12},
            {"principal": 1000, "annual_rate": -1, "months": 12},
            {"principal": 1000, "annual_rate": 7.2, "months": 0},
            {"principal": "bad", "annual_rate": 7.2, "months": 12},
        ):
            result = self.client.post(
                "/api/calc/repayment", json=payload, headers=headers
            )
            self.assertEqual(result.status_code, 400)

    def test_private_attachments_and_leader_access(self):
        os.makedirs(ledger.REPORT_ATTACHMENT_DIR, exist_ok=True)
        attachment_path = os.path.join(ledger.REPORT_ATTACHMENT_DIR, "report.pdf")
        with open(attachment_path, "wb") as fh:
            fh.write(b"%PDF-1.4\n")

        self.assertEqual(
            self.client.get("/api/due-diligence-reports/attachments/report.pdf").status_code,
            401,
        )

        response, data = self.login("leader", "Leader-Test-Password-2026")
        self.assertEqual(response.status_code, 200)
        token = data["csrf_token"]
        attachment_response = self.client.get(
            "/api/due-diligence-reports/attachments/report.pdf"
        )
        self.assertEqual(attachment_response.status_code, 200)
        attachment_response.close()
        self.assertEqual(
            self.client.put(
                "/api/roles",
                json={"roles": []},
                headers={"X-CSRF-Token": token},
            ).status_code,
            200,
        )

    def test_chat_falls_back_to_local_ledger_query(self):
        response, data = self.login("staff", "Staff-Test-Password-2026")
        self.assertEqual(response.status_code, 200)
        with patch("app.urllib.request.urlopen", side_effect=OSError("model offline")):
            chat_response = self.client.post(
                "/api/chat",
                json={"question": "台账概况"},
                headers={"X-CSRF-Token": data["csrf_token"]},
            )
        result = chat_response.get_json()
        self.assertEqual(chat_response.status_code, 200)
        self.assertTrue(result["success"])
        self.assertEqual(result["source"], "ledger_fallback")
        self.assertFalse(result["model_online"])
        self.assertIn("项目台账共", result["answer"])

    def test_public_local_ai_target_does_not_receive_ledger_context(self):
        response, data = self.login("staff", "Staff-Test-Password-2026")
        self.assertEqual(response.status_code, 200)
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": "ok"}}]
                }).encode("utf-8")

        def fake_urlopen(req, timeout=0):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        backend = {
            "provider": "local",
            "url": "https://public-model.example/v1/chat/completions",
            "model": "test-model",
            "headers": {},
        }
        with patch("app._chat_backend_config", return_value=(backend, None)), \
             patch("app._ledger_context", return_value="SENSITIVE_LEDGER_MARKER"), \
             patch("app.config_value", side_effect=lambda name, default="": "0" if name == "AI_ALLOW_EXTERNAL_LEDGER_DATA" else default), \
             patch("app.urllib.request.urlopen", side_effect=fake_urlopen):
            chat_response = self.client.post(
                "/api/chat",
                json={"question": "概况", "context": {"customer": "SENSITIVE_PAGE_MARKER"}},
                headers={"X-CSRF-Token": data["csrf_token"]},
            )

        self.assertEqual(chat_response.status_code, 200)
        prompt = captured["payload"]["messages"][0]["content"]
        self.assertNotIn("SENSITIVE_LEDGER_MARKER", prompt)
        self.assertNotIn("SENSITIVE_PAGE_MARKER", prompt)
        self.assertIn("外部 AI 数据传输未授权", prompt)
        self.assertTrue(ledger._is_private_ai_url("http://127.0.0.1:8080"))
        self.assertTrue(ledger._is_private_ai_url("http://192.168.50.148:8080"))
        self.assertTrue(ledger._is_private_ai_url("http://[::1]:8080"))
        self.assertFalse(ledger._is_private_ai_url("https://public-model.example"))

    def test_direct_leader_action_creates_completed_approval_history(self):
        conn = sqlite3.connect(ledger.DB_PATH)
        conn.execute("INSERT INTO customers (id, name) VALUES (?, ?)", ("c_direct", "Direct Customer"))
        conn.execute(
            "INSERT INTO projects (id, project_name, customer_id, status) VALUES (?, ?, ?, ?)",
            ("p_direct", "Direct Project", "c_direct", "active"),
        )
        conn.execute(
            """INSERT INTO payment_schedules
               (id, project_id, period_number, due_date, total_amount, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("pay_direct", "p_direct", 1, "2026-06-15", 12345, "pending"),
        )
        conn.commit()
        conn.close()

        response, data = self.login("leader", "Leader-Test-Password-2026")
        self.assertEqual(response.status_code, 200)
        result = self.client.post(
            "/api/payments/pay_direct/confirm",
            json={"actual_pay_date": "2026-06-15", "actual_amount": 12345},
            headers={"X-CSRF-Token": data["csrf_token"]},
        )
        self.assertEqual(result.status_code, 200)
        approval_id = result.get_json()["approval_id"]

        conn = sqlite3.connect(ledger.DB_PATH)
        conn.row_factory = sqlite3.Row
        approval = conn.execute(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        log = conn.execute(
            "SELECT * FROM approval_logs WHERE approval_id=?", (approval_id,)
        ).fetchone()
        payment = conn.execute(
            "SELECT status FROM payment_schedules WHERE id='pay_direct'"
        ).fetchone()
        conn.close()

        self.assertEqual(approval["status"], "approved")
        self.assertEqual(approval["executed"], 1)
        self.assertEqual(json.loads(approval["snapshot"])["status"], "pending")
        self.assertEqual(log["action"], "direct_execute")
        self.assertEqual(payment["status"], "paid")

    def test_invalid_rowid_search_limit_recursive_redaction_and_csp(self):
        response, data = self.login("leader", "Leader-Test-Password-2026")
        self.assertEqual(response.status_code, 200)
        headers = {"X-CSRF-Token": data["csrf_token"]}

        invalid = self.client.get("/api/projects/__rowid__abc")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.get_json()["code"], "INVALID_IDENTIFIER")

        search = self.client.get("/api/search?q=" + ("x" * 101))
        self.assertEqual(search.status_code, 400)
        self.assertEqual(search.get_json()["code"], "SEARCH_TOO_LONG")

        created = self.client.post(
            "/api/customers",
            json={
                "name": "Audit Redaction Customer",
                "metadata": {
                    "access_token": "nested-token-value",
                    "safe": [{"api_key": "nested-key-value"}],
                },
            },
            headers=headers,
        )
        self.assertEqual(created.status_code, 200)

        conn = sqlite3.connect(ledger.DB_PATH)
        detail = conn.execute(
            "SELECT detail FROM audit_logs WHERE endpoint='create_customer' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        conn.close()
        self.assertNotIn("nested-token-value", detail)
        self.assertNotIn("nested-key-value", detail)
        self.assertGreaterEqual(detail.count("<redacted>"), 2)

        page = self.client.get("/")
        csp = page.headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("integrity=\"sha384-", page.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
