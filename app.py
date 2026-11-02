import hashlib
import hmac
import io
import random
import re
import time
from copy import copy

import pandas as pd
import streamlit as st
from streamlit_gsheets import GSheetsConnection

# =========================================================
# Monito AnurakSciCU
# - เพิ่มระบบล็อกผล (Fixed Matches) จับคู่ล่วงหน้า
# - ลดความยาวรหัสผ่าน Admin เป็น 6 ตัว
# - ป้องกัน Rate Limit จาก Google Sheets (ttl=30)
# =========================================================

st.set_page_config(
    page_title="Monito AnurakSciCU",
    page_icon="🎁",
    layout="centered",
)

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
        .lock-box { background: #e3f2fd; padding: 15px; border-radius: 8px; margin-bottom: 10px; border: 1px solid #90caf9;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🌲 Monito AnurakSciCU 🦉")
st.caption("ระบบจับฉลากบัดดี้ออนไลน์")

# ---------------------------------------------------------
# Settings
# ---------------------------------------------------------
MASTER_SHEET = "รายชื่อ"
RESULT_SHEET = "ผลจับฉลาก"
CONFIG_SHEET = "ตั้งค่า"
REQUIRED_MASTER_COLUMNS = ["Name", "Dept", "Year", "DOB"]
RESULT_COLUMNS = ["Name", "Dept", "Year", "Monito"]
PBKDF2_ITERATIONS = 600_000
ADMIN_SESSION_SECONDS = 30 * 60
MAX_DOB_ATTEMPTS_PER_SESSION = 5

try:
    SPREADSHEET_URL = str(st.secrets["app"]["spreadsheet_url"]).strip()
except Exception:
    st.error("ยังไม่ได้ตั้งค่า app.spreadsheet_url ใน Streamlit Secrets")
    st.stop()

BOOTSTRAP_ADMIN_PASSWORD = str(st.secrets.get("ADMIN_BOOTSTRAP_PASSWORD", ""))

try:
    conn = st.connection("gsheets", type=GSheetsConnection)
    
    # อ่านแท็บรายชื่อ (ตั้ง ttl=30 ลดภาระ API กัน Google บล็อก)
    df_raw = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=MASTER_SHEET, ttl=30)

    # อ่านแท็บผลจับฉลาก
    try:
        df_result_raw = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, ttl=30)
    except pd.errors.EmptyDataError:
        df_result_raw = pd.DataFrame()

    if df_result_raw.empty and not set(RESULT_COLUMNS).issubset(df_result_raw.columns):
        empty_row = {col: "" for col in RESULT_COLUMNS}
        conn.update(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, data=pd.DataFrame([empty_row]))
        st.cache_data.clear()
        df_result_raw = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, ttl=30)

    # อ่านแท็บตั้งค่า
    try:
        df_config = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, ttl=30)
    except pd.errors.EmptyDataError:
        df_config = pd.DataFrame()

    if df_config.empty and not {"Key", "Value"}.issubset(df_config.columns):
        conn.update(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, data=pd.DataFrame([{"Key": "", "Value": ""}]))
        st.cache_data.clear()
        df_config = conn.read(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, ttl=30)

except Exception as e:
    st.error("ไม่สามารถอ่าน Google Sheets ได้ กรุณาตรวจสอบลิงก์และสิทธิ์การเข้าถึง")
    st.stop()

# ---------------------------------------------------------
# Data validation
# ---------------------------------------------------------
def clean_cell(value) -> str:
    try:
        if pd.isna(value): return ""
    except (TypeError, ValueError): pass
    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}: return ""
    if re.fullmatch(r"-?\d+\.0", text): text = text[:-2]
    return text

def normalize_dob(value) -> str:
    text = clean_cell(value)
    if text.isdigit() and 0 < len(text) < 8: text = text.zfill(8)
    return text

