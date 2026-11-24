import hashlib
import hmac
import io
import json
import random
import re
import time
import uuid
from copy import copy
from datetime import datetime, timedelta, timezone

import extra_streamlit_components as stx
import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials
from gspread_formatting import CellFormat, Color, TextFormat, format_cell_range
from streamlit_gsheets import GSheetsConnection

# =========================================================
# Monito System - General Use + Ultimate Features
# - โปรไฟล์บัดดี้ (Food_Restriction, Favorites, Note_To_Monito)
# - กฎสุ่มตามบทบาท (Role) พร้อม Best-Effort Fallback
# - ระบบล็อกเครื่อง (Device Token) + ปุ่มปลดล็อกสำหรับ Admin
# - Cache & Batch Update เพื่อลด Google Sheets API Calls
# =========================================================

st.set_page_config(page_title="Monito System", page_icon="🎁", layout="centered")

st.markdown(
    """
    <style>
        .stApp { background-color: #f4f7f6; }
        [data-testid="stAppViewContainer"] { background-color: #f4f7f6; }
        div[data-baseweb="select"] > div { background-color: #ffffff; border-radius: 8px; }
        div[data-testid="stButton"] > button { border-radius: 8px; font-weight: 600; width: 100%; }
        .status-box { background: #ffffff; padding: 20px; border-radius: 12px; border: 1px solid #dcdde1; margin-bottom: 20px; text-align: center;}
        .big-name { font-size: 2.5rem; font-weight: 800; color: #2e86de; margin: 10px 0; }
        .profile-box { background: #f8f9fa; padding: 14px 18px; border-radius: 10px; border: 1px solid #e1e4e8; text-align: left; margin-top: 14px; }
    </style>
    """,
    unsafe_allow_html=True,
)

BKK_TZ = timezone(timedelta(hours=7))
def get_bkk_time(): return datetime.now(BKK_TZ)

MASTER_SHEET, RESULT_SHEET, CONFIG_SHEET = "รายชื่อ", "ผลจับฉลาก", "ตั้งค่า"
REQUIRED_MASTER_COLUMNS = ["Name", "Dept", "Year", "DOB"]
OPTIONAL_MASTER_COLUMNS = ["Role", "Food_Restriction", "Favorites", "Note_To_Monito"]
RESULT_COLUMNS = ["Name", "Dept", "Year", "Monito", "Viewed_At", "Device_Token"]
PBKDF2_ITERATIONS, ADMIN_SESSION_SECONDS, MAX_DOB_ATTEMPTS = 600_000, 1800, 5

try:
    SPREADSHEET_URL = str(st.secrets["app"]["spreadsheet_url"]).strip()
    BOOTSTRAP_ADMIN_PASSWORD = str(st.secrets.get("ADMIN_BOOTSTRAP_PASSWORD", ""))
except Exception:
    st.error("ยังไม่ได้ตั้งค่า app.spreadsheet_url ใน Streamlit Secrets"); st.stop()

cookie_manager = stx.CookieManager(key="monito_cookies")

# --- Connection & Cached Readers ---
@st.cache_resource
def _get_gspread_spreadsheet():
    """เปิด Spreadsheet ด้วย gspread และ cache การเชื่อมต่อไว้"""
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    raw = None
    if "connections" in st.secrets and "gsheets" in st.secrets["connections"]:
        raw = st.secrets["connections"]["gsheets"]
    elif "gcp_service_account" in st.secrets:
        raw = st.secrets["gcp_service_account"]
    elif "service_account" in st.secrets:
        raw = st.secrets["service_account"]
    info = json.loads(raw) if isinstance(raw, str) else {k: v for k, v in dict(raw).items() if k != "spreadsheet"}
    if "private_key" in info:
        info["private_key"] = str(info["private_key"]).replace("\\n", "\n")
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds).open_by_url(SPREADSHEET_URL)

@st.cache_resource
def _get_gsheets_conn():
    return st.connection("gsheets", type=GSheetsConnection)

@st.cache_data(ttl=300)
def read_master_cached():
    return _get_gsheets_conn().read(spreadsheet=SPREADSHEET_URL, worksheet=MASTER_SHEET, ttl=0)

@st.cache_data(ttl=300)
def read_config_cached():
    return _get_gsheets_conn().read(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, ttl=0)

