"""Smoke tests. Run with:  python -m unittest -v test_app"""
import io
import os
import re
import tempfile
import unittest
from datetime import timedelta

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ["ADMIN_EMAIL"] = "admin@test.com"
os.environ["ADMIN_PASSWORD"] = "AdminPass1"

import app as derma  # noqa: E402


def token(client, url="/login"):
    html = client.get(url).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def next_open_day(offset=1):
    d = derma.now().date() + timedelta(days=offset)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d


class DermaPlanetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        derma.app.config["TESTING"] = True

    def register_and_login(self, client, email="pat@test.com"):
        t = token(client, "/register")
        r = client.post("/register", data={
            "csrf_token": t, "name": "Pat Patient", "email": email, "phone": "9876543210",
            "password": "Passw0rdOK", "confirm": "Passw0rdOK"})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        t = token(client, "/login")
        r = client.post("/login", data={"csrf_token": t, "email": email, "password": "Passw0rdOK"})
        self.assertEqual(r.status_code, 302)

    def test_public_pages(self):
        c = derma.app.test_client()
        for url in ["/", "/about", "/services", "/doctors", "/contact", "/login",
                    "/register", "/admin-login"]:
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
        self.assertIn(b"Ananya Sharma", c.get("/doctors").data)
        self.assertEqual(c.get("/nope").status_code, 404)

    def test_csp_has_no_inline_code(self):
        c = derma.app.test_client()
        for url in ["/", "/login", "/doctors", "/about", "/services", "/contact"]:
            html = c.get(url).get_data(as_text=True)
            self.assertNotIn("style=", html, url)
            self.assertNotRegex(html, r"<script(?![^>]*src=)", url)
            self.assertNotRegex(html, r"\son\w+=", url)

    def test_login_required_and_csrf(self):
        c = derma.app.test_client()
        self.assertEqual(c.get("/appointment").status_code, 302)
        self.assertEqual(c.get("/dashboard").status_code, 302)
        self.assertEqual(c.post("/login", data={"email": "a@b.com", "password": "x"}).status_code, 400)

    def test_register_validation(self):
        c = derma.app.test_client()
        t = token(c, "/register")
        r = c.post("/register", data={"csrf_token": t, "name": "X", "email": "bad", "phone": "123",
                                      "password": "short", "confirm": "short"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"valid email", r.data)

    def test_booking_flow_and_double_booking(self):
        c = derma.app.test_client()
        self.register_and_login(c, "flow@test.com")
        self.assertEqual(c.get("/appointment").status_code, 200)
        self.assertEqual(c.get("/dashboard").status_code, 200)

        d = next_open_day(2)
        slots = c.get(f"/api/slots?doctor=1&date={d}").get_json()["slots"]
        self.assertTrue(slots)
        self.assertNotIn("13:00", [s["value"] for s in slots])
        slot = slots[0]["value"]

        t = token(c, "/appointment")
        data = {"csrf_token": t, "name": "Pat", "email": "flow@test.com", "phone": "9876543210",
                "doctor": "1", "date": d.isoformat(), "time": slot, "problem": "Acne on cheeks"}
        r = c.post("/appointment", data=data)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/my-appointments", r.headers["Location"])

        after = c.get(f"/api/slots?doctor=1&date={d}").get_json()["slots"]
        self.assertNotIn(slot, [s["value"] for s in after])

        r = c.post("/appointment", data=data)           # same slot again
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"no longer available", r.data)

        page = c.get("/my-appointments").get_data(as_text=True)
        self.assertIn("Pending", page)
        self.assertIn("Cancel", page)

        # cancel -> slot becomes free again
        appt_id = derma.connect_db().execute(
            "SELECT id FROM appointments WHERE email='flow@test.com'").fetchone()["id"]
        t = token(c, "/my-appointments")
        self.assertEqual(c.post(f"/appointments/{appt_id}/cancel", data={"csrf_token": t}).status_code, 302)
        again = c.get(f"/api/slots?doctor=1&date={d}").get_json()["slots"]
        self.assertIn(slot, [s["value"] for s in again])

    def test_sunday_and_past_rejected(self):
        c = derma.app.test_client()
        d = derma.now().date() + timedelta(days=1)
        while d.weekday() != 6:
            d += timedelta(days=1)
        self.assertEqual(c.get(f"/api/slots?doctor=1&date={d}").get_json()["slots"], [])
        past = derma.now().date() - timedelta(days=1)
        self.assertEqual(c.get(f"/api/slots?doctor=1&date={past}").get_json()["slots"], [])

    def test_patient_cannot_open_admin_or_cancel_others(self):
        c = derma.app.test_client()
        self.register_and_login(c, "norm@test.com")
        for url in ["/admin-dashboard", "/admin/doctors", "/admin/patients", "/admin/messages"]:
            self.assertEqual(c.get(url).status_code, 403, url)
        t = token(c, "/profile")
        self.assertEqual(c.post("/admin/appointments/1/delete", data={"csrf_token": t}).status_code, 403)
        self.assertEqual(c.post("/appointments/999/cancel", data={"csrf_token": t}).status_code, 404)

    def test_contact_and_admin_features(self):
        pub = derma.app.test_client()
        t = token(pub, "/contact")
        r = pub.post("/contact", data={"csrf_token": t, "name": "Sam", "email": "sam@test.com",
                                       "message": "Do you treat hair fall?"})
        self.assertEqual(r.status_code, 302)

        # patient books something for admin to manage
        pat = derma.app.test_client()
        self.register_and_login(pat, "adm_flow@test.com")
        d = next_open_day(3)
        slot = pat.get(f"/api/slots?doctor=2&date={d}").get_json()["slots"][0]["value"]
        t = token(pat, "/appointment")
        pat.post("/appointment", data={"csrf_token": t, "name": "Pat", "email": "adm_flow@test.com",
                                       "phone": "9876543210", "doctor": "2", "date": d.isoformat(),
                                       "time": slot, "problem": "Hair thinning"})

        adm = derma.app.test_client()
        # non-admin credentials rejected on admin login
        t = token(adm, "/admin-login")
        r = adm.post("/admin-login", data={"csrf_token": t, "email": "adm_flow@test.com",
                                           "password": "Passw0rdOK"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Incorrect", r.data)

        t = token(adm, "/admin-login")
        r = adm.post("/admin-login", data={"csrf_token": t, "email": "admin@test.com",
                                           "password": "AdminPass1"})
        self.assertEqual(r.status_code, 302)
        for url in ["/admin-dashboard", "/admin/doctors", "/admin/patients", "/admin/messages",
                    "/admin/doctors/new", "/admin/doctors/1/edit", "/admin-dashboard?status=Pending&q=Pat&when=upcoming"]:
            self.assertEqual(adm.get(url).status_code, 200, url)
        self.assertIn(b"Do you treat hair fall", adm.get("/admin/messages").data)

        row = derma.connect_db().execute(
            "SELECT id FROM appointments WHERE email='adm_flow@test.com'").fetchone()
        t = token(adm, "/admin-dashboard")
        r = adm.post(f"/admin/appointments/{row['id']}/status", data={"csrf_token": t, "status": "Approved"})
        self.assertEqual(r.status_code, 302)
        st = derma.connect_db().execute("SELECT status FROM appointments WHERE id=?", (row["id"],)).fetchone()
        self.assertEqual(st["status"], "Approved")
        r = adm.post(f"/admin/appointments/{row['id']}/status", data={"csrf_token": t, "status": "Bogus"})
        self.assertEqual(r.status_code, 400)

        # add doctor with a real image upload, then edit, then delete
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (1200, 900), (30, 120, 130)).save(buf, "PNG")
        buf.seek(0)
        t = token(adm, "/admin/doctors/new")
        r = adm.post("/admin/doctors/new", data={
            "csrf_token": t, "name": "Dr. Test Person", "specialization": "Pigmentation",
            "experience": "5 years", "bio": "Bio", "image": (buf, "photo.png")},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:400])
        doc = derma.connect_db().execute("SELECT * FROM doctors WHERE name='Dr. Test Person'").fetchone()
        self.assertTrue(doc["image"].startswith("uploads/"))
        self.assertTrue(os.path.exists(os.path.join(derma.app.config["UPLOAD_FOLDER"],
                                                    os.path.basename(doc["image"]))))
        self.assertIn(b"Test Person", pub.get("/doctors").data)

        # bad file type
        t = token(adm, "/admin/doctors/new")
        r = adm.post("/admin/doctors/new", data={
            "csrf_token": t, "name": "Dr. Bad", "specialization": "x", "experience": "1",
            "image": (io.BytesIO(b"<?php ?>"), "evil.php")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"JPG, PNG or WebP", r.data)

        t = token(adm, "/admin/doctors")
        r = adm.post(f"/admin/doctors/{doc['id']}/edit", data={
            "csrf_token": t, "name": "Dr. Test Person", "specialization": "Updated",
            "experience": "6 years", "bio": "", "active": "1"})
        self.assertEqual(r.status_code, 302)
        r = adm.post(f"/admin/doctors/{doc['id']}/delete", data={"csrf_token": t})
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(derma.connect_db().execute("SELECT 1 FROM doctors WHERE id=?", (doc["id"],)).fetchone())
        self.assertFalse(os.path.exists(os.path.join(derma.app.config["UPLOAD_FOLDER"],
                                                     os.path.basename(doc["image"]))))

        # doctor with history is hidden, not deleted
        r = adm.post("/admin/doctors/2/delete", data={"csrf_token": t})
        self.assertEqual(r.status_code, 302)
        row = derma.connect_db().execute("SELECT active FROM doctors WHERE id=2").fetchone()
        self.assertEqual(row["active"], 0)

        # logout
        self.assertEqual(adm.post("/logout", data={"csrf_token": t}).status_code, 302)
        self.assertEqual(adm.get("/admin-dashboard").status_code, 302)


    def test_admin_extras(self):
        pat = derma.app.test_client()
        self.register_and_login(pat, "extras@test.com")
        d = next_open_day(4)
        slots = pat.get(f"/api/slots?doctor=1&date={d}").get_json()["slots"]
        t = token(pat, "/appointment")
        pat.post("/appointment", data={"csrf_token": t, "name": "=cmd|' /C calc'!A0", "email": "extras@test.com",
                                       "phone": "9876543210", "doctor": "1", "date": d.isoformat(),
                                       "time": slots[0]["value"], "problem": "Eczema patches"})
        db = derma.connect_db()
        row = db.execute("SELECT id, user_id FROM appointments WHERE email='extras@test.com'").fetchone()
        appt_id, user_id = row["id"], row["user_id"]

        # patients cannot reach the new admin pages
        for url in [f"/admin/appointments/{appt_id}", f"/admin/patients/{user_id}", "/admin/staff",
                    "/admin/appointments/export.csv"]:
            self.assertEqual(pat.get(url).status_code, 403, url)

        adm = derma.app.test_client()
        t = token(adm, "/admin-login")
        adm.post("/admin-login", data={"csrf_token": t, "email": "admin@test.com", "password": "AdminPass1"})
        for url in [f"/admin/appointments/{appt_id}", f"/admin/patients/{user_id}", "/admin/staff"]:
            self.assertEqual(adm.get(url).status_code, 200, url)
        self.assertEqual(adm.get("/admin/appointments/99999").status_code, 404)

        # note shows up for the patient
        t = token(adm, f"/admin/appointments/{appt_id}")
        r = adm.post(f"/admin/appointments/{appt_id}", data={"csrf_token": t, "action": "note",
                                                              "note": "Please arrive early"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(b"Please arrive early", pat.get("/my-appointments").data)

        # reschedule: slot list for this appointment includes its own current slot
        own = adm.get(f"/api/slots?doctor=1&date={d}&exclude={appt_id}").get_json()["slots"]
        self.assertIn(slots[0]["value"], [x["value"] for x in own])
        other = [x["value"] for x in own if x["value"] != slots[0]["value"]][0]
        r = adm.post(f"/admin/appointments/{appt_id}", data={
            "csrf_token": t, "action": "reschedule", "doctor": "1", "date": d.isoformat(), "time": other})
        self.assertEqual(r.status_code, 302)
        moved = derma.connect_db().execute("SELECT appointment_time FROM appointments WHERE id=?",
                                           (appt_id,)).fetchone()
        self.assertEqual(moved["appointment_time"], other)
        r = adm.post(f"/admin/appointments/{appt_id}", data={
            "csrf_token": t, "action": "reschedule", "doctor": "1", "date": d.isoformat(), "time": "03:00"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"not available", r.data)

        # CSV export, with formula injection neutralised
        r = adm.get("/admin/appointments/export.csv?q=extras")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "text/csv")
        body = r.get_data(as_text=True)
        self.assertIn("extras@test.com", body)
        self.assertIn("'=cmd", body)

        # staff: create, cannot remove self, can remove other
        r = adm.post("/admin/staff", data={"csrf_token": t, "name": "Second Admin",
                                           "email": "second@test.com", "password": "Second123"})
        self.assertEqual(r.status_code, 302)
        sec = derma.connect_db().execute("SELECT id, is_admin FROM users WHERE email='second@test.com'").fetchone()
        self.assertEqual(sec["is_admin"], 1)
        me = derma.connect_db().execute("SELECT id FROM users WHERE email='admin@test.com'").fetchone()["id"]
        adm.post(f"/admin/staff/{me}/remove", data={"csrf_token": t})
        self.assertIsNotNone(derma.connect_db().execute("SELECT 1 FROM users WHERE id=?", (me,)).fetchone())
        adm.post(f"/admin/staff/{sec['id']}/remove", data={"csrf_token": t})
        self.assertIsNone(derma.connect_db().execute("SELECT 1 FROM users WHERE id=?", (sec["id"],)).fetchone())

        # deleting a patient removes their appointments
        r = adm.post(f"/admin/patients/{user_id}/delete", data={"csrf_token": t})
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(derma.connect_db().execute("SELECT 1 FROM appointments WHERE id=?", (appt_id,)).fetchone())

    def test_admin_link_visible_and_logo(self):
        html = derma.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("/admin-login", html)
        self.assertIn('class="brand-mark"', html)


    def test_theme_toggle_and_assets(self):
        c = derma.app.test_client()
        html = c.get("/").get_data(as_text=True)
        self.assertIn('class="theme-toggle"', html)
        self.assertIn("js/theme.js", html)
        self.assertIn("family=Poppins", html)
        css = c.get("/static/css/style.css").get_data(as_text=True)
        self.assertIn(':root[data-theme="dark"]', css)
        self.assertIn("--texture", css)
        self.assertEqual(c.get("/static/js/theme.js").status_code, 200)
        self.assertEqual(c.get("/static/js/script.js").status_code, 200)

    def test_open_redirect_blocked(self):
        c = derma.app.test_client()
        t = token(c, "/register")
        c.post("/register", data={"csrf_token": t, "name": "Red", "email": "red@test.com",
                                  "phone": "9876543210", "password": "Passw0rdOK", "confirm": "Passw0rdOK"})
        t = token(c, "/login")
        r = c.post("/login?next=https://evil.example/", data={
            "csrf_token": t, "email": "red@test.com", "password": "Passw0rdOK"})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("evil.example", r.headers["Location"])

    def test_login_rate_limit(self):
        c = derma.app.test_client()
        for _ in range(6):
            t = token(c, "/login")
            r = c.post("/login", data={"csrf_token": t, "email": "ghost@test.com", "password": "nope"})
        self.assertIn(b"Too many failed attempts", r.data)


if __name__ == "__main__":
    unittest.main()
