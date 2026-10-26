import streamlit as st
import pandas as pd
from streamlit_gsheets import GSheetsConnection
import time

st.set_page_config(page_title="Test Connection", layout="wide")
st.title("🧪 ระบบทดสอบการเชื่อมต่อ Google Sheets")

try:
    SPREADSHEET_URL = str(st.secrets["app"]["spreadsheet_url"]).strip()
    st.info(f"🔗 **URL ที่อ่านได้จาก Secrets:** `{SPREADSHEET_URL}`")
except Exception as e:
    st.error("❌ ไม่พบ URL ใน Streamlit Secrets กรุณาตั้งค่า `[app]` และ `spreadsheet_url`")
    st.stop()

if st.button("🚀 เริ่มการทดสอบ (Run Tests)"):
    try:
        conn = st.connection("gsheets", type=GSheetsConnection)
        
        # 1. ทดสอบการอ่าน (Read Permission)
        st.subheader("1. ทดสอบการอ่าน (Read)")
        try:
            df_master = conn.read(spreadsheet=SPREADSHEET_URL, worksheet="รายชื่อ", ttl=0)
            st.success(f"✅ อ่านแท็บ 'รายชื่อ' สำเร็จ (พบข้อมูล {len(df_master)} แถว)")
            st.dataframe(df_master.head(3))
        except Exception as e:
            st.error(f"❌ อ่านแท็บ 'รายชื่อ' ล้มเหลว\n\nรายละเอียด Error: `{type(e).__name__}: {e}`")

        # 2. ทดสอบการเขียน (Write Permission)
        st.subheader("2. ทดสอบการเขียน (Write)")
        try:
            # ลองเขียนข้อมูลลงแท็บตั้งค่า
            test_df = pd.DataFrame([{"Key": "test_write", "Value": f"ทดสอบเมื่อ {time.ctime()}"}])
            conn.update(spreadsheet=SPREADSHEET_URL, worksheet="ตั้งค่า", data=test_df)
            st.success("✅ เขียนข้อมูลลงแท็บ 'ตั้งค่า' สำเร็จ (Service Account มีสิทธิ์ Editor)")
            
            # อ่านกลับมาดูว่าเขียนลงจริงไหม
            df_config = conn.read(spreadsheet=SPREADSHEET_URL, worksheet="ตั้งค่า", ttl=0)
            st.dataframe(df_config)
        except Exception as e:
            st.error(f"❌ เขียนข้อมูลล้มเหลว (อาจไม่ได้แชร์สิทธิ์ Editor ให้ Service Account)\n\nรายละเอียด Error: `{type(e).__name__}: {e}`")

    except Exception as e:
        st.error(f"🚨 เชื่อมต่อ API ล้มเหลว กรุณาเช็ค Service Account JSON ใน Secrets\n\n`{e}`")