@st.cache_data(ttl=60)
def read_result_cached():
    return _get_gsheets_conn().read(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, ttl=0)

conn = _get_gsheets_conn()

try:
    df_raw = read_master_cached()

    try: df_result_raw = read_result_cached()
    except: df_result_raw = pd.DataFrame()

    # ตรวจสอบและปรับปรุงโครงสร้างชีทผลการจับฉลาก (รองรับ Viewed_At และ Device_Token)
    if df_result_raw.empty and not set(RESULT_COLUMNS).issubset(df_result_raw.columns):
        conn.update(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, data=pd.DataFrame([{col: "" for col in RESULT_COLUMNS}]))
        read_result_cached.clear(); df_result_raw = read_result_cached()
    elif not df_result_raw.empty and not set(RESULT_COLUMNS).issubset(df_result_raw.columns):
        for col in RESULT_COLUMNS:
            if col not in df_result_raw.columns: df_result_raw[col] = ""
        conn.update(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, data=df_result_raw[RESULT_COLUMNS])
        read_result_cached.clear(); df_result_raw = read_result_cached()

    try: df_config = read_config_cached()
    except: df_config = pd.DataFrame()

    if df_config.empty and not {"Key", "Value"}.issubset(df_config.columns):
        conn.update(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, data=pd.DataFrame([{"Key": "", "Value": ""}]))
        read_config_cached.clear(); df_config = read_config_cached()
except Exception:
    st.error("ไม่สามารถอ่าน Google Sheets ได้"); st.stop()

# --- Helpers ---
def clean_cell(value):
    if pd.isna(value): return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else (text[:-2] if re.fullmatch(r"-?\d+\.0", text) else text)

def normalize_dob(value):
    text = clean_cell(value)
    return text.zfill(8) if text.isdigit() and 0 < len(text) < 8 else text

def normalize_role(value):
    text = clean_cell(value).lower()
    if text in {"staff", "สตาฟ", "สต๊าฟ", "พี่ค่าย"}:
        return "staff"
    if text in {"camper", "ลูกค่าย", "น้องค่าย"}:
        return "camper"
    return text

def config_values(config_df):
    if config_df is None or config_df.empty: return {}
    return {clean_cell(r["Key"]): clean_cell(r["Value"]) for _, r in config_df.iterrows() if clean_cell(r.get("Key"))}

def write_log(msg):
    try:
        read_config_cached.clear()
        curr = read_config_cached()
        logs = sorted([r for _, r in curr.iterrows() if str(r.get("Key", "")).startswith("log_")], key=lambda x: x["Key"], reverse=True)[:14]
        new_log = {"Key": f"log_{get_bkk_time().strftime('%Y%m%d_%H%M%S')}", "Value": f"[{get_bkk_time().strftime('%d/%m/%Y %H:%M:%S')}] {msg}"}
        non_logs = [r for _, r in curr.iterrows() if not str(r.get("Key", "")).startswith("log_")]
        conn.update(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, data=pd.DataFrame(non_logs + [new_log] + logs))
        read_config_cached.clear()
    except: pass

def _set_monito_camouflage(hide: bool):
    """เปลี่ยนสีพื้นหลังและตัวอักษรคอลัมน์ Monito (D) ด้วย gspread-formatting"""
    sh = _get_gspread_spreadsheet()
    ws = sh.worksheet(RESULT_SHEET)
    last_row = max(2, ws.row_count)
    fg = Color(1, 1, 1) if hide else Color(0, 0, 0)
    fmt = CellFormat(backgroundColor=Color(1, 1, 1), textFormat=TextFormat(foregroundColor=fg))
    format_cell_range(ws, f"D2:D{last_row}", fmt)

def update_view_and_device_batch(sheet_row: int, viewed_at: str, device_token: str):
    """อัปเดต Viewed_At (E) และ Device_Token (F) พร้อมกันใน 1 Request"""
    sh = _get_gspread_spreadsheet()
    ws = sh.worksheet(RESULT_SHEET)
    ws.update(range_name=f"E{sheet_row}:F{sheet_row}", values=[[viewed_at, device_token]])
    read_result_cached.clear()

