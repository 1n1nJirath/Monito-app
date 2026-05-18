import streamlit as st
import random
import pandas as pd
import datetime
import os
import json

# ไฟล์เก็บข้อมูลหลักและไฟล์ Metadata (เก็บเวลาที่รีเซ็ต)
SAVE_PATH = "monito_secret_backup.csv"
META_PATH = "monito_metadata.json"

st.set_page_config(page_title="Monito AnurakSciCU", page_icon="🎁", layout="centered")

# CSS: บังคับสีเข้มและตกแต่ง UI แบบ Universal (กัน Dark Mode 100%)
st.markdown("""
<style>
    /* บังคับพื้นหลังและสีตัวอักษรหลัก */
    .stApp { background-color: #f4f7f6 !important; }
    .stApp, h1, h2, h3, h4, p, span, label, div, li { color: #1a1a1a !important; }
    
    /* กล่องสถานะด้านบน */
    .status-box {
        background-color: #ffffff;
        padding: 20px;
        border-radius: 10px;
        border: 1px solid #dcdde1;
        margin-bottom: 25px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.05);
    }

    /* 1. ตกแต่ง Dropdown (ตัวกล่องตอนยังไม่กด) */
    div[data-baseweb="select"] > div {
        background-color: #ffffff !important;
        border-radius: 8px !important;
        border: 1px solid #dcdde1 !important;
        color: #1a1a1a !important;
    }
    
    /* 2. แก้ปัญหา Dropdown Pop-up อ่านไม่ออกใน Dark Mode */
    div[role="listbox"], ul[data-baseweb="menu"] {
        background-color: #ffffff !important; /* บังคับพื้นหลังรายการเป็นสีขาว */
    }
    li[role="option"] {
        color: #1a1a1a !important; /* บังคับตัวอักษรรายการเป็นสีดำ */
        background-color: #ffffff !important;
    }
    li[role="option"]:hover, li[role="option"][aria-selected="true"] {
        background-color: #e3f2fd !important; /* สีฟ้าอ่อนตอนเอาเมาส์ชี้หรือเลือกไว้ */
        color: #0056b3 !important;
    }
    
    /* ปุ่มหลัก */
    div[data-testid="stButton"] > button {
        background-color: #3498db !important;
        color: white !important; 
        border-radius: 8px !important;
        font-weight: bold !important;
        width: 100% !important;
        padding: 0.6rem 1rem !important;
        border: none !important;
    }
    div[data-testid="stButton"] > button:hover {
        background-color: #2980b9 !important;
    }
    
    /* กล่องแสดงผลลัพธ์ */
    .result-box {
        background-color: #d4edda; 
        border-left: 5px solid #28a745; 
        padding: 15px; 
        border-radius: 8px; 
        margin: 15px 0;
        text-align: center;
    }
</style>
""", unsafe_allow_html=True)

# รายชื่อผู้เข้าร่วม
raw_names = """
หงส์ geo #4
ปกป้อง geo #4
กิต envi #4
แต๊งค์ envi #4
แพรว envi #4
ซาโด envi #4
ไทไท mathcom #4
ต๋อย bio #3
ภีภี bio #3
แน้ม bio #3
ต้นข้าว biochem #3
บัว biochem #3
โซ่ biochem #3
ไข่มุก matsci #3
น้ำหวาน envi #3
แพทตี้ envi #3
ไอซ์ micro #2
เอิร์น micro #2
เจน foodtech #2
อิกคิว marine #2
ออมสิน marine #1
เอมมี่ marine #1
ปลาแซลมอน marine #1
เรน marine #1
บอส math com #1
ชุน bio #2
ไอย์ geo #4
แอมแปร์ biochem #3
โอปอล์ biochem #3
ออร์แกน envi #3
ไออุ่น bio #2
ณิ foodtech #2
ริว geo #2
รวี geo #2
ไข่มุข chemtech #1
นนต์ geo #1
อั๊ยศ์ geo #1
น้ำอิง geo #1
อิงอิง envi #1
ตะวัน physics #1
เพทาย geo #บัณฑิต
อ.มอส botgen
อ.รุจน์ photo
"""
participants = sorted([name.strip() for name in raw_names.strip().split('\n') if name.strip()])

# --- Functions ---
def generate_single_cycle_pairs(names):
    shuffled_names = names.copy()
    random.shuffle(shuffled_names)
    pairs = {}
    n = len(shuffled_names)
    for i in range(n):
        pairs[shuffled_names[i]] = shuffled_names[(i + 1) % n]
    return pairs

