import unittest
from unittest.mock import MagicMock
import sys
import pandas as pd

# --- 1. จำลอง (Mock) Streamlit เพื่อไม่ให้พังตอน Import ไฟล์ app.py ---
mock_st = MagicMock()
mock_st.secrets = {
    "app": {"spreadsheet_url": "https://docs.google.com/spreadsheets/d/mock_url"},
    "ADMIN_BOOTSTRAP_PASSWORD": "mock_password"
}

# 💡 ต้องมี 2 บรรทัดนี้! เพื่อหลอก Streamlit ว่ามันคืนค่า Tabs และ Columns กลับมาให้จริงๆ
mock_st.columns.return_value = [MagicMock(), MagicMock(), MagicMock()]
mock_st.tabs.return_value = [MagicMock(), MagicMock()]

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
        self.assertEqual(app.normalize_dob("1011970"), "01011970")
        self.assertEqual(app.normalize_dob("05052003"), "05052003")

# =========================================================
# ชุดทดสอบที่ 2: ตรวจสอบการกรองรายชื่อ (Validation)
# =========================================================
class TestMonitoValidation(unittest.TestCase):
    def setUp(self):
        self.master_data = pd.DataFrame({
            "Name": ["A", "B", "C"],
            "Dept": ["IT", "HR", "MKT"],
            "Year": ["1", "2", "3"],
            "DOB": ["01012000", "02022000", "03032000"]
        })

    def test_prepare_master_missing_column(self):
        bad_data = self.master_data.drop(columns=["DOB"])
        with self.assertRaises(ValueError):
            app.prepare_master(bad_data)

    def test_prepare_master_duplicate_name(self):
        dup_data = pd.DataFrame({
            "Name": ["A", "A", "C"],
            "Dept": ["IT", "HR", "MKT"],
            "Year": ["1", "2", "3"],
            "DOB": ["01012000", "02022000", "03032000"]
        })
        with self.assertRaises(ValueError) as context:
            app.prepare_master(dup_data)
        self.assertIn("ซ้ำ", str(context.exception))

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
        result_data = pd.DataFrame({
            "Name": ["A", "B", "C"],
            "Dept": ["IT", "HR", "MKT"],
            "Year": ["1", "2", "3"],
            "Monito": ["A", "C", "B"],
            "Viewed_At": ["", "", ""]
        })
        is_valid, msg = app.validate_result_data(result_data, self.master_data)
        self.assertFalse(is_valid)
        self.assertIn("จับได้ตัวเอง", msg)

    def test_validate_result_mutual_pair(self):
        result_data = pd.DataFrame({
            "Name": ["A", "B", "C"],
            "Dept": ["IT", "HR", "MKT"],
            "Year": ["1", "2", "3"],
            "Monito": ["B", "C", "B"],
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
        password = "my_secure_password"
        salt = "random_salt_123"
        iterations = 1000
        
        hash_val = app.password_digest(password, salt, iterations)
        credentials = {"salt": salt, "hash": hash_val, "iterations": iterations}
        
        self.assertTrue(app.verify_admin_password(password, credentials))
        self.assertFalse(app.verify_admin_password("wrong_password", credentials))

if __name__ == '__main__':
    unittest.main()
