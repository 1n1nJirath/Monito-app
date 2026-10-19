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
# - Google Sheets เป็นฐานข้อมูลหลัก
# - สุ่มหลายวงได้ โดยแต่ละวงเป็นวงปิดของตัวเอง
# - ไม่บันทึก DOB ลงแท็บผลจับฉลากหรือไฟล์ Excel สำหรับออฟไลน์
# - รหัส Admin เปลี่ยนได้จากหน้าเว็บ และเก็บเป็น salted hash
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
        div[data-baseweb="select"] > div {
            background-color: #ffffff;
            border-radius: 8px;
        }
        div[data-testid="stButton"] > button,
        div[data-testid="stDownloadButton"] > button {
            border-radius: 8px;
            font-weight: 600;
            width: 100%;
        }
        .status-box {
            background: #ffffff;
            padding: 18px;
            border-radius: 10px;
            border: 1px solid #dcdde1;
            margin-bottom: 20px;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🎈 Monito AnurakSciCU 🎁")
st.caption("ระบบจับฉลากบัดดี้ออนไลน์")

# ---------------------------------------------------------
# Settings
# ---------------------------------------------------------
MASTER_SHEET = "รายชื่อ"
RESULT_SHEET = "ผลจับฉลาก"
CONFIG_SHEET = "ตั้งค่า"  # แท็บภายในสำหรับเก็บ hash ของรหัส Admin
REQUIRED_MASTER_COLUMNS = ["Name", "Dept", "Year", "DOB"]
RESULT_COLUMNS = ["Name", "Dept", "Year", "Monito"]
PBKDF2_ITERATIONS = 600_000
ADMIN_SESSION_SECONDS = 30 * 60
MAX_DOB_ATTEMPTS_PER_SESSION = 5

try:
    SPREADSHEET_URL = str(st.secrets["app"]["spreadsheet_url"]).strip()
except Exception:
    st.error(
        "ยังไม่ได้ตั้งค่า app.spreadsheet_url ใน Streamlit Secrets "
        "กรุณาตั้งค่า URL ของ Google Sheets ก่อนใช้งาน"
    )
    st.stop()

BOOTSTRAP_ADMIN_PASSWORD = str(st.secrets.get("ADMIN_BOOTSTRAP_PASSWORD", ""))

try:
    conn = st.connection("gsheets", type=GSheetsConnection)
    # ttl=0 ลดโอกาสแสดงผลเก่าหลังสุ่มหรือรีเซ็ต
    df_raw = conn.read(
        spreadsheet=SPREADSHEET_URL,
        worksheet=MASTER_SHEET,
        ttl=0,
    )
    df_result_raw = conn.read(
        spreadsheet=SPREADSHEET_URL,
        worksheet=RESULT_SHEET,
        ttl=0,
    )
    df_config = conn.read(
        spreadsheet=SPREADSHEET_URL,
        worksheet=CONFIG_SHEET,
        ttl=0,
    )
except Exception:
    st.error(
        "ไม่สามารถอ่าน Google Sheets ได้ กรุณาตรวจสอบ URL, ชื่อแท็บ, "
        "สิทธิ์ Editor ของ Service Account และการตั้งค่า Streamlit Secrets "
        "รวมถึงการสร้างแท็บ 'ตั้งค่า' แล้ว"
    )
    st.stop()


# ---------------------------------------------------------
# Data normalization and validation
# ---------------------------------------------------------
def clean_cell(value) -> str:
    """Convert a Google Sheets cell to a trimmed string."""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass

    text = str(value).strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    # Convert values such as 2.0 to 2 when a numeric cell is read as float.
    if re.fullmatch(r"-?\d+\.0", text):
        text = text[:-2]
    return text


def normalize_dob(value) -> str:
    """Normalize DDMMYYYY; pad leading zero if Sheets stored it as a number."""
    text = clean_cell(value)
    if text.isdigit() and 0 < len(text) < 8:
        text = text.zfill(8)
    return text


def prepare_master(dataframe: pd.DataFrame) -> pd.DataFrame:
    missing_columns = [c for c in REQUIRED_MASTER_COLUMNS if c not in dataframe.columns]
    if missing_columns:
        raise ValueError(
            "แท็บ 'รายชื่อ' ต้องมีหัวคอลัมน์ Name, Dept, Year, DOB "
            "และสะกดตรงตามนี้ทุกคอลัมน์"
        )

    df = dataframe[REQUIRED_MASTER_COLUMNS].copy()
    for col in ["Name", "Dept", "Year"]:
        df[col] = df[col].map(clean_cell)
    df["DOB"] = df["DOB"].map(normalize_dob)

    # Ignore fully empty rows, but don't silently ignore partial participant rows.
    df = df.loc[~df[REQUIRED_MASTER_COLUMNS].eq("").all(axis=1)].copy()
    if df.empty:
        return df.reset_index(drop=True)

    incomplete = df[["Name", "Dept", "Year", "DOB"]].eq("").any(axis=1)
    if incomplete.any():
        raise ValueError(
            "มีแถวในแท็บ 'รายชื่อ' ที่กรอกข้อมูลไม่ครบ กรุณาตรวจสอบ Name, Dept, Year และ DOB"
        )

    invalid_dob = ~df["DOB"].str.fullmatch(r"\d{8}")
    if invalid_dob.any():
        raise ValueError(
            "DOB ต้องเป็นตัวเลข 8 หลักในรูปแบบ DDMMYYYY เช่น 05052003 "
            "แนะนำให้ตั้งคอลัมน์ DOB ใน Google Sheets เป็น Plain text"
        )

    # The current schema uses Name to identify participants and Monito recipients.
    # Require unique names to avoid ambiguous lookups and wrong pairings.
    if df["Name"].duplicated(keep=False).any():
        raise ValueError(
            "พบค่า Name ซ้ำกันในแท็บ 'รายชื่อ' กรุณาทำให้ชื่อไม่ซ้ำ "
            "เช่น เพิ่มชื่อจริง/ชื่อเล่นหรือข้อมูลแยกความแตกต่าง"
        )

    if df["DOB"].duplicated(keep=False).any():
        # Two people may share a birthday, so this is not an error; only DOB is not an identifier.
        pass

    return df.reset_index(drop=True)


def normalize_result(dataframe: pd.DataFrame) -> pd.DataFrame:
    missing_columns = [c for c in RESULT_COLUMNS if c not in dataframe.columns]
    if missing_columns:
        return pd.DataFrame(columns=RESULT_COLUMNS)

    df = dataframe[RESULT_COLUMNS].copy()
    for col in RESULT_COLUMNS:
        df[col] = df[col].map(clean_cell)
    df = df.loc[~df[RESULT_COLUMNS].eq("").all(axis=1)].copy()
    return df.reset_index(drop=True)


def validate_result_data(result_df: pd.DataFrame, master_df: pd.DataFrame):
    if result_df.empty:
        return False, ""

    missing = [c for c in RESULT_COLUMNS if c not in result_df.columns]
    if missing:
        return False, "แท็บผลจับฉลากมีหัวคอลัมน์ไม่ครบ กรุณาให้สตาฟตรวจสอบ"

    if result_df[RESULT_COLUMNS].eq("").any(axis=None):
        return False, "ผลจับฉลากมีข้อมูลว่างบางช่อง กรุณาให้สตาฟตรวจสอบก่อนเปิดให้ดูผล"

    master_names = master_df["Name"].tolist()
    result_names = result_df["Name"].tolist()
    if len(result_df) != len(master_df) or len(result_names) != len(set(result_names)):
        return False, "จำนวนแถวหรือรายชื่อในผลจับฉลากไม่ครบ/ซ้ำ กรุณาให้สตาฟตรวจสอบ"
    if set(result_names) != set(master_names):
        return False, "รายชื่อในแท็บผลจับฉลากไม่ตรงกับแท็บรายชื่อ กรุณาอย่าแก้รายชื่อหลังสุ่ม"

    result_by_name = result_df.set_index("Name").to_dict(orient="index")
    allowed_names = set(master_names)
    for giver, row in result_by_name.items():
        target = row["Monito"]
        if target not in allowed_names:
            return False, "พบชื่อ Monito ที่ไม่มีอยู่ในแท็บรายชื่อ กรุณาให้สตาฟตรวจสอบ"
        if giver == target:
            return False, "ตรวจพบรายการที่จับได้ตัวเอง กรุณาให้สตาฟตรวจสอบ"

    for giver, row in result_by_name.items():
        target = row["Monito"]
        if result_by_name.get(target, {}).get("Monito") == giver:
            return False, "ตรวจพบการจับสลับกันเอง กรุณาให้สตาฟตรวจสอบ"

    return True, ""


# ---------------------------------------------------------
# Admin password: PBKDF2 hash stored in the internal config tab
# ---------------------------------------------------------
def config_values(config_df: pd.DataFrame) -> dict:
    if config_df is None or config_df.empty:
        return {}
    if "Key" not in config_df.columns or "Value" not in config_df.columns:
        return {}
    output = {}
    for _, row in config_df.iterrows():
        key = clean_cell(row.get("Key", ""))
        value = clean_cell(row.get("Value", ""))
        if key:
            output[key] = value
    return output


def stored_admin_credentials(config_df: pd.DataFrame) -> dict:
    values = config_values(config_df)
    salt = values.get("admin_salt", "")
    password_hash = values.get("admin_hash", "")
    iterations_text = values.get("admin_iterations", str(PBKDF2_ITERATIONS))
    if not salt or not password_hash:
        return {}
    try:
        iterations = int(iterations_text)
    except (TypeError, ValueError):
        iterations = PBKDF2_ITERATIONS
    return {"salt": salt, "hash": password_hash, "iterations": iterations}


def password_digest(password: str, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()


def verify_admin_password(password: str, credentials: dict) -> bool:
    if not credentials:
        return False
    candidate = password_digest(password, credentials["salt"], credentials["iterations"])
    return hmac.compare_digest(candidate, credentials["hash"])


def save_admin_password(password: str):
    salt = __import__("secrets").token_hex(24)
    digest = password_digest(password, salt, PBKDF2_ITERATIONS)
    config_to_save = pd.DataFrame(
        [
            {"Key": "admin_salt", "Value": salt},
            {"Key": "admin_hash", "Value": digest},
            {"Key": "admin_iterations", "Value": str(PBKDF2_ITERATIONS)},
            # Kept after password rotations so the one-time bootstrap secret is not
            # accepted again if only the hash rows are accidentally removed.
            {"Key": "admin_initialized", "Value": "1"},
        ]
    )
    conn.update(
        spreadsheet=SPREADSHEET_URL,
        worksheet=CONFIG_SHEET,
        data=config_to_save,
    )


def admin_is_authenticated() -> bool:
    if not st.session_state.get("admin_authenticated", False):
        return False
    expires_at = float(st.session_state.get("admin_expires_at", 0))
    if time.time() > expires_at:
        st.session_state["admin_authenticated"] = False
        return False
    # Refresh the inactivity timeout on each app rerun while authenticated.
    st.session_state["admin_expires_at"] = time.time() + ADMIN_SESSION_SECONDS
    return True


def render_admin_login(config_df: pd.DataFrame):
    credentials = stored_admin_credentials(config_df)
    cfg_values = config_values(config_df)
    already_initialized = cfg_values.get("admin_initialized", "") == "1"
    authenticated = admin_is_authenticated()

    with st.expander("🔐 สำหรับสตาฟ / Admin", expanded=not authenticated):
        if not authenticated:
            if credentials:
                st.caption("เข้าสู่ระบบด้วยรหัส Admin ปัจจุบัน")
            elif already_initialized:
                st.error(
                    "ระบบเคยตั้งรหัส Admin แล้ว แต่ไม่พบข้อมูล hash ที่สมบูรณ์ "
                    "จึงปิดการใช้รหัสเริ่มต้นเพื่อป้องกันการข้ามระบบ กรุณาให้เจ้าของไฟล์ตรวจสอบแท็บ 'ตั้งค่า'"
                )
            else:
                st.warning(
                    "การตั้งค่าครั้งแรก: ใช้ ADMIN_BOOTSTRAP_PASSWORD จาก Streamlit Secrets "
                    "เพื่อเข้าสู่ระบบ แล้วตั้งรหัส Admin ใหม่ทันที"
                )

            entered_password = st.text_input(
                "รหัสผ่าน Admin",
                type="password",
                key="admin_login_password_input",
            )
            if st.button("เข้าสู่ระบบผู้ดูแล", key="admin_login_button"):
                if credentials:
                    valid = verify_admin_password(entered_password, credentials)
                elif already_initialized:
                    valid = False
                else:
                    valid = bool(BOOTSTRAP_ADMIN_PASSWORD) and hmac.compare_digest(
                        entered_password, BOOTSTRAP_ADMIN_PASSWORD
                    )

                if valid:
                    st.session_state["admin_authenticated"] = True
                    st.session_state["admin_expires_at"] = time.time() + ADMIN_SESSION_SECONDS
                    st.rerun()
                else:
                    st.error("รหัสผ่าน Admin ไม่ถูกต้อง")
        else:
            st.success("เข้าสู่ระบบผู้ดูแลแล้ว")
            if st.button("ออกจากระบบ Admin", key="admin_logout_button"):
                st.session_state["admin_authenticated"] = False
                st.session_state.pop("admin_expires_at", None)
                st.rerun()

            if not credentials:
                st.warning("กรุณาตั้งรหัส Admin ใหม่ก่อนเริ่มใช้งานระบบ")
                form_title = "ตั้งรหัส Admin ครั้งแรก"
                form_key = "initial_admin_password_form"
            else:
                with st.expander("เปลี่ยนรหัสผ่าน Admin"):
                    form_title = "เปลี่ยนรหัส Admin"
                    form_key = "change_admin_password_form"
                    _render_password_change_form(form_title, form_key)
                return

            _render_password_change_form(form_title, form_key)


def _render_password_change_form(title: str, form_key: str):
    st.markdown(f"**{title}**")
    with st.form(form_key):
        new_password = st.text_input(
            "รหัส Admin ใหม่ (อย่างน้อย 12 ตัวอักษร)",
            type="password",
            key=f"{form_key}_new",
        )
        confirm_password = st.text_input(
            "ยืนยันรหัส Admin ใหม่",
            type="password",
            key=f"{form_key}_confirm",
        )
        submitted = st.form_submit_button("บันทึกรหัส Admin ใหม่")

    if submitted:
        if len(new_password) < 12:
            st.error("รหัส Admin ใหม่ต้องมีอย่างน้อย 12 ตัวอักษร")
        elif new_password != confirm_password:
            st.error("รหัสผ่านทั้งสองช่องไม่ตรงกัน")
        else:
            try:
                save_admin_password(new_password)
                st.session_state["admin_authenticated"] = True
                st.session_state["admin_expires_at"] = time.time() + ADMIN_SESSION_SECONDS
                st.success("บันทึกรหัส Admin ใหม่แล้ว ไม่ต้องแก้โค้ดหรือ Streamlit Secrets ในครั้งถัดไป")
                st.rerun()
            except Exception:
                st.error("บันทึกรหัส Admin ไม่สำเร็จ กรุณาตรวจสอบสิทธิ์แก้ไขแท็บ 'ตั้งค่า'")


# ---------------------------------------------------------
# Prepare master and migrate legacy results without retaining DOB
# ---------------------------------------------------------
master_error = ""
try:
    df_master = prepare_master(df_raw)
except ValueError as exc:
    df_master = pd.DataFrame(columns=REQUIRED_MASTER_COLUMNS)
    master_error = str(exc)

has_legacy_results = (
    not df_result_raw.empty
    and "Monito" in df_result_raw.columns
    and "DOB" in df_result_raw.columns
)

if has_legacy_results and not master_error:
    # Older versions wrote DOB into the results tab. Validate rows against the master
    # list, then automatically rewrite the results tab without DOB while preserving pairings.
    try:
        legacy = df_result_raw.copy()
        for col in ["Name", "Dept", "Year"]:
            if col not in legacy.columns:
                raise ValueError("แท็บผลจับฉลากรุ่นเก่าไม่มีคอลัมน์ที่จำเป็น")
            legacy[col] = legacy[col].map(clean_cell)
        legacy["DOB"] = legacy["DOB"].map(normalize_dob)
        legacy["Monito"] = legacy["Monito"].map(clean_cell)
        legacy = legacy.loc[~legacy[["Name", "Dept", "Year", "DOB", "Monito"]].eq("").all(axis=1)].copy()

        master_keys = set(
            zip(df_master["Name"], df_master["Dept"], df_master["Year"], df_master["DOB"])
        )
        legacy_keys = list(zip(legacy["Name"], legacy["Dept"], legacy["Year"], legacy["DOB"]))
        if len(legacy) != len(df_master) or any(key not in master_keys for key in legacy_keys):
            raise ValueError("ข้อมูลผลจับฉลากรุ่นเก่าไม่ตรงกับแท็บรายชื่อ จึงไม่สามารถลบ DOB อัตโนมัติได้")

        migrated = legacy[RESULT_COLUMNS].copy()
        conn.update(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET, data=migrated)
        st.info("ระบบปรับแท็บผลจับฉลากเป็นรูปแบบที่ไม่เก็บ DOB แล้ว")
        st.rerun()
    except Exception:
        st.error(
            "พบผลจับฉลากจากโค้ดรุ่นเก่าที่มี DOB อยู่ในแท็บผลจับฉลาก "
            "แต่ระบบไม่สามารถปรับข้อมูลให้ปลอดภัยโดยอัตโนมัติได้ "
            "กรุณาสำรองไฟล์ไว้ก่อน แล้วให้สตาฟตรวจสอบรายชื่อกับผลจับฉลาก"
        )
        st.stop()

# Standardize result data. The result tab and offline export should never include DOB.
df_result = normalize_result(df_result_raw)
result_exists = not df_result.empty
result_is_valid = False
result_validation_message = ""
if result_exists and not master_error and not df_master.empty:
    result_is_valid, result_validation_message = validate_result_data(df_result, df_master)

# Admin gate appears before the role-specific sections so authenticated admins can act immediately.
render_admin_login(df_config)

st.divider()

# ---------------------------------------------------------
# Shared helpers for result rows and Excel export
# ---------------------------------------------------------
def display_label(row) -> str:
    return f"{row['Name']} ({row['Dept']} #{row['Year']})"


def order_results_by_cycle(result_df: pd.DataFrame) -> pd.DataFrame:
    """Order rows by following each person's Monito link around each closed circle."""
    ordered_source = result_df[RESULT_COLUMNS].copy()
    if ordered_source.empty:
        ordered_source.insert(0, "วง", pd.Series(dtype="int64"))
        ordered_source.insert(1, "ลำดับในวง", pd.Series(dtype="int64"))
        return ordered_source

    names = ordered_source["Name"].tolist()
    next_person = dict(zip(ordered_source["Name"], ordered_source["Monito"]))

    # A valid circle assignment must be a permutation: every participant appears
    # exactly once as a giver and exactly once as a recipient.
    if len(next_person) != len(names) or set(next_person.values()) != set(names):
        raise ValueError("ผลจับฉลากไม่ใช่วงปิดที่สมบูรณ์ จึงไม่สามารถเรียงลำดับ Excel ตามวงได้")

    visited = set()
    ordered_names = []
    circle_by_name = {}
    position_by_name = {}
    circle_number = 0

    # Start each new circle from the first not-yet-visited name in the sheet,
    # then follow Monito -> next row until the circle returns to its start.
    for start in names:
        if start in visited:
            continue
        circle_number += 1
        current = start
        circle_names = []

        while current not in circle_names:
            if current in visited:
                raise ValueError("พบวงจับฉลากเชื่อมกันผิดรูปแบบ จึงไม่สามารถเรียง Excel ได้")
            circle_names.append(current)
            current = next_person[current]

        if current != start:
            raise ValueError("วงจับฉลากไม่ย้อนกลับมายังจุดเริ่มต้น จึงไม่สามารถเรียง Excel ได้")

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
    """Export an easy-to-follow circle order, without DOB."""
    buffer = io.BytesIO()
    export_df = order_results_by_cycle(result_df)

    # Keep exact names in the database, but include the target's Dept/Year in
    # the offline file so staff can follow each giver -> recipient -> next giver.
    person_labels = {
        row["Name"]: display_label(row)
        for _, row in df_master.iterrows()
    }
    export_df["Monito"] = export_df["Monito"].map(person_labels).fillna(export_df["Monito"])

    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False, sheet_name="ผลจับฉลาก")
        worksheet = writer.sheets["ผลจับฉลาก"]
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        worksheet.column_dimensions["A"].width = 9   # วง
        worksheet.column_dimensions["B"].width = 12  # ลำดับในวง
        worksheet.column_dimensions["C"].width = 24  # Name
        worksheet.column_dimensions["D"].width = 22  # Dept
        worksheet.column_dimensions["E"].width = 12  # Year
        worksheet.column_dimensions["F"].width = 40  # Monito (incl. dept/year)
        for cell in worksheet[1]:
            header_font = copy(cell.font)
            header_font.bold = True
            cell.font = header_font
    return buffer.getvalue()


def run_draw(num_circles: int):
    # A fresh read acts as a final guard against an accidental second draw from a stale page.
    current_result = conn.read(
        spreadsheet=SPREADSHEET_URL,
        worksheet=RESULT_SHEET,
        ttl=0,
    )
    current_clean = normalize_result(current_result)
    if not current_clean.empty:
        st.error("ตรวจพบว่ามีผลจับฉลากแล้ว ระบบไม่สุ่มทับผลเดิม กรุณารีเฟรชหน้าเว็บ")
        st.rerun()

    participant_names = df_master["Name"].tolist()
    if len(participant_names) < 3:
        st.error("ต้องมีผู้เข้าร่วมอย่างน้อย 3 คน จึงจะจับแบบวงปิดที่ไม่มีการจับสลับกันเองได้")
        return

    shuffled_names = participant_names.copy()
    random.SystemRandom().shuffle(shuffled_names)

    groups = [[] for _ in range(num_circles)]
    for idx, name in enumerate(shuffled_names):
        groups[idx % num_circles].append(name)

    too_small = any(len(group) < 3 for group in groups)
    if too_small:
        st.error("แต่ละวงต้องมีอย่างน้อย 3 คน เพื่อป้องกันการจับสลับกันเอง")
        return

    pairs = {}
    for group in groups:
        for idx, name in enumerate(group):
            pairs[name] = group[(idx + 1) % len(group)]

    # Defensive checks before writing anything.
    if len(pairs) != len(participant_names):
        st.error("ระบบตรวจสอบพบว่าจำนวนคู่ไม่ครบ จึงยกเลิกการบันทึก")
        return
    if any(name == target for name, target in pairs.items()):
        st.error("ระบบตรวจสอบพบการจับได้ตัวเอง จึงยกเลิกการบันทึก")
        return
    if any(pairs.get(target) == giver for giver, target in pairs.items()):
        st.error("ระบบตรวจสอบพบการจับสลับกันเอง จึงยกเลิกการบันทึก")
        return

    result_to_save = df_master[["Name", "Dept", "Year"]].copy()
    result_to_save["Monito"] = result_to_save["Name"].map(pairs)
    result_to_save = result_to_save[RESULT_COLUMNS]

    try:
        conn.update(
            spreadsheet=SPREADSHEET_URL,
            worksheet=RESULT_SHEET,
            data=result_to_save,
        )
        check_back = conn.read(
            spreadsheet=SPREADSHEET_URL,
            worksheet=RESULT_SHEET,
            ttl=0,
        )
        check_back = normalize_result(check_back)
        is_valid, message = validate_result_data(check_back, df_master)
        if not is_valid:
            st.error(
                "ระบบบันทึกผลแล้ว แต่การตรวจสอบหลังบันทึกไม่ผ่าน: " + message
                + " กรุณาอย่าให้ผู้เข้าร่วมดูผลจนกว่าสตาฟจะตรวจสอบเสร็จ"
            )
            st.stop()
        st.success("สุ่มและตรวจสอบผลครบทุกคนแล้ว ระบบบันทึกผลลง Google Sheets สำเร็จ")
        st.rerun()
    except Exception:
        st.error("บันทึกผลจับฉลากไม่สำเร็จ กรุณาตรวจสอบการเชื่อมต่อและสิทธิ์ Service Account")


# ---------------------------------------------------------
# Participant view
# ---------------------------------------------------------
if result_exists:
    if master_error:
        st.error("ไม่สามารถตรวจสอบตัวตนได้: " + master_error)
        st.info("กรุณาติดต่อสตาฟให้ตรวจสอบแท็บรายชื่อ โดยยังไม่ควรเปิดดูผลจนกว่าข้อมูลจะถูกต้อง")
    elif not result_is_valid:
        st.error(result_validation_message or "ผลจับฉลากยังไม่ผ่านการตรวจสอบ")
        st.info("กรุณาติดต่อสตาฟเพื่อแก้ไขข้อมูลก่อนเปิดให้ผู้เข้าร่วมดูผล")
    else:
        st.markdown(
            f"""
            <div class="status-box">
                <h4 style="margin:0;">✅ ระบบเปิดให้ดูผลแล้ว</h4>
                <p style="margin:5px 0 0 0;">จำนวนผู้เข้าร่วมทั้งหมด: {len(df_result)} คน</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        result_lookup = df_result.set_index("Name").to_dict(orient="index")
        dob_lookup = df_master.set_index("Name")["DOB"].to_dict()
        name_to_label = {
            row["Name"]: display_label(row)
            for _, row in df_result.iterrows()
        }
        selected_name = st.selectbox(
            "👤 1. เลือกชื่อของคุณ",
            options=[""] + df_result["Name"].tolist(),
            format_func=lambda value: "-- กรุณาเลือกชื่อของคุณ --" if value == "" else name_to_label[value],
            key="participant_name_select",
        )

        if st.session_state.get("last_selected_participant") != selected_name:
            st.session_state.pop("verified_participant_name", None)
            st.session_state.pop("verified_participant_until", None)
            st.session_state["last_selected_participant"] = selected_name

        if selected_name:
            attempts_key = "dob_attempts_" + hashlib.sha256(selected_name.encode("utf-8")).hexdigest()[:16]
            attempts = int(st.session_state.get(attempts_key, 0))

            if attempts >= MAX_DOB_ATTEMPTS_PER_SESSION:
                st.error("กรอกรหัสผิดหลายครั้งเกินกำหนดในเซสชันนี้ กรุณาติดต่อสตาฟ")
            else:
                with st.form("participant_result_form"):
                    entered_dob = st.text_input(
                        "🔑 2. รหัสผ่าน (วันเกิด DDMMYYYY)",
                        type="password",
                        placeholder="เช่น 05052003",
                        key="participant_dob_input",
                    )
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
                        st.error("รหัสวันเกิดไม่ถูกต้อง กรุณาลองใหม่")

            verified_name = st.session_state.get("verified_participant_name")
            verified_until = float(st.session_state.get("verified_participant_until", 0))
            if verified_name == selected_name and time.time() <= verified_until:
                st.success("🎉 Monito ของคุณคือ:")
                monito_name = result_lookup[selected_name]["Monito"]
                # Show the recipient's name, department, and year in the UI.
                st.subheader(name_to_label.get(monito_name, monito_name))
                st.caption("เมื่อจำผลได้แล้ว กดซ่อนผลก่อนส่งเครื่องให้คนถัดไป")

                def hide_participant_result():
                    """Clear this browser session's result before the next person uses it."""
                    st.session_state.pop("verified_participant_name", None)
                    st.session_state.pop("verified_participant_until", None)
                    st.session_state["participant_name_select"] = ""
                    st.session_state["last_selected_participant"] = ""
                    st.session_state["participant_dob_input"] = ""

                st.button(
                    "🙈 ซ่อนผลและให้คนถัดไปใช้เครื่องนี้",
                    key="hide_participant_result_button",
                    on_click=hide_participant_result,
                    use_container_width=True,
                )

    # -----------------------------------------------------
    # Admin tools when results exist
    # -----------------------------------------------------
    if admin_is_authenticated():
        st.divider()
        st.subheader("🛠️ เครื่องมือ Admin")

        if result_is_valid:
            with st.expander("🔎 ค้นหาผลให้ผู้ที่ลืม Monito"):
                search_name = st.text_input("พิมพ์ชื่อผู้เข้าร่วม", key="admin_search_name")
                if search_name.strip():
                    found = df_result[
                        df_result["Name"].str.casefold().str.contains(
                            search_name.strip().casefold(), regex=False
                        )
                    ][RESULT_COLUMNS].copy()
                    if found.empty:
                        st.info("ไม่พบชื่อที่ค้นหา")
                    else:
                        # For Admin's display, show the recipient's Dept and Year too.
                        found["Monito"] = found["Monito"].map(name_to_label).fillna(found["Monito"])
                        st.dataframe(found, use_container_width=True, hide_index=True)

            try:
                offline_file = create_offline_excel(df_result)
                st.download_button(
                    "⬇️ ดาวน์โหลดไฟล์ Excel สำรองสำหรับออฟไลน์",
                    data=offline_file,
                    file_name="Monito_AnurakSciCU_Offline.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="download_offline_results",
                )
                st.caption("ไฟล์นี้เรียงผลต่อเนื่องตามแต่ละวง มีเลขวง/ลำดับในวง และไม่มี DOB")
            except Exception:
                st.warning("สร้างไฟล์ Excel ไม่สำเร็จ กรุณาตรวจสอบข้อมูลผลจับฉลากและการติดตั้ง openpyxl")

        st.warning("ล้างผลเฉพาะแท็บ 'ผลจับฉลาก' เท่านั้น แท็บรายชื่อและการตั้งค่ารหัส Admin จะไม่ถูกลบ")
        confirm_reset = st.checkbox("ฉันสำรองผลจับฉลากแล้วและยืนยันให้ล้างผล", key="confirm_reset_results")
        if st.button("⚠️ ล้างผลจับฉลากเพื่อเริ่มค่ายใหม่", disabled=not confirm_reset, key="reset_results_button"):
            try:
                conn.clear(spreadsheet=SPREADSHEET_URL, worksheet=RESULT_SHEET)
                st.session_state.pop("verified_participant_name", None)
                st.session_state.pop("verified_participant_until", None)
                st.success("ล้างผลจับฉลากแล้ว")
                st.rerun()
            except Exception:
                st.error("ล้างผลจับฉลากไม่สำเร็จ กรุณาตรวจสอบสิทธิ์ Service Account")

# ---------------------------------------------------------
# Admin preparation / draw mode
# ---------------------------------------------------------
else:
    st.info("ยังไม่มีผลจับฉลาก ระบบจึงอยู่ในโหมดเตรียมการสำหรับสตาฟ")
    if master_error:
        st.error(master_error)
    elif df_master.empty:
        st.error("ไม่พบรายชื่อผู้เข้าร่วมในแท็บ 'รายชื่อ' กรุณากรอกข้อมูลให้ครบก่อนเริ่มสุ่ม")
    else:
        st.write(f"ตรวจพบรายชื่อผู้เข้าร่วม {len(df_master)} คน")
        if not admin_is_authenticated():
            st.warning("ต้องเข้าสู่ระบบ Admin ก่อน จึงจะเริ่มสุ่มหรือเขียนผลลง Google Sheets ได้")
        elif len(df_master) < 3:
            st.error("ต้องมีผู้เข้าร่วมอย่างน้อย 3 คน เพื่อป้องกันการจับสลับกันเอง")
        else:
            max_circles = max(1, len(df_master) // 3)
            num_circles = st.slider(
                "🔄 ต้องการแบ่งการสุ่มเป็นกี่วง?",
                min_value=1,
                max_value=max_circles,
                value=1,
                help="แต่ละวงมีอย่างน้อย 3 คน และปิดวงภายในกลุ่มตัวเอง",
            )
            st.caption("ระบบจะล็อกผลลง Google Sheets เมื่อสุ่มสำเร็จ และไม่สุ่มทับผลที่มีอยู่แล้ว")
            if st.button("🎲 เริ่มสุ่มจับคู่และล็อกผล", use_container_width=True, key="draw_results_button"):
                with st.spinner("กำลังสุ่ม บันทึก และตรวจสอบผลจับฉลาก..."):
                    run_draw(num_circles)

st.divider()
st.caption(
    "หมายเหตุ: การดูผลผ่านเว็บไซต์ต้องใช้อินเทอร์เน็ต หากอยู่ในพื้นที่ไม่มีสัญญาณ "
    "ให้สตาฟดาวน์โหลดไฟล์ Excel สำรองไว้ล่วงหน้า"
)