def clear_player_state():
    """ล้าง key ของ widget และสถานะการดูผลใน st.session_state"""
    for k in ("verified_user", "verified_until", "player_name_select", "player_dob_input"):
        st.session_state.pop(k, None)

def display_label(row):
    role = clean_cell(row.get("Role", ""))
    base = f"{row['Name']} ({row['Dept']} #{row['Year']})"
    return f"{base} [{role}]" if role else base

# --- Event Settings ---
cfg = config_values(df_config)
EVENT_NAME = cfg.get("event_name", "Monito AnurakSciCU")

st.title(f"🌲 {EVENT_NAME} 🦉")

# --- Data Prep ---
for opt_col in OPTIONAL_MASTER_COLUMNS:
    if opt_col not in df_raw.columns:
        df_raw[opt_col] = ""

df_master = df_raw[REQUIRED_MASTER_COLUMNS + OPTIONAL_MASTER_COLUMNS].copy()
for col in ["Name", "Dept", "Year"] + OPTIONAL_MASTER_COLUMNS:
    df_master[col] = df_master[col].map(clean_cell)
df_master["DOB"] = df_master["DOB"].map(normalize_dob)
df_master = df_master.loc[~df_master[REQUIRED_MASTER_COLUMNS].eq("").all(axis=1)].copy().reset_index(drop=True)
master_error = "มีข้อมูลว่าง" if df_master[REQUIRED_MASTER_COLUMNS].eq("").any(axis=None) else ("ชื่อซ้ำ" if df_master["Name"].duplicated().any() else "")

df_result = pd.DataFrame(columns=RESULT_COLUMNS + ["_sheet_row"])
if not df_result_raw.empty and set(RESULT_COLUMNS).issubset(df_result_raw.columns):
    df_result = df_result_raw[RESULT_COLUMNS].copy()
    df_result["_sheet_row"] = df_result.index + 2  # แถวจริงใน Google Sheets (แถว 1 คือ Header)
    for col in RESULT_COLUMNS:
        df_result[col] = df_result[col].map(clean_cell)
    df_result = df_result.loc[~df_result[RESULT_COLUMNS].eq("").all(axis=1)].copy().reset_index(drop=True)

result_exists = not df_result.empty
name_to_label = {row["Name"]: display_label(row) for _, row in df_master.iterrows()}
master_profile_lookup = df_master.set_index("Name").to_dict(orient="index")

# =========================================================
# UI TABS
# =========================================================
tab_player, tab_admin = st.tabs(["🎁 ดูผลจับฉลาก (Player)", "🛠️ เครื่องมือ (Admin)"])