def prepare_master(dataframe: pd.DataFrame) -> pd.DataFrame:
    if [c for c in REQUIRED_MASTER_COLUMNS if c not in dataframe.columns]:
        raise ValueError("แท็บ 'รายชื่อ' ต้องมีหัวคอลัมน์ Name, Dept, Year, DOB")
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
    df = df.loc[~df[RESULT_COLUMNS].eq("").all(axis=1)].copy()
    return df.reset_index(drop=True)

def validate_result_data(result_df: pd.DataFrame, master_df: pd.DataFrame):
    if result_df.empty: return False, ""
    if [c for c in RESULT_COLUMNS if c not in result_df.columns]: return False, "หัวคอลัมน์ไม่ครบ"
    if result_df[RESULT_COLUMNS].eq("").any(axis=None): return False, "มีข้อมูลว่าง"
    master_names = master_df["Name"].tolist()
    result_names = result_df["Name"].tolist()
    if len(result_df) != len(master_df) or len(set(result_names)) != len(master_names): return False, "รายชื่อไม่ครบ/ซ้ำ"
    result_by_name = result_df.set_index("Name").to_dict(orient="index")
    allowed_names = set(master_names)
    for giver, row in result_by_name.items():
        target = row["Monito"]
        if target not in allowed_names: return False, "พบ Monito ไม่มีในรายชื่อ"
        if giver == target: return False, "จับได้ตัวเอง"
        if result_by_name.get(target, {}).get("Monito") == giver: return False, "จับสลับกันเอง"
    return True, ""

# ---------------------------------------------------------
# Admin password Setup
# ---------------------------------------------------------
def config_values(config_df: pd.DataFrame) -> dict:
    if config_df is None or config_df.empty: return {}
    output = {}
    for _, row in config_df.iterrows():
        key, value = clean_cell(row.get("Key", "")), clean_cell(row.get("Value", ""))
        if key: output[key] = value
    return output

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
    config_to_save = pd.DataFrame([
        {"Key": "admin_salt", "Value": salt},
        {"Key": "admin_hash", "Value": password_digest(password, salt, PBKDF2_ITERATIONS)},
        {"Key": "admin_iterations", "Value": str(PBKDF2_ITERATIONS)},
        {"Key": "admin_initialized", "Value": "1"},
    ])
    conn.update(spreadsheet=SPREADSHEET_URL, worksheet=CONFIG_SHEET, data=config_to_save)
    st.cache_data.clear()

def admin_is_authenticated() -> bool:
    if not st.session_state.get("admin_authenticated", False): return False
    if time.time() > float(st.session_state.get("admin_expires_at", 0)):
        st.session_state["admin_authenticated"] = False
        return False
    st.session_state["admin_expires_at"] = time.time() + ADMIN_SESSION_SECONDS
    return True

def render_admin_login(config_df: pd.DataFrame):
    credentials = stored_admin_credentials(config_df)
    cfg_values = config_values(config_df)
    already_initialized = cfg_values.get("admin_initialized", "") == "1"
    authenticated = admin_is_authenticated()

    with st.expander("🔐 สำหรับสตาฟ / Admin", expanded=not authenticated):
        if not authenticated:
            if credentials: st.caption("เข้าสู่ระบบด้วยรหัส Admin ปัจจุบัน")
            elif already_initialized: st.error("ข้อมูลรหัสสูญหาย")
            else: st.warning("ตั้งค่าครั้งแรก: ใส่รหัส Bootstrap")
            
            entered_password = st.text_input("รหัสผ่าน Admin", type="password")
            if st.button("เข้าสู่ระบบผู้ดูแล"):
                valid = verify_admin_password(entered_password, credentials) if credentials else (bool(BOOTSTRAP_ADMIN_PASSWORD) and hmac.compare_digest(entered_password, BOOTSTRAP_ADMIN_PASSWORD) if not already_initialized else False)
                if valid:
                    st.session_state["admin_authenticated"] = True
                    st.session_state["admin_expires_at"] = time.time() + ADMIN_SESSION_SECONDS
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
                with st.expander("เปลี่ยนรหัสผ่าน Admin"):
                    _render_password_change_form("เปลี่ยนรหัส Admin", "change_pwd")