def save_metadata():
    with open(META_PATH, 'w') as f:
        json.dump({"last_reset": datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")}, f)

def get_metadata():
    if os.path.exists(META_PATH):
        with open(META_PATH, 'r') as f:
            return json.load(f).get("last_reset", "เพิ่งเปิดระบบครั้งแรก")
    return "เพิ่งเปิดระบบครั้งแรก"

# --- Data Management & Auto-Fix Error ---
if 'data' not in st.session_state:
    if os.path.exists(SAVE_PATH):
        df = pd.read_csv(SAVE_PATH)
        if 'Checked' not in df.columns:
            df['Checked'] = False
            df.to_csv(SAVE_PATH, index=False)
        st.session_state.data = df
    else:
        pairs = generate_single_cycle_pairs(participants)
        df = pd.DataFrame([{"Participant": k, "Monito": v, "Checked": False} for k, v in pairs.items()])
        st.session_state.data = df
        df.to_csv(SAVE_PATH, index=False)
        save_metadata()

# --- UI Header & Status ---
st.write(f"### 🎈 Monito AnurakSciCU 🎁")

last_reset_time = get_metadata()
checked_count = st.session_state.data['Checked'].sum()
total_count = len(participants)

st.markdown(f"""
<div class="status-box">
    <p style="margin:0; font-size:14px; color:#7f8c8d !important;">📊 สถานะการจับฉลาก</p>
    <h3 style="margin:5px 0; color:#2c3e50 !important;">สุ่มไปแล้ว: {checked_count} / {total_count} คน</h3>
    <p style="margin:0; font-size:12px; color:#95a5a6 !important;">🔄 รีเซ็ตล่าสุดเมื่อ: {last_reset_time}</p>
</div>
""", unsafe_allow_html=True)

# --- Dropdown Logic ---
display_options = []
name_map = {} 

for _, row in st.session_state.data.sort_values(by="Participant").iterrows():
    name = row['Participant']
    is_checked = row['Checked']
    label = f"{name} ✅" if is_checked else name
    display_options.append(label)
    name_map[label] = name

selected_label = st.selectbox("👤 เลือกชื่อของคุณ", ["-- กรุณาเลือกชื่อ --"] + display_options)

if selected_label != "-- กรุณาเลือกชื่อ --":
    actual_name = name_map[selected_label]
    result_container = st.empty()
    
    col1, col2 = st.columns(2)
    with col1: 
        show_btn = st.button("👀 ดูผลจับฉลาก", use_container_width=True)
    with col2: 
        hide_btn = st.button("🔒 ซ่อนผล", use_container_width=True)

    if show_btn:
        user_row_idx = st.session_state.data.index[st.session_state.data['Participant'] == actual_name][0]
        monito_name = st.session_state.data.at[user_row_idx, 'Monito']
        
        if not st.session_state.data.at[user_row_idx, 'Checked']:
            st.session_state.data.at[user_row_idx, 'Checked'] = True
            st.session_state.data.to_csv(SAVE_PATH, index=False)
            
        with result_container.container():
            st.markdown(f"""
            <div class="result-box">
                <h4 style="color: #155724 !important; margin: 0;">🎉 Monito ของคุณคือ: <br><br><b style="font-size: 24px;">{monito_name}</b></h4>
            </div>
            """, unsafe_allow_html=True)
            st.warning("⚠️ จำชื่อนี้ไว้ให้แม่น ห้ามบอกใคร แล้วกดซ่อนผลทันทีครับ")

    if hide_btn: 
        result_container.empty()

# ==========================================
# ส่วนของ Admin (ใส่รหัส 7886969 เพื่อรีเซ็ต หรือดาวน์โหลดไฟล์)
# ==========================================
st.markdown("<br><br><hr>", unsafe_allow_html=True)
with st.expander("🛠️ Admin Tools (สำหรับผู้ดูแล)"):
    admin_code = st.text_input("กรุณาใส่รหัสผ่านเพื่อรีเซ็ตระบบ:", type="password")
    
    if admin_code == "7886969":
        # --- เพิ่มปุ่มดาวน์โหลดไฟล์สำรองตรงนี้ ---
        if os.path.exists(SAVE_PATH):
            with open(SAVE_PATH, "rb") as file:
                st.download_button(
                    label="📥 ดาวน์โหลดไฟล์สำรอง (Backup Monito)",
                    data=file,
                    file_name="monito_backup.csv",
                    mime="text/csv",
                    use_container_width=True
                )
        
        st.markdown("<br>", unsafe_allow_html=True)
        
        # ปุ่มรีเซ็ตเดิม
        if st.button("⚠️ ล้างข้อมูลและสุ่มคู่ใหม่ทั้งหมด", use_container_width=True):
            if os.path.exists(SAVE_PATH): os.remove(SAVE_PATH)
            if os.path.exists(META_PATH): os.remove(META_PATH)
            st.session_state.clear()
            st.success("รีเซ็ตระบบสำเร็จ! กำลังเริ่มสุ่มใหม่...")
            st.rerun()
    elif admin_code != "":
        st.error("รหัสผ่านไม่ถูกต้อง")