# ----------------- TAB 1: PLAYER -----------------
with tab_player:
    if not result_exists:
        st.info(f"ขณะนี้ยังไม่มีผลการจับฉลากในระบบ กรุณารอ Admin ดำเนินการสุ่มสำหรับกิจกรรม {EVENT_NAME}")
    elif master_error:
        st.error(f"ข้อมูลฐานชื่อมีปัญหา: {master_error}")
    else:
        st.markdown(f'<div class="status-box"><p style="color:#7f8fa6; margin:0;">ระบบเปิดให้ดูผลแล้ว ผู้เข้าร่วมทั้งหมด</p><h2 style="margin:0;">{len(df_result)} คน</h2></div>', unsafe_allow_html=True)

        result_lookup = df_result.set_index("Name").to_dict(orient="index")
        dob_lookup = df_master.set_index("Name")["DOB"].to_dict()
        browser_token = clean_cell(cookie_manager.get("monito_device_token")) or clean_cell(st.session_state.get("local_device_token", ""))

        selected_name = st.selectbox(
            "👤 เลือกชื่อของคุณ",
            options=[""] + df_result["Name"].tolist(),
            format_func=lambda v: "-- ค้นหาชื่อของคุณ --" if v == "" else name_to_label.get(v, v),
            key="player_name_select",
        )

        if selected_name:
            attempts_key = "dob_" + hashlib.sha256(selected_name.encode()).hexdigest()[:10]
            if int(st.session_state.get(attempts_key, 0)) >= MAX_DOB_ATTEMPTS:
                st.error("กรอกรหัสผิดเกินกำหนด")
            else:
                with st.form("pwd_form"):
                    pwd = st.text_input("🔑 รหัสผ่าน (วันเกิด DDMMYYYY)", type="password", key="player_dob_input")
                    if st.form_submit_button("👀 ดูผลจับฉลาก"):
                        if normalize_dob(pwd) and hmac.compare_digest(normalize_dob(pwd), dob_lookup.get(selected_name, "")):
                            row_info = result_lookup[selected_name]
                            existing_token = clean_cell(row_info.get("Device_Token", ""))
                            existing_viewed = clean_cell(row_info.get("Viewed_At", ""))

                            # ตรวจสอบ Device Token
                            if existing_token and existing_token != browser_token:
                                st.error("บัญชีนี้ถูกเปิดดูจากเครื่องอื่นไปแล้ว หากเปลี่ยนเครื่องกรุณาติดต่อ Admin เพื่อปลดล็อก")
                            else:
                                if not existing_token:
                                    if not browser_token:
                                        browser_token = str(uuid.uuid4())
                                        cookie_manager.set(
                                            "monito_device_token",
                                            browser_token,
                                            expires_at=datetime.now() + timedelta(days=30),
                                        )
                                        st.session_state["local_device_token"] = browser_token
                                    viewed_val = existing_viewed or get_bkk_time().strftime("%d/%m/%Y %H:%M:%S")
                                    try:
                                        update_view_and_device_batch(int(row_info["_sheet_row"]), viewed_val, browser_token)
                                    except Exception as e:
                                        print(f"Batch update error: {e}")
                                elif not existing_viewed:
                                    viewed_val = get_bkk_time().strftime("%d/%m/%Y %H:%M:%S")
                                    try:
                                        update_view_and_device_batch(int(row_info["_sheet_row"]), viewed_val, existing_token)
                                    except Exception as e:
                                        print(f"Batch update error: {e}")
                                # หาก Viewed_At และ Device_Token มีอยู่แล้วและตรงกัน -> ไม่ยิง Request เขียนซ้ำ

                                st.session_state.update({
                                    "verified_user": selected_name,
                                    "verified_until": get_bkk_time().timestamp() + 300,
                                    attempts_key: 0,
                                })
                                st.balloons()
                        else:
                            st.session_state[attempts_key] = int(st.session_state.get(attempts_key, 0)) + 1
                            st.error("รหัสไม่ถูกต้อง")

            if st.session_state.get("verified_user") == selected_name and get_bkk_time().timestamp() <= float(st.session_state.get("verified_until", 0)):
                target = result_lookup[selected_name]["Monito"]
                target_profile = master_profile_lookup.get(target, {})
                food_res = target_profile.get("Food_Restriction") or "-"
                favs = target_profile.get("Favorites") or "-"
                note = target_profile.get("Note_To_Monito") or "-"

                st.markdown(f"""
                    <div class="status-box" style="border-color:#2e86de;">
                        <p style="margin:0; font-size:1.2rem;">🎉 บัดดี้ของคุณคือ...</p>
                        <div class="big-name">{name_to_label.get(target, target)}</div>
                        <div class="profile-box">
                            <p style="margin:4px 0;"><b>🚫 ข้อจำกัดด้านอาหาร/แพ้อาหาร:</b> {food_res}</p>
                            <p style="margin:4px 0;"><b>💛 ของที่ชอบ:</b> {favs}</p>
                            <p style="margin:4px 0;"><b>💌 สิ่งที่อยากบอกโมนิโต้:</b> {note}</p>
                        </div>
                        <p style="color:#e74c3c; font-size:0.9rem; margin-top:12px;">(อย่าลืมซ่อนผลก่อนส่งเครื่องให้คนอื่น)</p>
                    </div>
                """, unsafe_allow_html=True)
                st.button("🙈 ซ่อนผลและออกจากระบบ", on_click=clear_player_state, use_container_width=True, type="primary")