def _render_password_change_form(title: str, form_key: str):
    st.markdown(f"**{title}**")
    with st.form(form_key):
        new_password = st.text_input("รหัส Admin ใหม่ (อย่างน้อย 6 ตัวอักษร)", type="password") # เปลี่ยนเป็น 6 ตัว
        confirm_password = st.text_input("ยืนยันรหัส Admin ใหม่", type="password")
        submitted = st.form_submit_button("บันทึก")
    if submitted:
        if len(new_password) < 6: st.error("รหัสต้องมีอย่างน้อย 6 ตัวอักษร") # แจ้งเตือน 6 ตัว
        elif new_password != confirm_password: st.error("รหัสไม่ตรงกัน")
        else:
            try:
                save_admin_password(new_password)
                st.session_state["admin_authenticated"] = True
                st.success("บันทึกสำเร็จ!")
                time.sleep(1)
                st.rerun()
            except Exception: st.error("บันทึกไม่สำเร็จ")

# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------
master_error = ""
try: df_master = prepare_master(df_raw)
except ValueError as exc:
    df_master = pd.DataFrame(columns=REQUIRED_MASTER_COLUMNS)
    master_error = str(exc)

df_result = normalize_result(df_result_raw)
result_exists = not df_result.empty
result_is_valid = False
if result_exists and not master_error and not df_master.empty:
    result_is_valid, result_validation_message = validate_result_data(df_result, df_master)

render_admin_login(df_config)
st.divider()

def display_label(row) -> str: return f"{row['Name']} ({row['Dept']} #{row['Year']})"

def order_results_by_cycle(result_df: pd.DataFrame) -> pd.DataFrame:
    ordered_source = result_df[RESULT_COLUMNS].copy()
    if ordered_source.empty:
        ordered_source.insert(0, "วง", pd.Series(dtype="int64"))
        ordered_source.insert(1, "ลำดับในวง", pd.Series(dtype="int64"))
        return ordered_source
    names = ordered_source["Name"].tolist()
    next_person = dict(zip(ordered_source["Name"], ordered_source["Monito"]))
    if len(next_person) != len(names) or set(next_person.values()) != set(names): raise ValueError("ผลจับฉลากไม่ใช่วงปิดที่สมบูรณ์")
    visited = set()
    ordered_names = []
    circle_by_name = {}
    position_by_name = {}
    circle_number = 0
    for start in names:
        if start in visited: continue
        circle_number += 1
        current = start
        circle_names = []
        while current not in circle_names:
            if current in visited: raise ValueError("พบวงจับฉลากเชื่อมกันผิดรูปแบบ")
            circle_names.append(current)
            current = next_person[current]
        if current != start: raise ValueError("วงจับฉลากไม่ย้อนกลับมายังจุดเริ่มต้น")
        for position, name in enumerate(circle_names, start=1):
            visited.add(name)
            ordered_names.append(name)
            circle_by_name[name] = circle_number
            position_by_name[name] = position
    indexed = ordered_source.set_index("Name", drop=False)
    export_df = indexed.loc[ordered_names].reset_index(drop=True)
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
        ws.auto_filter.ref = ws.dimensions
        ws.column_dimensions["C"].width = 24
        ws.column_dimensions["F"].width = 40
        for cell in ws[1]: cell.font = copy(cell.font); cell.font.bold = True
    return buffer.getvalue()

