import unittest
from unittest.mock import MagicMock
import sys
import pandas as pd

# --- 1. จำลอง (Mock) Streamlit เพื่อไม่ให้พังตอน Import ไฟล์ app.py ---
# หลอกระบบว่ามี Streamlit Secrets อยู่แล้ว เพื่อให้ผ่านบรรทัด st.secrets ไปได้
mock_st = MagicMock()
mock_st.secrets = {
    "app": {"spreadsheet_url": "https://docs.google.com/spreadsheets/d/mock_url"},
    "ADMIN_BOOTSTRAP_PASSWORD": "mock_password"
}
sys.modules["streamlit"] = mock_st
sys.modules["streamlit_gsheets"] = MagicMock()

# --- 2. นำเข้าฟังก์ชันจากไฟล์แอปของเรา (ต้องตั้งชื่อไฟล์แอปว่า app.py) ---
import app

# =========================================================
# ชุดทดสอบที่ 1: ตรวจสอบการทำความสะอาดข้อมูล (Data Cleaning)
# =========================================================
class TestMonitoDataCleaning(unittest.TestCase):
    def test_clean_cell(self):
        """ทดสอบการลบช่องว่าง และแปลงตัวเลขทศนิยมเป็นจำนวนเต็ม"""
        self.assertEqual(app.clean_cell("  นาย เอ  "), "นาย เอ")
        self.assertEqual(app.clean_cell("99.0"), "99")
        self.assertEqual(app.clean_cell(float('nan')), "")
        self.assertEqual(app.clean_cell("None"), "")

    def test_normalize_dob(self):
        """ทดสอบการเติมเลข 0 ข้างหน้ารหัสวันเกิดที่หายไป (ปัญหาคลาสสิกของ Excel)"""
        self.assertEqual(app.normalize_dob("1011970"), "01011970") # เติม 0
        self.assertEqual(app.normalize_dob("05052003"), "05052003") # ปกติ

# =========================================================
# ชุดทดสอบที่ 2: ตรวจสอบการกรองรายชื่อ (Validation)
# =========================================================
class TestMonitoValidation(unittest.TestCase):
    def setUp(self):
        # สร้างข้อมูลจำลอง (Mock DataFrame) ไว้ใช้ตอนเทส
        self.master_data = pd.DataFrame({
            "Name": ["A", "B", "C"],
            "Dept": ["IT", "HR", "MKT"],
            "Year": ["1", "2", "3"],
            "DOB": ["01012000", "02022000", "03032000"]
        })

    def test_prepare_master_missing_column(self):
        """ทดสอบว่าถ้ารายชื่อขาดคอลัมน์สำคัญ ระบบต้องเตะกลับด้วย ValueError"""
        bad_data = self.master_data.drop(columns=["DOB"]) # ลบคอลัมน์ DOB ทิ้ง
        with self.assertRaises(ValueError):
            app.prepare_master(bad_data)

    def test_prepare_master_duplicate_name(self):
        """ทดสอบว่าถ้ามีชื่อคนซ้ำกัน ระบบต้องแจ้งเตือน"""
        dup_data = pd.DataFrame({
            "Name": ["A", "A", "C"], # ชื่อ A ซ้ำ
            "Dept": ["IT", "HR", "MKT"],
            "Year": ["1", "2", "3"],
            "DOB": ["01012000", "02022000", "03032000"]
        })
        with self.assertRaises(ValueError) as context:
            app.prepare_master(dup_data)
        self.assertIn("ซ้ำกัน", str(context.exception))

# =========================================================
# ชุดทดสอบที่ 3: ตรวจสอบกฎการจับคู่ (Algorithm Rules)
# =========================================================
class TestMonitoAlgorithmRules(unittest.TestCase):
    def setUp(self):
        self.master_data = pd.DataFrame({
            "Name": ["A", "B", "C"],
            "Dept": ["IT", "HR", "MKT"],
            "Year": ["1", "2", "3"]
        })

    def test_validate_result_success(self):
        """ทดสอบเคสที่ถูกต้อง: วงปิดสมบูรณ์ A->B, B->C, C->A"""
        result_data = pd.DataFrame({
            "Name": ["A", "B", "C"],
            "Dept": ["IT", "HR", "MKT"],
            "Year": ["1", "2", "3"],
            "Monito": ["B", "C", "A"],
            "Viewed_At": ["", "", ""]
        })
        is_valid, msg = app.validate_result_data(result_data, self.master_data)
        self.assertTrue(is_valid)

    def test_validate_result_self_loop(self):
        """ทดสอบการแหกกฎ: ห้ามจับได้ตัวเอง (A->A)"""
        result_data = pd.DataFrame({
            "Name": ["A", "B", "C"],
            "Dept": ["IT", "HR", "MKT"],
            "Year": ["1", "2", "3"],
            "Monito": ["A", "C", "B"], # A ได้ A
            "Viewed_At": ["", "", ""]
        })
        is_valid, msg = app.validate_result_data(result_data, self.master_data)
        self.assertFalse(is_valid)
        self.assertIn("จับได้ตัวเอง", msg)

    def test_validate_result_mutual_pair(self):
        """ทดสอบการแหกกฎ: ห้ามจับสลับกันเอง (B->C และ C->B)"""
        result_data = pd.DataFrame({
            "Name": ["A", "B", "C"],
            "Dept": ["IT", "HR", "MKT"],
            "Year": ["1", "2", "3"],
            "Monito": ["B", "C", "B"], # สลับกัน
            "Viewed_At": ["", "", ""]
        })
        is_valid, msg = app.validate_result_data(result_data, self.master_data)
        self.assertFalse(is_valid)
        self.assertIn("จับสลับกันเอง", msg)

# =========================================================
# ชุดทดสอบที่ 4: ตรวจสอบความปลอดภัย (Security)
# =========================================================
class TestMonitoSecurity(unittest.TestCase):
    def test_password_digest_and_verify(self):
        """ทดสอบการเข้ารหัสและตรวจสอบรหัสผ่าน Admin (PBKDF2 HMAC-SHA256)"""
        password = "my_secure_password"
        salt = "random_salt_123"
        iterations = 1000 # ใช้รอบต่ำๆ ในเทสเพื่อความรวดเร็ว
        
        # 1. สร้าง Hash
        hash_val = app.password_digest(password, salt, iterations)
        
        # 2. จำลองข้อมูลที่ดึงมาจาก Google Sheets
        credentials = {"salt": salt, "hash": hash_val, "iterations": iterations}
        
        # 3. เทสว่ารหัสถูก/ผิด ทำงานได้จริงไหม
        self.assertTrue(app.verify_admin_password(password, credentials))
        self.assertFalse(app.verify_admin_password("wrong_password", credentials))

if __name__ == '__main__':
    unittest.main()