# ----------------- TAB 2: ADMIN -----------------
with tab_admin:
    def verify_admin(pwd): return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pwd.encode(), cfg.get("admin_salt","").encode(), int(cfg.get("admin_iterations", 600000))).hex(), cfg.get("admin_hash",""))
    is_auth = st.session_state.get("is_admin", False) and get_bkk_time().timestamp() < st.session_state.get("admin_exp", 0)

    if not is_auth:
        st.warning("กรุณาเข้าสู่ระบบด้วยรหัสผ่านสตาฟ")
        admin_pwd = st.text_input("Admin Password", type="password")
        if st.button("เข้าสู่ระบบ"):
            if ("admin_hash" in cfg and verify_admin(admin_pwd)) or ("admin_hash" not in cfg and hmac.compare_digest(admin_pwd, BOOTSTRAP_ADMIN_PASSWORD)):
                st.session_state.update({"is_admin": True, "admin_exp": get_bkk_time().timestamp() + 1800})
                write_log("Admin logged in")
                st.rerun()
            else: st.error("รหัสไม่ถูกต้อง")
    else:
        top_c1, top_c2, top_c3 = st.columns([5, 3, 2])
        top_c1.success("✅ เข้าสู่ระบบ Admin แล้ว")
        if top_c2.button("🔄 รีเฟรชข้อมูลล่าสุด"):
            st.cache_data.clear()
            st.rerun()
        if top_c3.button("ออกจากระบบ"):
            st.session_state.pop("is_admin", None)
            st.rerun()

        if "draw_flash_warning" in st.session_state:
            st.warning(st.session_state.pop("draw_flash_warning"))

        c1, c2, c3 = st.columns(3)
        c1.metric("👥 ผู้เข้าร่วม", len(df_master))
        c2.metric("🔒 สถานะการสุ่ม", "เสร็จสิ้น" if result_exists else "รอการสุ่ม")
        c3.metric("📝 กฎที่มี", f"{len(st.session_state.get('locked_pairs',[]))} Lock / {len(st.session_state.get('blocked_pairs',[]))} Block")
        st.divider()

        sub_tab1, sub_tab2, sub_tab3 = st.tabs(["🎯 สุ่มผล & ตั้งกฎ", "🔎 ตรวจสอบผล/สถานะ", "⚙️ ตั้งค่าระบบ"])

        # ----- สุ่มผล -----
        with sub_tab1:
            if result_exists:
                st.info("มีการสุ่มผลแล้ว หากต้องการสุ่มใหม่ กรุณาล้างผลที่แท็บ 'ตั้งค่าระบบ' ก่อน")
            else:
                if "locked_pairs" not in st.session_state: st.session_state.locked_pairs = []
                if "blocked_pairs" not in st.session_state: st.session_state.blocked_pairs = []

                st.markdown("##### 🔒 ล็อกคู่ (ต้องได้)")
                if st.session_state.locked_pairs:
                    for i, lock in enumerate(st.session_state.locked_pairs):
                        c1, c2, c3 = st.columns([4, 4, 2])
                        c1.write(f"**{name_to_label.get(lock['giver'], lock['giver'])}** ➡️")
                        c2.write(f"**{name_to_label.get(lock['target'], lock['target'])}**")
                        if c3.button("ลบ", key=f"del_lock_{i}"): st.session_state.locked_pairs.pop(i); st.rerun()
                with st.expander("➕ เพิ่มล็อกคู่"):
                    col1, col2 = st.columns(2)
                    avail_g = [n for n in df_master["Name"] if n not in [l['giver'] for l in st.session_state.locked_pairs]]
                    avail_t = [n for n in df_master["Name"] if n not in [l['target'] for l in st.session_state.locked_pairs]]
                    with col1: giver = st.selectbox("ผู้จับ", [""] + avail_g, format_func=lambda x: name_to_label.get(x, x) if x else "-- เลือกผู้จับ --", key="lock_g")
                    with col2: target = st.selectbox("จับได้ใคร", [""] + avail_t, format_func=lambda x: name_to_label.get(x, x) if x else "-- เลือกเป้าหมาย --", key="lock_t")
                    if st.button("บันทึกล็อกคู่", use_container_width=True):
                        if not giver or not target: st.error("เลือกให้ครบ")
                        elif giver == target: st.error("ห้ามจับตัวเอง")
                        else: st.session_state.locked_pairs.append({"giver": giver, "target": target}); st.rerun()

                st.markdown("##### 🚫 ห้ามคู่ (ห้ามได้)")
                if st.session_state.blocked_pairs:
                    for i, block in enumerate(st.session_state.blocked_pairs):
                        c1, c2, c3 = st.columns([4, 4, 2])
                        c1.write(f"**{name_to_label.get(block['giver'], block['giver'])}** ❌")
                        c2.write(f"**{name_to_label.get(block['target'], block['target'])}**")
                        if c3.button("ลบ", key=f"del_block_{i}"): st.session_state.blocked_pairs.pop(i); st.rerun()
                with st.expander("➖ เพิ่มห้ามคู่"):
                    col1, col2 = st.columns(2)
                    with col1: b_giver = st.selectbox("ผู้จับ", [""] + df_master["Name"].tolist(), format_func=lambda x: name_to_label.get(x, x) if x else "-- เลือกผู้จับ --", key="block_g")
                    with col2: b_target = st.selectbox("ห้ามจับได้ใคร", [""] + df_master["Name"].tolist(), format_func=lambda x: name_to_label.get(x, x) if x else "-- เลือกเป้าหมาย --", key="block_t")
                    if st.button("บันทึกห้ามคู่", use_container_width=True):
                        if not b_giver or not b_target: st.error("เลือกให้ครบ")
                        elif b_giver == b_target: st.error("ระบบห้ามจับตัวเองอยู่แล้ว")
                        else: st.session_state.blocked_pairs.append({"giver": b_giver, "target": b_target}); st.rerun()

                st.markdown("##### 🎭 กฎตามบทบาท (Role-based Soft Rules)")
                rc1, rc2 = st.columns(2)
                forbid_camper_camper = rc1.checkbox("ห้ามลูกค่ายจับได้ลูกค่ายกันเอง", value=False)
                forbid_staff_staff = rc2.checkbox("ห้ามสตาฟจับได้สตาฟกันเอง", value=False)

                st.markdown("---")
                num_circles = st.slider("จำนวนวง", 1, max(1, len(df_master)//3), 1)

                if st.button("🎲 เริ่มสุ่มจับคู่", type="primary", use_container_width=True):
                    with st.spinner("กำลังคำนวณและเขียนลง Sheets..."):
                        names = df_master["Name"].tolist()
                        role_map = {row["Name"]: normalize_role(row.get("Role", "")) for _, row in df_master.iterrows()}
                        locked_dict = {p["giver"]: p["target"] for p in st.session_state.locked_pairs}
                        blocked_set = set((p["giver"], p["target"]) for p in st.session_state.blocked_pairs)

                        next_node = locked_dict.copy()
                        in_degree = {n: 0 for n in names}
                        for g, t in next_node.items(): in_degree[t] += 1

                        paths, visited, forced_cycles = [], set(), []
                        for n in names:
                            if in_degree[n] == 0:
                                p_list, curr = [], n
                                while curr is not None:
                                    p_list.append(curr); visited.add(curr); curr = next_node.get(curr)
                                paths.append(p_list)
                        for n in names:
                            if n not in visited:
                                p_list, curr = [], n
                                while curr not in visited:
                                    p_list.append(curr); visited.add(curr); curr = next_node.get(curr)
                                forced_cycles.append(p_list)

                        remaining_circles = num_circles - len(forced_cycles)
                        best_pairs = None
                        best_violations = float("inf")

                        for _ in range(2000):
                            random.SystemRandom().shuffle(paths)
                            groups = [[] for _ in range(remaining_circles)]
                            for idx, p in enumerate(paths): groups[idx % remaining_circles].append(p)

                            temp_pairs = {}
                            for c in forced_cycles:
                                for i in range(len(c)): temp_pairs[c[i]] = c[(i+1)%len(c)]

                            is_invalid = False
                            for g in groups:
                                if not g: continue
                                if sum(len(p) for p in g) < 3: is_invalid = True; break
                                for i in range(len(g)):
                                    curr_path, next_path = g[i], g[(i+1)%len(g)]
                                    for j in range(len(curr_path)-1): temp_pairs[curr_path[j]] = curr_path[j+1]
                                    if (curr_path[-1], next_path[0]) in blocked_set: is_invalid = True; break
                                    temp_pairs[curr_path[-1]] = next_path[0]
                                if is_invalid: break

                            if not is_invalid:
                                violations = 0
                                if forbid_camper_camper or forbid_staff_staff:
                                    for g_name, t_name in temp_pairs.items():
                                        g_role, t_role = role_map.get(g_name, ""), role_map.get(t_name, "")
                                        if forbid_camper_camper and g_role == "camper" and t_role == "camper":
                                            violations += 1
                                        if forbid_staff_staff and g_role == "staff" and t_role == "staff":
                                            violations += 1
                                if violations < best_violations:
                                    best_violations = violations
                                    best_pairs = temp_pairs
                                if best_violations == 0:
                                    break

                        if not best_pairs:
                            st.error("หาการจับคู่ไม่พบ! เงื่อนไขขัดแย้งกันเกินไป กรุณาลดกฎลง")
                        else:
                            result_to_save = df_master[["Name", "Dept", "Year"]].copy()
                            result_to_save["Monito"] = result_to_save["Name"].map(best_pairs)
                            result_to_save["Viewed_At"] = ""
                            result_to_save["Device_Token"] = ""

                            try:
                                conn.update(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, data=result_to_save[RESULT_COLUMNS])

                                # --- พรางตาสีตัวอักษรใน Google Sheets ให้เป็นสีขาว (กลืนกับพื้นหลัง) ---
                                try:
                                    _set_monito_camouflage(hide=True)
                                except Exception as e: print(f"Format hiding error: {e}")

                                read_result_cached.clear()
                                write_log(f"Draw generated successfully. (role_violations={best_violations})")
                                if best_violations > 0:
                                    st.session_state["draw_flash_warning"] = f"⚠️ สุ่มสำเร็จแบบ Best-Effort: มีการแหกกฎบทบาทจำนวน {best_violations} คู่ เนื่องจากข้อจำกัดไม่สามารถหลีกเลี่ยงได้ทั้งหมด"
                                st.success("สุ่มและบันทึกผลสำเร็จ! (ซ่อนตัวอักษรใน Google Sheets แล้ว)"); time.sleep(1.5); st.rerun()
                            except Exception: st.error("บันทึกผลไม่สำเร็จ")

        # ----- ค้นหาและดูสถานะ -----
        with sub_tab2:
            st.markdown("### 👁️ สถานะการเปิดดูผล (Read Receipt)")
            if result_exists:
                viewed_mask = (df_result["Viewed_At"].notna()) & (df_result["Viewed_At"] != "")
                viewed = df_result[viewed_mask]["Name"].tolist()
                not_viewed = df_result[~viewed_mask]["Name"].tolist()

                vc1, vc2 = st.columns(2)
                vc1.metric("✅ เปิดดูแล้ว", f"{len(viewed)} คน")
                vc2.metric("❌ ยังไม่เปิดดู", f"{len(not_viewed)} คน")

                with st.expander("รายชื่อคนที่ยังไม่เปิดดู", expanded=True):
                    if not_viewed:
                        not_viewed_labels = [name_to_label.get(n, n) for n in not_viewed]
                        st.error(", ".join(not_viewed_labels))
                    else:
                        st.success("ทุกคนเข้ามาดูผลครบแล้ว!")
            else:
                st.info("ยังไม่มีการสุ่มผล")

            st.divider()
            st.markdown("### 🔓 ปลดล็อกเครื่องผู้เล่น (Reset Device Token)")
            if result_exists:
                locked_players = df_result[df_result["Device_Token"].astype(str).str.strip() != ""]["Name"].tolist()
                if not locked_players:
                    st.info("ยังไม่มีผู้เล่นที่ถูกล็อกเครื่อง")
                else:
                    unlock_target = st.selectbox(
                        "เลือกผู้เล่นที่ต้องการปลดล็อกเครื่อง",
                        options=[""] + locked_players,
                        format_func=lambda v: "-- เลือกชื่อ --" if v == "" else name_to_label.get(v, v),
                        key="admin_unlock_select",
                    )
                    if unlock_target and st.button("🔓 ปลดล็อกเครื่อง", use_container_width=True):
                        try:
                            target_row = int(df_result.loc[df_result["Name"] == unlock_target, "_sheet_row"].iloc[0])
                            sh = _get_gspread_spreadsheet()
                            ws = sh.worksheet(RESULT_SHEET)
                            ws.update(range_name=f"F{target_row}", values=[[""]])
                            read_result_cached.clear()
                            write_log(f"Unlocked device token for {unlock_target}")
                            st.success(f"ปลดล็อกเครื่องให้ {name_to_label.get(unlock_target, unlock_target)} เรียบร้อยแล้ว")
                            time.sleep(1); st.rerun()
                        except Exception as e:
                            st.error(f"ปลดล็อกไม่สำเร็จ: {e}")
            else:
                st.info("ยังไม่มีการสุ่มผล")

            st.divider()
            st.markdown("### 🔎 ค้นหาผล (กรณีเด็กลืมบัดดี้)")
            admin_search = st.selectbox("ค้นหาชื่อผู้จับ", options=[""] + (df_result["Name"].tolist() if result_exists else []), format_func=lambda v: "-- เลือกชื่อ --" if v == "" else name_to_label.get(v, v))
            if admin_search and result_exists:
                target = df_result.set_index("Name").to_dict(orient="index")[admin_search]["Monito"]
                st.success(f"{name_to_label.get(admin_search, admin_search)} จับได้ ➡️ {name_to_label.get(target, target)}")

        # ----- ตั้งค่าระบบ -----
        with sub_tab3:
            st.markdown("### ตั้งค่าข้อมูลค่าย")
            st.caption("สามารถเปลี่ยนชื่อค่ายได้โดยพิมพ์ `event_name` ในแท็บ 'ตั้งค่า' ของ Google Sheets")

            if result_exists:
                buffer = io.BytesIO()
                person_labels = {row["Name"]: display_label(row) for _, row in df_master.iterrows()}
                export_df = df_result[RESULT_COLUMNS].copy()
                export_df["Monito"] = export_df["Monito"].map(person_labels).fillna(export_df["Monito"])
                with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                    export_df.to_excel(writer, index=False, sheet_name="ผลจับฉลาก")
                    ws = writer.sheets["ผลจับฉลาก"]
                    ws.freeze_panes = "A2"; ws.column_dimensions["C"].width = 24; ws.column_dimensions["F"].width = 40

                st.download_button("⬇️ ดาวน์โหลด Excel สำรอง (สำหรับหน้างานออฟไลน์)", data=buffer.getvalue(), file_name=f"Monito_{EVENT_NAME}.xlsx")

            st.divider()
            st.markdown("### ⚠️ ล้างผลจับฉลาก (โซนอันตราย)")
            st.warning("การล้างผล ระบบจะเก็บผลปัจจุบันไปไว้ในแท็บ Backup อัตโนมัติ (และสถานะการเข้าดูรวมถึง Device Token จะถูกลบไปด้วย)")
            reset_pwd = st.text_input("กรอกรหัสผ่าน Admin เพื่อยืนยัน", type="password", key="reset_confirm_pwd")
            if st.button("🗑️ ยืนยันการล้างผล", type="primary"):
                is_valid_admin = ("admin_hash" in cfg and verify_admin(reset_pwd)) or ("admin_hash" not in cfg and hmac.compare_digest(reset_pwd, BOOTSTRAP_ADMIN_PASSWORD))
                if not is_valid_admin: st.error("รหัสผ่านไม่ถูกต้อง")
                else:
                    try:
                        backup_sheet_name = f"Backup_{get_bkk_time().strftime('%d%b_%H%M')}"
                        try:
                            sh = _get_gspread_spreadsheet()
                            if any(w.title == backup_sheet_name for w in sh.worksheets()):
                                backup_sheet_name = f"Backup_{get_bkk_time().strftime('%d%b_%H%M%S')}"
                            sh.add_worksheet(title=backup_sheet_name, rows=max(100, len(df_result_raw)+20), cols=10)
                            conn.update(spreadsheet=SPREADSHEET_URL, worksheet=backup_sheet_name, data=df_result_raw)
                        except Exception as e: print(f"Backup Error: {e}")

                        # ล้างข้อมูล รีเซต Viewed_At และ Device_Token ไปพร้อมกัน
                        conn.update(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, data=pd.DataFrame([{col: "" for col in RESULT_COLUMNS}]))

                        try:
                            _set_monito_camouflage(hide=False)
                        except Exception as e: print(f"Format restore error: {e}")

                        st.cache_data.clear(); clear_player_state()
                        st.success(f"ล้างผลและสถานะการเข้าดูสำเร็จ (สร้าง Backup ไว้ที่แท็บ {backup_sheet_name})")
                        time.sleep(2); st.rerun()
                    except Exception: st.error("ล้างผลไม่สำเร็จ")