# ---------------------------------------------------------
# Algorithm Generator with Lock Pairs
# ---------------------------------------------------------
def run_draw(num_circles: int, locked_pairs_list: list):
    participant_names = df_master["Name"].tolist()
    if len(participant_names) < 3:
        st.error("ต้องมีผู้เข้าร่วมอย่างน้อย 3 คน")
        return

    locked_pairs = {p["giver"]: p["target"] for p in locked_pairs_list}
    
    # 1. Validation Locks
    targets = set()
    for g, t in locked_pairs.items():
        if g not in participant_names or t not in participant_names:
            st.error(f"รายชื่อล็อก ({g} หรือ {t}) ไม่อยู่ในฐานข้อมูล")
            return
        if g == t:
            st.error(f"ไม่อนุญาตให้ {g} จับได้ตัวเอง")
            return
        if t in targets:
            st.error(f"มีการล็อกให้เป้าหมาย '{t}' ซ้ำกันมากกว่า 1 คน")
            return
        targets.add(t)

    # 2. Build Super Nodes (Paths)
    next_node = locked_pairs.copy()
    in_degree = {n: 0 for n in participant_names}
    for g, t in next_node.items(): in_degree[t] += 1

    paths = []
    visited = set()
    for n in participant_names:
        if in_degree[n] == 0:
            path = []
            curr = n
            while curr is not None:
                path.append(curr)
                visited.add(curr)
                curr = next_node.get(curr)
            paths.append(path)

    forced_cycles = []
    for n in participant_names:
        if n not in visited:
            path = []
            curr = n
            while curr not in visited:
                path.append(curr)
                visited.add(curr)
                curr = next_node.get(curr)
            forced_cycles.append(path)

    # 3. Check Circle Constraints
    if len(forced_cycles) > num_circles:
        st.error(f"การล็อกทำให้เกิดวงปิดแล้ว ({len(forced_cycles)} วง) ซึ่งเกินกว่าจำนวนวงที่ตั้งไว้ ({num_circles} วง) กรุณาเพิ่มจำนวนวง หรือลบการล็อกออก")
        return
    if forced_cycles and num_circles == 1 and len(forced_cycles[0]) < len(participant_names):
        st.error("การล็อกทำให้เกิดวงย่อยซ้อนทับ ขัดแย้งกับการสุ่ม 1 วงใหญ่ กรุณาเพิ่มจำนวนวง")
        return

    remaining_circles = num_circles - len(forced_cycles)
    if paths and remaining_circles <= 0:
        st.error("จำนวนวงเต็มจากการล็อกแล้ว ไม่สามารถจัดคนที่เหลือลงวงได้ กรุณาเพิ่มจำนวนวง")
        return

    random.SystemRandom().shuffle(paths)
    groups = [[] for _ in range(remaining_circles)]
    for idx, p in enumerate(paths):
        groups[idx % remaining_circles].append(p)

    final_pairs = {}
    for c in forced_cycles:
        for i in range(len(c)): final_pairs[c[i]] = c[(i+1)%len(c)]

    for g in groups:
        if not g: continue
        group_size = sum(len(p) for p in g)
        if group_size < 3:
            st.error(f"พบวงที่มีขนาดเล็กกว่า 3 คน (มี {group_size} คน) ขัดแย้งกฎป้องกันการจับสลับกันเอง กรุณาปรับลดจำนวนวง")
            return
        
        for i in range(len(g)):
            current_path = g[i]
            next_path = g[(i+1)%len(g)]
            for j in range(len(current_path)-1): final_pairs[current_path[j]] = current_path[j+1]
            final_pairs[current_path[-1]] = next_path[0]

    # 4. Final Security Check
    if len(final_pairs) != len(participant_names) or any(k == v for k, v in final_pairs.items()) or any(final_pairs.get(v) == k for k, v in final_pairs.items()):
        st.error("ระบบตรวจสอบพบการชนกัน หรือจับได้ตัวเอง/สลับกันเอง จึงยกเลิกการบันทึก")
        return

    # 5. Save Results
    result_to_save = df_master[["Name", "Dept", "Year"]].copy()
    result_to_save["Monito"] = result_to_save["Name"].map(final_pairs)
    
    try:
        conn.update(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, data=result_to_save[RESULT_COLUMNS])
        st.cache_data.clear()
        st.success("สุ่มและบันทึกผลจับฉลาก (รวมการล็อก) สำเร็จ!")
        time.sleep(1.5)
        st.rerun()
    except Exception:
        st.error("บันทึกผลไม่สำเร็จ กรุณาตรวจสอบสิทธิ์ Google Sheets")

