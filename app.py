import hashlib
import hmac
import io
import random
import re
import time
from copy import copy
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from streamlit_gsheets import GSheetsConnection

# =========================================================
# Monito AnurakSciCU (Ultimate Version)
# - เพิ่ม Concurrency Control (กันกดสุ่มพร้อมกัน)
# - เพิ่ม Auto-Backup เมื่อกดล้างผล (Fix สร้างแท็บใหม่)
# - เพิ่ม Audit Logs ในแท็บตั้งค่า
# - เวลาในระบบตรงกับเวลาประเทศไทย (UTC+7)
# - ค้นหาด้วย Dropdown และล้างผลด้วยรหัสผ่าน Admin
# =========================================================

st.set_page_config(page_title="Monito AnurakSciCU", page_icon="🎁", layout="centered")

st.markdown(
    """
    <style>
        .stApp { background-color: #f4f7f6; }
        [data-testid="stAppViewContainer"] { background-color: #f4f7f6; }
        h1, h2, h3, h4, p, label { color: #1a1a1a; }
        div[data-baseweb="select"] > div { background-color: #ffffff; border-radius: 8px; }
        div[data-testid="stButton"] > button, div[data-testid="stDownloadButton"] > button {
            border-radius: 8px; font-weight: 600; width: 100%;
        }
        .status-box { background: #ffffff; padding: 18px; border-radius: 10px; border: 1px solid #dcdde1; margin-bottom: 20px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🌲 Monito AnurakSciCU 🦉")
st.caption("ระบบจับฉลากบัดดี้ออนไลน์")

# ---------------------------------------------------------
# Settings & Timezone
# ---------------------------------------------------------
MASTER_SHEET = "รายชื่อ"
RESULT_SHEET = "ผลจับฉลาก"
CONFIG_SHEET = "ตั้งค่า"
REQUIRED_MASTER_COLUMNS = ["Name", "Dept", "Year", "DOB"]
RESULT_COLUMNS = ["Name", "Dept", "Year", "Monito"]
PBKDF2_ITERATIONS = 600_000
ADMIN_SESSION_SECONDS = 30 * 60
MAX_DOB_ATTEMPTS_PER_SESSION = 5

BKK_TZ = timezone(timedelta(hours=7))

def get_bkk_time():
    return datetime.now(BKK_TZ)

try:
    SPREADSHEET_URL = str(st.secrets["app"]["spreadsheet_url"]).strip()
except Exception:
    st.error("ยังไม่ได้ตั้งค่า app.spreadsheet_url ใน Streamlit Secrets")
    st.stop()

BOOTSTRAP_ADMIN_PASSWORD = str(st.secrets.get("ADMIN_BOOTSTRAP_PASSWORD", ""))

# ---------------------------------------------------------
# Connection & Initialize
# ---------------------------------------------------------
try:
    conn = st.connection("gsheets", type=GSheetsConnection)
    df_raw = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=MASTER_SHEET, ttl=30)
    
    try: df_result_raw = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, ttl=30)
    except pd.errors.EmptyDataError: df_result_raw = pd.DataFrame()

    if df_result_raw.empty and not set(RESULT_COLUMNS).issubset(df_result_raw.columns):
        conn.update(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, data=pd.DataFrame([{col: "" for col in RESULT_COLUMNS}]))
        st.cache_data.clear()
        df_result_raw = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, ttl=30)

    try: df_config = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, ttl=30)
    except pd.errors.EmptyDataError: df_config = pd.DataFrame()

    if df_config.empty and not {"Key", "Value"}.issubset(df_config.columns):
        conn.update(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, data=pd.DataFrame([{"Key": "", "Value": ""}]))
        st.cache_data.clear()
        df_config = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, ttl=30)
except Exception:
    st.error("ไม่สามารถอ่าน Google Sheets ได้ กรุณาตรวจสอบลิงก์และสิทธิ์การเข้าถึง")
    st.stop()

# ---------------------------------------------------------
# Audit Logs System
# ---------------------------------------------------------
def write_log(message: str):
    try:
        current_config = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, ttl=0)
        logs = [r for _, r in current_config.iterrows() if str(r.get("Key", "")).startswith("log_")]
        logs = sorted(logs, key=lambda x: x["Key"], reverse=True)[:14]
        
        timestamp_str = get_bkk_time().strftime("%Y%m%d_%H%M%S")
        new_log = {"Key": f"log_{timestamp_str}", "Value": f"[{get_bkk_time().strftime('%d/%m/%Y %H:%M:%S')}] {message}"}
        
        non_logs = [r for _, r in current_config.iterrows() if not str(r.get("Key", "")).startswith("log_")]
        updated_config = pd.DataFrame(non_logs + [new_log] + logs)
        
        conn.update(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, data=updated_config)
    except Exception as e:
        print(f"Log Error: {e}")

# ---------------------------------------------------------
# Concurrency Lock (Drawing Status)
# ---------------------------------------------------------
def is_drawing_in_progress() -> bool:
    values = config_values(df_config)
    lock_time_str = values.get("system_drawing_lock", "0")
    try:
        lock_time = float(lock_time_str)
        if get_bkk_time().timestamp() - lock_time < 120:
            return True
    except: pass
    return False

def set_drawing_lock(status: bool):
    current_config = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, ttl=0)
    current_config = current_config[current_config["Key"] != "system_drawing_lock"]
    if status:
        lock_data = pd.DataFrame([{"Key": "system_drawing_lock", "Value": str(get_bkk_time().timestamp())}])
        updated_config = pd.concat([current_config, lock_data], ignore_index=True)
    else:
        updated_config = current_config
    conn.update(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, data=updated_config)
    st.cache_data.clear()

# ---------------------------------------------------------
# Validation Helpers
# ---------------------------------------------------------
def clean_cell(value) -> str:
    try:
        if pd.isna(value): return ""
    except: pass
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}: return ""
    if re.fullmatch(r"-?\d+\.0", text): text = text[:-2]
    return text

def normalize_dob(value) -> str:
    text = clean_cell(value)
    if text.isdigit() and 0 < len(text) < 8: text = text.zfill(8)
    return text

def prepare_master(dataframe: pd.DataFrame) -> pd.DataFrame:
    if [c for c in REQUIRED_MASTER_COLUMNS if c not in dataframe.columns]: raise ValueError("แท็บ 'รายชื่อ' ขาดหัวคอลัมน์สำคัญ")
    df = dataframe[REQUIRED_MASTER_COLUMNS].copy()
    for col in ["Name", "Dept", "Year"]: df[col] = df[col].map(clean_cell)
    df["DOB"] = df["DOB"].map(normalize_dob)
    df = df.loc[~df[REQUIRED_MASTER_COLUMNS].eq("").all(axis=1)].copy()
    if df.empty: return df.reset_index(drop=True)
    if df[["Name", "Dept", "Year", "DOB"]].eq("").any(axis=1).any(): raise ValueError("มีข้อมูลกรอกไม่ครบ")
    if (~df["DOB"].str.fullmatch(r"\d{8}")).any(): raise ValueError("DOB ต้องเป็นตัวเลข 8 หลัก")
    if df["Name"].duplicated(keep=False).any(): raise ValueError("พบชื่อ (Name) ซ้ำกัน")
    return df.reset_index(drop=True)

def normalize_result(dataframe: pd.DataFrame) -> pd.DataFrame:
    if [c for c in RESULT_COLUMNS if c not in dataframe.columns]: return pd.DataFrame(columns=RESULT_COLUMNS)
    df = dataframe[RESULT_COLUMNS].copy()
    for col in RESULT_COLUMNS: df[col] = df[col].map(clean_cell)
    return df.loc[~df[RESULT_COLUMNS].eq("").all(axis=1)].copy().reset_index(drop=True)

def validate_result_data(result_df: pd.DataFrame, master_df: pd.DataFrame):
    if result_df.empty: return False, ""
    if [c for c in RESULT_COLUMNS if c not in result_df.columns]: return False, "หัวคอลัมน์ไม่ครบ"
    if result_df[RESULT_COLUMNS].eq("").any(axis=None): return False, "มีข้อมูลว่าง"
    master_names = master_df["Name"].tolist()
    result_names = result_df["Name"].tolist()
    if len(result_df) != len(master_df) or len(set(result_names)) != len(master_names): return False, "รายชื่อไม่ครบ/ซ้ำ"
    result_by_name = result_df.set_index("Name").to_dict(orient="index")
    for giver, row in result_by_name.items():
        target = row["Monito"]
        if target not in master_names: return False, "พบ Monito ไม่มีในรายชื่อ"
        if giver == target: return False, "จับได้ตัวเอง"
        if result_by_name.get(target, {}).get("Monito") == giver: return False, "จับสลับกันเอง"
    return True, ""

def config_values(config_df: pd.DataFrame) -> dict:
    if config_df is None or config_df.empty: return {}
    return {clean_cell(r["Key"]): clean_cell(r["Value"]) for _, r in config_df.iterrows() if clean_cell(r.get("Key"))}

# ---------------------------------------------------------
# Authentication
# ---------------------------------------------------------
def stored_admin_credentials(config_df: pd.DataFrame) -> dict:
    values = config_values(config_df)
    if not values.get("admin_salt") or not values.get("admin_hash"): return {}
    return {"salt": values["admin_salt"], "hash": values["admin_hash"], "iterations": int(values.get("admin_iterations", PBKDF2_ITERATIONS))}

def password_digest(password: str, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations).hex()

def verify_admin_password(password: str, credentials: dict) -> bool:
    if not credentials: return False
    return hmac.compare_digest(password_digest(password, credentials["salt"], credentials["iterations"]), credentials["hash"])

def save_admin_password(password: str):
    salt = __import__("secrets").token_hex(24)
    current_config = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, ttl=0)
    current_config = current_config[~current_config["Key"].isin(["admin_salt", "admin_hash", "admin_iterations", "admin_initialized"])]
    new_creds = pd.DataFrame([
        {"Key": "admin_salt", "Value": salt},
        {"Key": "admin_hash", "Value": password_digest(password, salt, PBKDF2_ITERATIONS)},
        {"Key": "admin_iterations", "Value": str(PBKDF2_ITERATIONS)},
        {"Key": "admin_initialized", "Value": "1"},
    ])
    conn.update(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, data=pd.concat([current_config, new_creds], ignore_index=True))
    st.cache_data.clear()
    write_log("Admin password was changed/initialized.")

def admin_is_authenticated() -> bool:
    if not st.session_state.get("admin_authenticated", False): return False
    if get_bkk_time().timestamp() > float(st.session_state.get("admin_expires_at", 0)):
        st.session_state["admin_authenticated"] = False
        return False
    st.session_state["admin_expires_at"] = get_bkk_time().timestamp() + ADMIN_SESSION_SECONDS
    return True

def render_admin_login(config_df: pd.DataFrame):
    credentials, already_initialized = stored_admin_credentials(config_df), config_values(config_df).get("admin_initialized") == "1"
    authenticated = admin_is_authenticated()
    with st.expander("🔐 สำหรับสตาฟ / Admin", expanded=not authenticated):
        if not authenticated:
            if credentials: st.caption("เข้าสู่ระบบด้วยรหัส Admin ปัจจุบัน")
            elif already_initialized: st.error("ข้อมูลรหัสสูญหาย")
            else: st.warning("ตั้งค่าครั้งแรก: ใส่รหัส Bootstrap")
            pwd = st.text_input("รหัสผ่าน Admin", type="password")
            if st.button("เข้าสู่ระบบผู้ดูแล"):
                valid = verify_admin_password(pwd, credentials) if credentials else (bool(BOOTSTRAP_ADMIN_PASSWORD) and hmac.compare_digest(pwd, BOOTSTRAP_ADMIN_PASSWORD) if not already_initialized else False)
                if valid:
                    st.session_state.update({"admin_authenticated": True, "admin_expires_at": get_bkk_time().timestamp() + ADMIN_SESSION_SECONDS})
                    write_log("Admin logged in successfully.")
                    st.rerun()
                else: st.error("รหัสไม่ถูกต้อง")
        else:
            st.success("เข้าสู่ระบบผู้ดูแลแล้ว")
            if st.button("ออกจากระบบ Admin"):
                st.session_state["admin_authenticated"] = False
                st.rerun()
            if not credentials:
                st.warning("กรุณาตั้งรหัส Admin ใหม่ก่อนเริ่มใช้งานระบบ")
                _render_password_change_form("ตั้งรหัส Admin ครั้งแรก", "init_pwd")
            else:
                with st.expander("เปลี่ยนรหัสผ่าน Admin"): _render_password_change_form("เปลี่ยนรหัส Admin", "change_pwd")

def _render_password_change_form(title: str, form_key: str):
    st.markdown(f"**{title}**")
    with st.form(form_key):
        new_pwd = st.text_input("รหัส Admin ใหม่ (อย่างน้อย 6 ตัวอักษร)", type="password")
        conf_pwd = st.text_input("ยืนยันรหัส Admin ใหม่", type="password")
        if st.form_submit_button("บันทึก"):
            if len(new_pwd) < 6: st.error("รหัสต้องมีอย่างน้อย 6 ตัวอักษร")
            elif new_pwd != conf_pwd: st.error("รหัสไม่ตรงกัน")
            else:
                try:
                    save_admin_password(new_pwd)
                    st.session_state["admin_authenticated"] = True
                    st.success("บันทึกสำเร็จ!")
                    time.sleep(1)
                    st.rerun()
                except Exception: st.error("บันทึกไม่สำเร็จ")

# ---------------------------------------------------------
# Algorithm Generator
# ---------------------------------------------------------
def run_draw(num_circles: int, locked_pairs_list: list, blocked_pairs_list: list):
    names = df_master["Name"].tolist()
    if len(names) < 3:
        st.error("ต้องมีผู้เข้าร่วมอย่างน้อย 3 คน")
        return

    locked_pairs = {p["giver"]: p["target"] for p in locked_pairs_list}
    blocked_edges = set((p["giver"], p["target"]) for p in blocked_pairs_list)

    targets = set()
    for g, t in locked_pairs.items():
        if g not in names or t not in names: return st.error(f"รายชื่อล็อก ({g} หรือ {t}) ไม่อยู่ในฐานข้อมูล")
        if t in targets: return st.error(f"มีการล็อกให้เป้าหมาย '{t}' ซ้ำกันมากกว่า 1 คน")
        if (g, t) in blocked_edges: return st.error(f"ข้อขัดแย้ง: {g} ถูกล็อกให้จับ {t} แต่มีกฎห้ามจับคู่นี้อยู่")
        targets.add(t)

    next_node = locked_pairs.copy()
    in_degree = {n: 0 for n in names}
    for g, t in next_node.items(): in_degree[t] += 1

    paths, visited, forced_cycles = [], set(), []
    for n in names:
        if in_degree[n] == 0:
            path, curr = [], n
            while curr is not None:
                path.append(curr); visited.add(curr); curr = next_node.get(curr)
            paths.append(path)
    for n in names:
        if n not in visited:
            path, curr = [], n
            while curr not in visited:
                path.append(curr); visited.add(curr); curr = next_node.get(curr)
            forced_cycles.append(path)

    if len(forced_cycles) > num_circles: return st.error(f"การล็อกเกิดวงปิดแล้ว ({len(forced_cycles)} วง) เกินวงที่ตั้งไว้ กรุณาเพิ่มวง")
    if forced_cycles and num_circles == 1 and len(forced_cycles[0]) < len(names): return st.error("วงย่อยขัดแย้งกับสุ่ม 1 วงใหญ่ กรุณาเพิ่มวง หรือลบการล็อก")

    remaining_circles = num_circles - len(forced_cycles)
    if paths and remaining_circles <= 0: return st.error("วงเต็มแล้ว กรุณาเพิ่มวง")

    valid_final_pairs = None
    for _ in range(2000):
        random.SystemRandom().shuffle(paths)
        groups = [[] for _ in range(remaining_circles)]
        for idx, p in enumerate(paths): groups[idx % remaining_circles].append(p)

        final_pairs = {}
        for c in forced_cycles:
            for i in range(len(c)): final_pairs[c[i]] = c[(i+1)%len(c)]

        is_invalid = False
        for g in groups:
            if not g: continue
            if sum(len(p) for p in g) < 3: is_invalid = True; break
            for i in range(len(g)):
                curr_path, next_path = g[i], g[(i+1)%len(g)]
                for j in range(len(curr_path)-1): final_pairs[curr_path[j]] = curr_path[j+1]
                if (curr_path[-1], next_path[0]) in blocked_edges: is_invalid = True; break
                final_pairs[curr_path[-1]] = next_path[0]
            if is_invalid: break
        if not is_invalid: valid_final_pairs = final_pairs; break
            
    if not valid_final_pairs: return st.error("หาการจับคู่ไม่พบ! เงื่อนไขขัดแย้งกันเกินไป กรุณาลดกฎลง")

    if len(valid_final_pairs) != len(names) or any(k == v for k, v in valid_final_pairs.items()) or any(valid_final_pairs.get(v) == k for k, v in valid_final_pairs.items()):
        return st.error("ตรวจพบการชนกัน จึงยกเลิกการบันทึก")

    result_to_save = df_master[["Name", "Dept", "Year"]].copy()
    result_to_save["Monito"] = result_to_save["Name"].map(valid_final_pairs)
    
    try:
        conn.update(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, data=result_to_save[RESULT_COLUMNS])
        st.cache_data.clear()
        write_log(f"Draw generated successfully ({num_circles} circles).")
        st.success("สุ่มและบันทึกผลสำเร็จ!")
        time.sleep(1.5)
        st.rerun()
    except Exception: st.error("บันทึกผลไม่สำเร็จ")
    finally:
        set_drawing_lock(False)

# ---------------------------------------------------------
# App Main Flow
# ---------------------------------------------------------
master_error = ""
try: df_master = prepare_master(df_raw)
except ValueError as exc: df_master, master_error = pd.DataFrame(columns=REQUIRED_MASTER_COLUMNS), str(exc)

df_result = normalize_result(df_result_raw)
result_exists = not df_result.empty
result_is_valid = False
if result_exists and not master_error and not df_master.empty: result_is_valid, _ = validate_result_data(df_result, df_master)

render_admin_login(df_config)
st.divider()

def display_label(row) -> str: return f"{row['Name']} ({row['Dept']} #{row['Year']})"

def order_results_by_cycle(result_df: pd.DataFrame) -> pd.DataFrame:
    if result_df.empty: return pd.DataFrame(columns=["วง", "ลำดับในวง"] + RESULT_COLUMNS)
    names = result_df["Name"].tolist()
    next_person = dict(zip(result_df["Name"], result_df["Monito"]))
    visited, ordered_names, circle_by_name, position_by_name, circle_number = set(), [], {}, {}, 0
    for start in names:
        if start in visited: continue
        circle_number += 1
        curr, circle_names = start, []
        while curr not in circle_names:
            circle_names.append(curr); curr = next_person[curr]
        for position, name in enumerate(circle_names, start=1):
            visited.add(name); ordered_names.append(name)
            circle_by_name[name] = circle_number; position_by_name[name] = position
    export_df = result_df.set_index("Name", drop=False).loc[ordered_names].reset_index(drop=True)
    export_df.insert(0, "วง", export_df["Name"].map(circle_by_name))
    export_df.insert(1, "ลำดับในวง", export_df["Name"].map(position_by_name))
    return export_df

def create_offline_excel(result_df: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    export_df = order_results_by_cycle(result_df)
    person_labels = {row["Name"]: display_label(row) for _, row in df_master.iterrows()}
    export_df["Monito"] = export_df["Monito"].map(person_labels).fillna(export_df["Monito"])
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False, sheet_name="ผลจับฉลาก")
        ws = writer.sheets["ผลจับฉลาก"]
        ws.freeze_panes = "A2"
        ws.column_dimensions["C"].width = 24; ws.column_dimensions["F"].width = 40
    return buffer.getvalue()

if result_exists:
    if master_error: st.error("ข้อผิดพลาด: " + master_error)
    elif not result_is_valid: st.error("ผลจับฉลากยังไม่ผ่านการตรวจสอบ (ขัดแย้งรายชื่อหลัก)")
    else:
        st.markdown(f'<div class="status-box"><h4 style="margin:0;">✅ ระบบเปิดให้ดูผลแล้ว</h4><p style="margin:5px 0 0 0;">ผู้เข้าร่วม: {len(df_result)} คน</p></div>', unsafe_allow_html=True)
        result_lookup = df_result.set_index("Name").to_dict(orient="index")
        dob_lookup = df_master.set_index("Name")["DOB"].to_dict()
        name_to_label = {row["Name"]: display_label(row) for _, row in df_result.iterrows()}
        selected_name = st.selectbox("👤 1. เลือกชื่อของคุณ", options=[""] + df_result["Name"].tolist(), format_func=lambda value: "-- เลือกชื่อ --" if value == "" else name_to_label[value])

        if st.session_state.get("last_selected_participant") != selected_name:
            st.session_state.pop("verified_participant_name", None); st.session_state["last_selected_participant"] = selected_name

        if selected_name:
            attempts_key = "dob_attempts_" + hashlib.sha256(selected_name.encode("utf-8")).hexdigest()[:16]
            if int(st.session_state.get(attempts_key, 0)) >= MAX_DOB_ATTEMPTS_PER_SESSION: 
                st.error("กรอกรหัสผิดเกินกำหนด")
                write_log(f"Locked out: {selected_name} exceeded max DOB attempts.")
            else:
                with st.form("participant_result_form"):
                    entered_dob = st.text_input("🔑 2. รหัสผ่าน (DDMMYYYY)", type="password")
                    if st.form_submit_button("👀 ดูผลจับฉลาก"):
                        if normalize_dob(entered_dob) and hmac.compare_digest(normalize_dob(entered_dob), dob_lookup.get(selected_name, "")):
                            st.session_state.update({"verified_participant_name": selected_name, "verified_participant_until": get_bkk_time().timestamp() + 300, attempts_key: 0})
                        else:
                            st.session_state[attempts_key] = int(st.session_state.get(attempts_key, 0)) + 1
                            st.error("รหัสไม่ถูกต้อง")

            if st.session_state.get("verified_participant_name") == selected_name and get_bkk_time().timestamp() <= float(st.session_state.get("verified_participant_until", 0)):
                st.success("🎉 Monito ของคุณคือ:")
                monito_name = result_lookup[selected_name]["Monito"]
                st.subheader(name_to_label.get(monito_name, monito_name))
                st.button("🙈 ซ่อนผล", on_click=lambda: st.session_state.pop("verified_participant_name", None), use_container_width=True)

    if admin_is_authenticated():
        st.divider(); st.subheader("🛠️ เครื่องมือ Admin")
        if result_is_valid:
            with st.expander("🔎 ค้นหาผลให้ผู้ที่ลืม Monito"):
                admin_search_name = st.selectbox(
                    "เลือกชื่อผู้เข้าร่วมที่ต้องการค้นหา", 
                    options=[""] + df_result["Name"].tolist(), 
                    format_func=lambda value: "-- เลือกชื่อ --" if value == "" else name_to_label[value],
                    key="admin_search_dropdown"
                )
                if admin_search_name:
                    found = df_result[df_result["Name"] == admin_search_name].copy()
                    if not found.empty:
                        found["Monito"] = found["Monito"].map(name_to_label).fillna(found["Monito"])
                        st.dataframe(found, use_container_width=True, hide_index=True)
            
            try: st.download_button("⬇️ ดาวน์โหลด Excel สำรอง", data=create_offline_excel(df_result), file_name="Monito_Offline.xlsx")
            except Exception: pass

        with st.expander("⚠️ ล้างผลจับฉลาก (เริ่มค่ายใหม่)"):
            st.warning("การล้างผล ระบบจะดึงผลปัจจุบันไปเก็บไว้ในแท็บ Backup อัตโนมัติ ป้องกันข้อมูลสูญหาย")
            reset_pwd = st.text_input("กรอกรหัสผ่าน Admin เพื่อยืนยัน", type="password", key="reset_confirm_pwd")
            if st.button("🗑️ ยืนยันการล้างผล", type="primary", use_container_width=True):
                credentials = stored_admin_credentials(df_config)
                if not verify_admin_password(reset_pwd, credentials):
                    st.error("รหัสผ่านไม่ถูกต้อง ล้างผลไม่สำเร็จ")
                elif is_drawing_in_progress():
                    st.error("❌ มีสตาฟท่านอื่นกำลังประมวลผลอยู่ กรุณารอสักครู่")
                else:
                    try:
                        set_drawing_lock(True)
                        backup_sheet_name = f"Backup_{get_bkk_time().strftime('%d%b_%H%M')}"
                        
                        # --- แก้ไขการสร้างแท็บ Backup ให้เสถียร 100% ---
                        try:
                            # ดึง gspread client ออกมาใช้สร้างแท็บใหม่ตรงๆ
                            sh = conn.client.open_by_url(SPREADSHEET_URL)
                            try:
                                sh.worksheet(backup_sheet_name) # ลองดูว่ามีแท็บนี้หรือยัง
                            except:
                                sh.add_worksheet(title=backup_sheet_name