# ---------------------------------------------------------
# Participant view & Admin Tools
# ---------------------------------------------------------
if result_exists:
    if master_error: st.error("ข้อผิดพลาด: " + master_error)
    elif not result_is_valid: st.error("ผลจับฉลากยังไม่ผ่านการตรวจสอบ (ขัดแย้งรายชื่อหลัก)")
    else:
        st.markdown(f'<div class="status-box"><h4 style="margin:0;">✅ ระบบเปิดให้ดูผลแล้ว</h4><p style="margin:5px 0 0 0;">ผู้เข้าร่วม: {len(df_result)} คน</p></div>', unsafe_allow_html=True)
        result_lookup = df_result.set_index("Name").to_dict(orient="index")
        dob_lookup = df_master.set_index("Name")["DOB"].to_dict()
        name_to_label = {row["Name"]: display_label(row) for _, row in df_result.iterrows()}
        selected_name = st.selectbox("👤 1. เลือกชื่อของคุณ", options=[""] + df_result["Name"].tolist(), format_func=lambda value: "-- กรุณาเลือกชื่อ --" if value == "" else name_to_label[value])

        if st.session_state.get("last_selected_participant") != selected_name:
            st.session_state.pop("verified_participant_name", None)
            st.session_state["last_selected_participant"] = selected_name

        if selected_name:
            attempts_key = "dob_attempts_" + hashlib.sha256(selected_name.encode("utf-8")).hexdigest()[:16]
            attempts = int(st.session_state.get(attempts_key, 0))

            if attempts >= MAX_DOB_ATTEMPTS_PER_SESSION: st.error("กรอกรหัสผิดเกินกำหนด")
            else:
                with st.form("participant_result_form"):
                    entered_dob = st.text_input("🔑 2. รหัสผ่าน (DDMMYYYY)", type="password")
                    submitted = st.form_submit_button("👀 ดูผลจับฉลาก")

                if submitted:
                    expected_dob = dob_lookup.get(selected_name, "")
                    normalized_entered_dob = normalize_dob(entered_dob)
                    if normalized_entered_dob and hmac.compare_digest(normalized_entered_dob, expected_dob):
                        st.session_state["verified_participant_name"] = selected_name
                        st.session_state["verified_participant_until"] = time.time() + 300
                        st.session_state[attempts_key] = 0
                    else:
                        st.session_state[attempts_key] = attempts + 1
                        st.error("รหัสไม่ถูกต้อง กรุณาลองใหม่")

            verified_name = st.session_state.get("verified_participant_name")
            if verified_name == selected_name and time.time() <= float(st.session_state.get("verified_participant_until", 0)):
                st.success("🎉 Monito ของคุณคือ:")
                monito_name = result_lookup[selected_name]["Monito"]
                st.subheader(name_to_label.get(monito_name, monito_name))
                st.button("🙈 ซ่อนผล", on_click=lambda: st.session_state.pop("verified_participant_name", None), use_container_width=True)

    if admin_is_authenticated():
        st.divider()
        st.subheader("🛠️ เครื่องมือ Admin")
        if result_is_valid:
            with st.expander("🔎 ค้นหาผลให้ผู้ที่ลืม Monito"):
                search_name = st.text_input("พิมพ์ชื่อผู้เข้าร่วม")
                if search_name.strip():
                    found = df_result[df_result["Name"].str.casefold().str.contains(search_name.strip().casefold(), regex=False)].copy()
                    if found.empty: st.info("ไม่พบชื่อที่ค้นหา")
                    else:
                        found["Monito"] = found["Monito"].map(name_to_label).fillna(found["Monito"])
                        st.dataframe(found, use_container_width=True, hide_index=True)
            try:
                offline_file = create_offline_excel(df_result)
                st.download_button("⬇️ ดาวน์โหลดไฟล์ Excel สำรอง", data=offline_file, file_name="Monito_Offline.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            except Exception: pass

        if st.button("⚠️ ล้างผลจับฉลาก"):
            try:
                empty_row = {col: "" for col in RESULT_COLUMNS}
                conn.update(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, data=pd.DataFrame([empty_row]))
                st.cache_data.clear()
                st.session_state.pop("verified_participant_name", None)
                st.success("ล้างผลสำเร็จ")
                time.sleep(1)
                st.rerun()
            except Exception: st.error("ล้างผลไม่สำเร็จ")

# ---------------------------------------------------------
# Admin Draw Mode with Fixed Pairs (Lock)
# ---------------------------------------------------------
else:
    st.info("โหมดเตรียมการสำหรับสตาฟ")
    if master_error: st.error(master_error)
    elif df_master.empty: st.error("ไม่พบรายชื่อในแท็บ 'รายชื่อ'")
    else:
        st.write(f"พบรายชื่อผู้เข้าร่วม {len(df_master)} คน")
        if not admin_is_authenticated(): st.warning("ต้องเข้าสู่ระบบ Admin ก่อน จึงจะจัดวงและสุ่มได้")
        elif len(df_master) < 3: st.error("ต้องมีอย่างน้อย 3 คน")
        else:
            participant_names = df_master["Name"].tolist()

            # --- ระบบล็อกคู่ ---
            st.markdown("### 🔒 ล็อกผลจับคู่ล่วงหน้า (ไม่บังคับ)")
            st.caption("สามารถผูกคู่ที่ต้องการให้จับกันแน่นอน (เช่น พี่รหัสจับได้น้องรหัส) ส่วนคนที่เหลือระบบจะสุ่มให้อัตโนมัติ")
            
            if "locked_pairs" not in st.session_state:
                st.session_state.locked_pairs = []

            # แสดงคู่ที่ล็อกไว้
            if st.session_state.locked_pairs:
                for i, lock in enumerate(st.session_state.locked_pairs):
                    c1, c2, c3 = st.columns([4, 4, 2])
                    c1.markdown(f"**{lock['giver']}** ➡️ จับได้")
                    c2.markdown(f"**{lock['target']}**")
                    if c3.button("ลบ", key=f"del_lock_{i}"):
                        st.session_state.locked_pairs.pop(i)
                        st.rerun()

            # ฟอร์มเพิ่มคู่ล็อก
            with st.expander("➕ เพิ่มคู่ล็อกใหม่"):
                used_givers = [l['giver'] for l in st.session_state.locked_pairs]
                used_targets = [l['target'] for l in st.session_state.locked_pairs]
                
                avail_g = [n for n in participant_names if n not in used_givers]
                avail_t = [n for n in participant_names if n not in used_targets]

                col1, col2 = st.columns(2)
                with col1: giver = st.selectbox("ผู้จับ (ใคร)", [""] + avail_g)
                with col2: target = st.selectbox("เป้าหมาย (ได้ใคร)", [""] + avail_t)

                if st.button("บันทึกคู่นี้", use_container_width=True):
                    if not giver or not target: st.error("กรุณาเลือกทั้งสองช่อง")
                    elif giver == target: st.error("ไม่สามารถล็อกให้จับตัวเองได้")
                    else:
                        st.session_state.locked_pairs.append({"giver": giver, "target": target})
                        st.rerun()

            st.markdown("---")
            # --- จบระบบล็อกคู่ ---

            max_circles = max(1, len(df_master) // 3)
            num_circles = st.slider("🔄 ต้องการแบ่งการสุ่มเป็นกี่วง?", min_value=1, max_value=max_circles, value=1)
            
            if st.button("🎲 เริ่มสุ่มจับคู่และล็อกผล", use_container_width=True):
                with st.spinner("กำลังสุ่มจัดเรียง..."):
                    run_draw(num_circles, st.session_state.locked_pairs)
