import streamlit as st
import pandas as pd
import plotly.express as px
from pathlib import Path
import tempfile
import io
import json

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    GOOGLE_DRIVE_AVAILABLE = True
except Exception:
    GOOGLE_DRIVE_AVAILABLE = False

st.set_page_config(page_title="Venky Life Dashboard", layout="wide")

LOCAL_DATA_FOLDER = Path(r"C:\LifeDashboard\data")
CLOUD_DATA_FOLDER = Path(tempfile.gettempdir()) / "life_dashboard_google_drive_data"
DATA_FOLDER = LOCAL_DATA_FOLDER
DATA_SOURCE_LABEL = "Local folder"
DATA_SOURCE_DETAIL = str(LOCAL_DATA_FOLDER)
GOOGLE_DRIVE_STATUS = "Not connected"

GOOGLE_DRIVE_ALLOWED_EXTENSIONS = [".csv", ".xlsx"]
GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
GOOGLE_DRIVE_DEBUG_MESSAGES = []


def get_secret_section(section_name):
    try:
        return st.secrets[section_name]
    except Exception:
        return None


def get_secret_value(section_name, key, default=None):
    section = get_secret_section(section_name)
    if section is None:
        return default

    try:
        return section.get(key, default)
    except Exception:
        try:
            return section[key]
        except Exception:
            return default


def get_google_drive_credentials_path():
    """Return the local Google service account JSON path.

    Keep this file outside git/shareable code. For your local setup, place it here:
    C:\\LifeDashboard\\credentials.json
    """
    return Path(r"C:\LifeDashboard\credentials.json")


def get_google_drive_credentials():
    """Get Google Drive credentials from Streamlit Cloud secrets first, then local credentials.json."""

    # Option 1: Streamlit Cloud secrets using full JSON.
    # Recommended Secrets format:
    # [GOOGLE_SERVICE_ACCOUNT_JSON]
    # json = '''{...full Google JSON...}'''
    try:
        if "GOOGLE_SERVICE_ACCOUNT_JSON" in st.secrets:
            raw_secret = st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"]

            # Streamlit returns section secrets as an AttrDict, not always a normal dict.
            if hasattr(raw_secret, "get"):
                raw_secret = raw_secret.get("json", "")

            service_account_info = json.loads(str(raw_secret))

            if "private_key" in service_account_info:
                service_account_info["private_key"] = (
                    str(service_account_info["private_key"])
                    .replace("\\n", "\n")
                )

            return service_account.Credentials.from_service_account_info(
                service_account_info,
                scopes=GOOGLE_DRIVE_SCOPES
            )
    except Exception as e:
        GOOGLE_DRIVE_DEBUG_MESSAGES.append(
            f"Could not read GOOGLE_SERVICE_ACCOUNT_JSON secret: {e}"
        )

    # Option 2: Streamlit Cloud secrets using [gdrive_service_account].
    try:
        if "gdrive_service_account" in st.secrets:
            service_account_info = dict(st.secrets["gdrive_service_account"])

            if "private_key" in service_account_info:
                service_account_info["private_key"] = (
                    str(service_account_info["private_key"])
                    .replace("\\n", "\n")
                )

            return service_account.Credentials.from_service_account_info(
                service_account_info,
                scopes=GOOGLE_DRIVE_SCOPES
            )
    except Exception as e:
        GOOGLE_DRIVE_DEBUG_MESSAGES.append(
            f"Could not read gdrive_service_account secret: {e}"
        )

    # Option 3: Local machine fallback.
    credentials_path = get_google_drive_credentials_path()

    if credentials_path.exists():
        return service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=GOOGLE_DRIVE_SCOPES
        )

    GOOGLE_DRIVE_DEBUG_MESSAGES.append(
        "No Google Drive credentials found in Streamlit Secrets or local credentials.json."
    )
    return None


def has_google_drive_credentials():
    try:
        if "gdrive_service_account" in st.secrets:
            return True
    except Exception:
        pass

    try:
        if "GOOGLE_SERVICE_ACCOUNT_JSON" in st.secrets:
            return True
    except Exception:
        pass

    return get_google_drive_credentials_path().exists()


def should_use_google_drive():
    enabled = get_secret_value("google_drive", "enabled", False)
    folder_id = get_secret_value("google_drive", "folder_id", "")

    return bool(
        enabled
        and folder_id
        and has_google_drive_credentials()
        and GOOGLE_DRIVE_AVAILABLE
    )


@st.cache_data(ttl=600, show_spinner=False)
def sync_google_drive_folder(folder_id):
    CLOUD_DATA_FOLDER.mkdir(parents=True, exist_ok=True)

    for old_file in CLOUD_DATA_FOLDER.glob("*"):
        if old_file.is_file():
            old_file.unlink()

    credentials = get_google_drive_credentials()

    if credentials is None:
        details = " | ".join(GOOGLE_DRIVE_DEBUG_MESSAGES)
        raise FileNotFoundError(
            f"Google Drive credentials could not be created. {details}"
        )

    service = build("drive", "v3", credentials=credentials)

    query = f"'{folder_id}' in parents and trashed=false"

    files = []
    page_token = None

    while True:
        response = service.files().list(
            q=query,
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
            pageToken=page_token,
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()

        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")

        if not page_token:
            break

    downloaded_files = []

    for file in files:
        file_id = file.get("id")
        file_name = file.get("name", "")
        mime_type = file.get("mimeType", "")

        if not file_name or file_name.startswith("~$"):
            continue

        output_name = file_name

        if mime_type == "application/vnd.google-apps.spreadsheet":
            request = service.files().export_media(
                fileId=file_id,
                mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

            if not output_name.lower().endswith(".xlsx"):
                output_name = f"{output_name}.xlsx"

        elif Path(file_name).suffix.lower() in GOOGLE_DRIVE_ALLOWED_EXTENSIONS:
            request = service.files().get_media(fileId=file_id)

        else:
            continue

        destination = CLOUD_DATA_FOLDER / output_name

        file_handle = io.FileIO(destination, "wb")
        downloader = MediaIoBaseDownload(file_handle, request)

        done = False
        while not done:
            status, done = downloader.next_chunk()

        file_handle.close()
        downloaded_files.append(output_name)

    return downloaded_files


def configure_data_source():
    global DATA_FOLDER, DATA_SOURCE_LABEL, DATA_SOURCE_DETAIL, GOOGLE_DRIVE_STATUS

    if not GOOGLE_DRIVE_AVAILABLE:
        GOOGLE_DRIVE_STATUS = "Google Drive libraries not installed"
        return

    if not should_use_google_drive():
        enabled = get_secret_value("google_drive", "enabled", False)
        folder_id = get_secret_value("google_drive", "folder_id", "")

        if not enabled:
            GOOGLE_DRIVE_STATUS = "Using local folder · Google Drive disabled in secrets"
        elif not folder_id:
            GOOGLE_DRIVE_STATUS = "Using local folder · Missing Google Drive folder_id"
        elif not has_google_drive_credentials():
            GOOGLE_DRIVE_STATUS = "Using local folder · Missing Google Drive credentials in Streamlit Secrets or local credentials.json"
        else:
            GOOGLE_DRIVE_STATUS = "Using local folder"
        return

    folder_id = get_secret_value("google_drive", "folder_id", "")

    try:
        downloaded_files = sync_google_drive_folder(folder_id)
        DATA_FOLDER = CLOUD_DATA_FOLDER
        DATA_SOURCE_LABEL = "Google Drive"
        DATA_SOURCE_DETAIL = f"{len(downloaded_files)} files synced"
        GOOGLE_DRIVE_STATUS = f"Connected · {len(downloaded_files)} files synced"

    except Exception as e:
        DATA_FOLDER = LOCAL_DATA_FOLDER
        DATA_SOURCE_LABEL = "Local folder fallback"
        DATA_SOURCE_DETAIL = str(LOCAL_DATA_FOLDER)
        GOOGLE_DRIVE_STATUS = f"Google Drive sync failed: {e}"


configure_data_source()


# -----------------------------
# UI Styling
# -----------------------------
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');

    :root {
        --bg: #f4f6fb;
        --dark: #0f172a;
        --dark2: #111827;
        --muted: #64748b;
        --orange: #f97316;
        --orange2: #fb923c;
        --green: #10b981;
        --blue: #38bdf8;
        --purple: #a855f7;
        --card: rgba(255,255,255,0.88);
        --border: rgba(226,232,240,0.95);
        --shadow: 0 18px 45px rgba(15,23,42,0.08);
    }

    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }

    .stApp {
        background:
            radial-gradient(circle at top left, rgba(249,115,22,0.16), transparent 28%),
            radial-gradient(circle at top right, rgba(56,189,248,0.14), transparent 30%),
            linear-gradient(180deg, #f8fafc 0%, #eef2f7 100%);
    }

    .block-container {
        padding-top: 1.2rem;
        padding-bottom: 2.5rem;
        max-width: 1550px;
    }

    .app-header {
        position: relative;
        overflow: hidden;
        background:
            linear-gradient(135deg, rgba(17,24,39,1) 0%, rgba(30,41,59,1) 54%, rgba(124,45,18,1) 100%);
        padding: 34px 36px;
        border-radius: 30px;
        margin-bottom: 22px;
        color: white;
        box-shadow: 0 25px 70px rgba(15,23,42,0.28);
        border: 1px solid rgba(255,255,255,0.10);
    }

    .app-header::before {
        content: "";
        position: absolute;
        inset: -80px -80px auto auto;
        height: 220px;
        width: 220px;
        background: radial-gradient(circle, rgba(249,115,22,0.55), transparent 66%);
        border-radius: 999px;
    }

    .app-header::after {
        content: "";
        position: absolute;
        left: -70px;
        bottom: -110px;
        height: 220px;
        width: 220px;
        background: radial-gradient(circle, rgba(56,189,248,0.25), transparent 67%);
        border-radius: 999px;
    }

    .hero-eyebrow {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        background: rgba(255,255,255,0.12);
        border: 1px solid rgba(255,255,255,0.18);
        color: #fed7aa;
        padding: 8px 12px;
        border-radius: 999px;
        font-size: 13px;
        font-weight: 800;
        letter-spacing: 0.2px;
        margin-bottom: 14px;
        position: relative;
        z-index: 2;
    }

    .app-header h1 {
        color: white;
        font-size: 42px;
        line-height: 1.05;
        margin: 0 0 10px 0;
        font-weight: 950;
        letter-spacing: -1.2px;
        position: relative;
        z-index: 2;
    }

    .app-header p {
        color: #dbeafe;
        font-size: 16px;
        max-width: 780px;
        margin: 0;
        position: relative;
        z-index: 2;
    }

    .hero-strip {
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        margin-top: 20px;
        position: relative;
        z-index: 2;
    }

    .hero-chip {
        background: rgba(255,255,255,0.10);
        color: #ffffff;
        border: 1px solid rgba(255,255,255,0.18);
        padding: 9px 13px;
        border-radius: 999px;
        font-size: 13px;
        font-weight: 750;
        backdrop-filter: blur(10px);
    }

    .section-card {
        background: linear-gradient(135deg, rgba(255,255,255,0.92), rgba(255,247,237,0.82));
        padding: 22px 24px;
        border-radius: 24px;
        border: 1px solid var(--border);
        box-shadow: var(--shadow);
        margin-bottom: 18px;
    }

    .section-title {
        font-size: 26px;
        font-weight: 900;
        color: #0f172a;
        margin-bottom: 5px;
        letter-spacing: -0.4px;
    }

    .section-subtitle {
        font-size: 14px;
        color: #64748b;
        margin-bottom: 0px;
        font-weight: 550;
    }

    [data-testid="stMetric"] {
        background: linear-gradient(145deg, rgba(255,255,255,0.95), rgba(248,250,252,0.9));
        padding: 20px 18px;
        border-radius: 24px;
        border: 1px solid rgba(226,232,240,0.95);
        box-shadow: 0 16px 38px rgba(15,23,42,0.08);
        transition: all 0.20s ease;
        position: relative;
        overflow: hidden;
    }

    [data-testid="stMetric"]::before {
        content: "";
        position: absolute;
        top: 0;
        left: 0;
        width: 100%;
        height: 4px;
        background: linear-gradient(90deg, #f97316, #fb923c, #38bdf8);
    }

    [data-testid="stMetric"]:hover {
        transform: translateY(-3px);
        box-shadow: 0 22px 50px rgba(15,23,42,0.12);
    }

    [data-testid="stMetric"] label {
        color: #475569 !important;
        font-weight: 850 !important;
        font-size: 13px !important;
    }

    [data-testid="stMetricValue"] {
        color: #0f172a !important;
        font-weight: 950 !important;
        letter-spacing: -0.8px;
    }

    [data-testid="stMetricDelta"] {
        color: #0f172a !important;
        font-weight: 800 !important;
    }

    .stTabs [data-baseweb="tab-list"] {
        gap: 9px;
        background-color: transparent;
        border-bottom: 0;
        flex-wrap: wrap;
    }

    .stTabs [data-baseweb="tab"] {
        background: rgba(255,255,255,0.86);
        border-radius: 999px;
        padding: 11px 18px;
        border: 1px solid rgba(226,232,240,1);
        color: #334155;
        font-weight: 850;
        box-shadow: 0 8px 22px rgba(15,23,42,0.05);
    }

    .stTabs [aria-selected="true"] {
        background: linear-gradient(135deg, #111827, #f97316) !important;
        color: white !important;
        border-color: rgba(249,115,22,0.6) !important;
    }

    div[data-testid="stDataFrame"] {
        border-radius: 20px;
        overflow: hidden;
        border: 1px solid #e2e8f0;
        box-shadow: 0 14px 34px rgba(15,23,42,0.06);
    }

    [data-testid="stPlotlyChart"] {
        background: #111827;
        border-radius: 24px;
        padding: 8px;
        box-shadow: 0 20px 52px rgba(15,23,42,0.18);
        border: 1px solid rgba(255,255,255,0.06);
    }

    .insight-card {
        background: linear-gradient(135deg, rgba(255,255,255,0.95), rgba(248,250,252,0.88));
        padding: 19px 21px;
        border-radius: 22px;
        border: 1px solid #e2e8f0;
        box-shadow: 0 14px 34px rgba(15,23,42,0.06);
        margin-bottom: 12px;
        position: relative;
        overflow: hidden;
    }

    .insight-card::before {
        content: "";
        position: absolute;
        left: 0;
        top: 0;
        bottom: 0;
        width: 5px;
        background: linear-gradient(180deg, #f97316, #38bdf8);
    }

    .insight-card h4 {
        margin: 0 0 7px 0;
        color: #0f172a;
        font-weight: 900;
    }

    .insight-card p {
        margin: 0;
        color: #475569;
        font-weight: 550;
    }

    .success-pill {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        background: linear-gradient(135deg, #ecfdf5, #f0fdfa);
        color: #047857;
        padding: 10px 14px;
        border-radius: 999px;
        font-weight: 850;
        font-size: 13px;
        margin-bottom: 14px;
        border: 1px solid #bbf7d0;
        box-shadow: 0 12px 28px rgba(16,185,129,0.10);
    }

    .fancy-card-grid {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 14px;
        margin-bottom: 18px;
    }

    .fancy-card {
        background: rgba(255,255,255,0.88);
        border: 1px solid #e2e8f0;
        border-radius: 22px;
        padding: 18px;
        box-shadow: 0 14px 34px rgba(15,23,42,0.06);
    }

    .fancy-card .icon {
        font-size: 26px;
        margin-bottom: 8px;
    }

    .fancy-card .label {
        color: #64748b;
        font-size: 12px;
        font-weight: 850;
        text-transform: uppercase;
        letter-spacing: 0.7px;
    }

    .fancy-card .value {
        color: #0f172a;
        font-size: 24px;
        font-weight: 950;
        margin-top: 4px;
        letter-spacing: -0.7px;
    }

    .stButton > button, .stDownloadButton > button {
        border-radius: 999px !important;
        border: 1px solid rgba(249,115,22,0.45) !important;
        background: linear-gradient(135deg, #111827, #f97316) !important;
        color: white !important;
        font-weight: 900 !important;
        padding: 0.65rem 1rem !important;
        box-shadow: 0 14px 30px rgba(249,115,22,0.22) !important;
    }

    div[data-baseweb="select"] > div,
    div[data-baseweb="input"] > div,
    textarea {
        border-radius: 16px !important;
        border-color: #e2e8f0 !important;
        box-shadow: 0 8px 22px rgba(15,23,42,0.04) !important;
    }

    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0f172a 0%, #111827 55%, #1f2937 100%);
    }

    section[data-testid="stSidebar"] * {
        color: #e5e7eb !important;
    }

    section[data-testid="stSidebar"] [data-testid="stRadio"] label {
        color: #f8fafc !important;
        font-weight: 850 !important;
    }

    section[data-testid="stSidebar"] div[role="radiogroup"] label {
        background: rgba(255,255,255,0.08);
        border: 1px solid rgba(255,255,255,0.10);
        border-radius: 16px;
        padding: 8px 10px;
        margin-bottom: 7px;
    }

    section[data-testid="stSidebar"] code {
        color: #fed7aa !important;
        background: rgba(255,255,255,0.08) !important;
        border-radius: 12px !important;
    }



    /* Strong readable headings outside the hero */
    [data-testid="stMarkdownContainer"] h1,
    [data-testid="stMarkdownContainer"] h2,
    [data-testid="stMarkdownContainer"] h3,
    [data-testid="stMarkdownContainer"] h4,
    [data-testid="stMarkdownContainer"] h5,
    [data-testid="stMarkdownContainer"] h6 {
        color: #1f2937 !important;
        font-weight: 950 !important;
        letter-spacing: -0.3px;
    }

    .app-header h1,
    .app-header p,
    .app-header .hero-eyebrow,
    .app-header .hero-chip {
        color: white !important;
    }

    .app-header .hero-eyebrow {
        color: #fed7aa !important;
    }

    @media (max-width: 900px) {
        .fancy-card-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .app-header h1 {
            font-size: 34px;
        }
    }
</style>
""", unsafe_allow_html=True)


# -----------------------------
# Header
# -----------------------------
st.markdown("""
<div class="app-header">
    <div class="hero-eyebrow">✨ Personal Life OS · Local Analytics</div>
    <h1>🚀 Venky Life Dashboard</h1>
    <p>Your premium command center for health, expenses, workouts, investments, planning, and personal performance.</p>
    <div class="hero-strip">
        <span class="hero-chip">🛌 Sleep</span>
        <span class="hero-chip">❤️ Recovery</span>
        <span class="hero-chip">🏃 Workouts</span>
        <span class="hero-chip">💳 Expenses</span>
        <span class="hero-chip">📈 Investments</span>
        <span class="hero-chip">🎯 Goals</span>
    </div>
</div>
""", unsafe_allow_html=True)


# -----------------------------
# File loading
# -----------------------------
def load_all_csv_files(keyword):
    files = sorted(DATA_FOLDER.glob(f"{keyword}_*.csv"))

    if not files:
        return None, []

    combined = []

    for file in files:
        df = pd.read_csv(file)
        df["source_file"] = file.name
        combined.append(df)

    return pd.concat(combined, ignore_index=True), files


def load_all_expense_files():
    baseline_files = sorted(DATA_FOLDER.glob("expenses_last12_*.xlsx"))

    monthly_csv_files = sorted(DATA_FOLDER.glob("expenses_*.csv"))
    monthly_excel_files = sorted(DATA_FOLDER.glob("expenses_*.xlsx"))

    monthly_files = [
        file for file in monthly_csv_files + monthly_excel_files
        if "last12" not in file.name.lower()
    ]

    files = baseline_files + monthly_files

    if not files:
        return None, []

    combined = []

    for file in files:
        if "last12" in file.name.lower():
            df = pd.read_excel(file)
            df["expense_file_type"] = "Baseline"
            df["expense_period_type"] = "Last 12 Months"
            df["expense_period"] = "Last 12 Months to Apr 2026"
            df["month"] = "Last 12 Months"
        else:
            if file.suffix.lower() == ".csv":
                df = pd.read_csv(file)
            else:
                df = pd.read_excel(file, sheet_name="All Transactions", header=1)

            month_from_filename = file.stem.replace("expenses_", "").replace("_", "-")

            df["expense_file_type"] = "Transactions"
            df["expense_period_type"] = "Monthly"
            df["expense_period"] = month_from_filename
            df["month"] = month_from_filename

        df["source_file"] = file.name
        combined.append(df)

    return pd.concat(combined, ignore_index=True), files


def load_ringconn_data():
    sleep, sleep_files = load_all_csv_files("sleep")
    activity, activity_files = load_all_csv_files("activity")
    vitals, vitals_files = load_all_csv_files("vitals")

    return sleep, activity, vitals, sleep_files, activity_files, vitals_files

def load_all_workout_files():
    workout_files = sorted(DATA_FOLDER.glob("workouts_*.csv"))

    if not workout_files:
        return None, []

    combined = []

    for file in workout_files:
        try:
            df = pd.read_csv(file)
            df["source_file"] = file.name
            combined.append(df)
        except Exception as e:
            st.warning(f"Could not read workout file {file.name}: {e}")

    if not combined:
        return None, workout_files

    return pd.concat(combined, ignore_index=True), workout_files




def load_all_investment_files():
    investment_files = sorted(DATA_FOLDER.glob("*investment*.xlsx"))
    investment_files += sorted(DATA_FOLDER.glob("*Investment*.xlsx"))

    investment_files = sorted({file for file in investment_files if not file.name.startswith("~$")})

    if not investment_files:
        return None, []

    combined = []

    for file in investment_files:
        try:
            raw = pd.read_excel(file, header=None)
            parsed = parse_investment_workbook(raw, file.name)

            if parsed is not None and not parsed.empty:
                combined.append(parsed)

        except PermissionError:
            st.warning(f"Skipped locked Excel temp file: {file.name}")
            continue

        except Exception as e:
            st.warning(f"Could not read investment file {file.name}: {e}")
            continue

    if not combined:
        return None, investment_files

    investments = pd.concat(combined, ignore_index=True)

    required_cols = {
        "investment_name": "Unknown",
        "investment_type": "Other",
        "period": None,
        "period_label": "",
        "is_total_row": False,
        "starting_balance": 0,
        "gain_or_interest": 0,
        "monthly_deposit": 0,
        "ending_balance": 0,
        "section_order": 0,
        "source_file": ""
    }

    for col, default_value in required_cols.items():
        if col not in investments.columns:
            investments[col] = default_value

    investments["period_sort"] = pd.to_numeric(investments["period"], errors="coerce")

    return investments, investment_files


def parse_investment_workbook(raw, source_file):
    rows = []
    current_title = None
    headers = None
    section_order = 0

    for idx, row in raw.iterrows():
        first_cell = row.iloc[0]

        if isinstance(first_cell, str) and first_cell.strip().lower().startswith("investment"):
            current_title = first_cell.strip()
            headers = None
            section_order += 1
            continue

        if current_title is not None and headers is None:
            non_empty = row.dropna().tolist()
            if len(non_empty) >= 2:
                headers = [str(x).strip() if not pd.isna(x) else "" for x in row.tolist()]
            continue

        if current_title is not None and headers is not None:
            if row.isna().all():
                current_title = None
                headers = None
                continue

            period_value = row.iloc[0]

            if pd.isna(period_value):
                continue

            period_text = str(period_value).strip()
            is_total_row = period_text.lower() == "total"

            investment_name = current_title
            if ":" in investment_name:
                investment_name = investment_name.split(":", 1)[1].strip()

            investment_type = "Other"
            title_lower = current_title.lower()
            if "bank" in title_lower or "savings" in title_lower:
                investment_type = "Bank Savings"
            elif "real estate" in title_lower:
                investment_type = "Real Estate"
            elif "stock" in title_lower:
                investment_type = "Stocks"

            starting_balance = row.iloc[1] if len(row) > 1 else None
            gain_or_interest = row.iloc[2] if len(row) > 2 else None

            if investment_type == "Real Estate":
                monthly_deposit = 0
                ending_balance = row.iloc[3] if len(row) > 3 else None
            else:
                monthly_deposit = row.iloc[3] if len(row) > 3 else None
                ending_balance = row.iloc[4] if len(row) > 4 else None

            rows.append({
                "investment_name": investment_name,
                "investment_type": investment_type,
                "period": period_value,
                "period_label": period_text,
                "is_total_row": is_total_row,
                "starting_balance": pd.to_numeric(starting_balance, errors="coerce"),
                "gain_or_interest": pd.to_numeric(gain_or_interest, errors="coerce"),
                "monthly_deposit": pd.to_numeric(monthly_deposit, errors="coerce"),
                "ending_balance": pd.to_numeric(ending_balance, errors="coerce"),
                "section_order": section_order,
                "source_file": source_file
            })

    if not rows:
        return None

    investments = pd.DataFrame(rows)
    investments["period_sort"] = pd.to_numeric(investments["period"], errors="coerce")
    return investments



# -----------------------------
# Financial Planning Loader
# -----------------------------
def load_all_financial_planning_files():
    planning_files = []
    patterns = [
        "*FinancialPlanning*.xlsx",
        "*financialplanning*.xlsx",
        "*Financial_Planning*.xlsx",
        "*financial_planning*.xlsx",
        "*Planning*.xlsx",
        "*planning*.xlsx"
    ]

    for pattern in patterns:
        planning_files.extend(sorted(DATA_FOLDER.glob(pattern)))

    planning_files = sorted({
        file for file in planning_files
        if not file.name.startswith("~$")
        and "investment" not in file.name.lower()
        and "expenses" not in file.name.lower()
    })

    if not planning_files:
        return None, []

    parsed_files = []

    for file in planning_files:
        try:
            raw = pd.read_excel(file, sheet_name=0, header=None)
            parsed = parse_financial_planning_workbook(raw, file.name)
            if parsed is not None:
                parsed_files.append(parsed)
        except Exception as e:
            st.warning(f"Could not read planning file {file.name}: {e}")

    if not parsed_files:
        return None, planning_files

    # For now, use the latest/last matching file as the planning model.
    # This keeps the dashboard simple and avoids accidental duplicates.
    return parsed_files[-1], planning_files


def safe_number(value):
    return pd.to_numeric(value, errors="coerce")


def money_text(value, currency="AED"):
    value = safe_number(value)
    if pd.isna(value):
        return "-"
    return f"{currency} {value:,.0f}"


def percent_text(value):
    value = safe_number(value)
    if pd.isna(value):
        return "-"
    return f"{value * 100:.2f}%"


def parse_financial_planning_workbook(raw, source_file):
    planning = {
        "source_file": source_file,
        "scenarios": pd.DataFrame(),
        "living_costs": pd.DataFrame(),
        "savings_only": pd.DataFrame(),
        "calc_main": pd.DataFrame(),
        "calc_assets": pd.DataFrame(),
        "calc_summary": pd.DataFrame()
    }

    # Scenario blocks are laid out side-by-side in the uploaded workbook.
    scenario_starts = [8, 16, 24]
    scenario_names = [
        "Scenario 1 - With All Investments",
        "Scenario 2 - Without Indian Savings",
        "Scenario 3 - Without Indian Savings + Real Estate"
    ]

    scenario_rows = []
    living_rows = []
    savings_rows = []

    for scenario_name, start_col in zip(scenario_names, scenario_starts):
        # Main projection table: rows 2 to 6, columns start_col to start_col + 6
        for row_idx in range(2, 7):
            period = raw.iloc[row_idx, start_col]
            if pd.isna(period):
                continue

            scenario_rows.append({
                "scenario": scenario_name,
                "period": str(period).strip(),
                "total_value": safe_number(raw.iloc[row_idx, start_col + 1]),
                "monthly_low_7": safe_number(raw.iloc[row_idx, start_col + 2]),
                "monthly_avg_12": safe_number(raw.iloc[row_idx, start_col + 3]),
                "monthly_best_20": safe_number(raw.iloc[row_idx, start_col + 4]),
                "experiment_loss_pct": safe_number(raw.iloc[row_idx, start_col + 5]),
                "freedom_cost_pct": safe_number(raw.iloc[row_idx, start_col + 6])
            })

        # Monthly lifestyle cost section: rows 13 to 18
        for row_idx in range(13, 19):
            item = raw.iloc[row_idx, start_col]
            amount = raw.iloc[row_idx, start_col + 1]
            if pd.isna(item) or pd.isna(amount):
                continue

            living_rows.append({
                "scenario": scenario_name,
                "cost_item": str(item).strip(),
                "monthly_amount": safe_number(amount)
            })

        # Savings only section: rows 20 to 24
        for row_idx in range(20, 25):
            period = raw.iloc[row_idx, start_col]
            if pd.isna(period):
                continue

            savings_rows.append({
                "scenario": scenario_name,
                "period": str(period).strip(),
                "monthly_savings": safe_number(raw.iloc[row_idx, start_col + 1]),
                "savings_per_day": safe_number(raw.iloc[row_idx, start_col + 2]),
                "one_month": safe_number(raw.iloc[row_idx, start_col + 3]),
                "two_months": safe_number(raw.iloc[row_idx, start_col + 4]),
                "twelve_months": safe_number(raw.iloc[row_idx, start_col + 5])
            })

    planning["scenarios"] = pd.DataFrame(scenario_rows)
    planning["living_costs"] = pd.DataFrame(living_rows)
    planning["savings_only"] = pd.DataFrame(savings_rows)

    # Calculation block on the left side.
    calc_main_rows = []
    for row_idx in range(3, 10):
        item = raw.iloc[row_idx, 0]
        if pd.isna(item):
            continue
        calc_main_rows.append({
            "component": str(item).strip(),
            "2_years_value": safe_number(raw.iloc[row_idx, 1]),
            "2_years_alt": safe_number(raw.iloc[row_idx, 2]),
            "3_years_value": safe_number(raw.iloc[row_idx, 3]),
            "3_years_alt": safe_number(raw.iloc[row_idx, 4]),
            "4_years_value": safe_number(raw.iloc[row_idx, 5]),
            "4_years_alt": safe_number(raw.iloc[row_idx, 6])
        })

    planning["calc_main"] = pd.DataFrame(calc_main_rows)

    calc_asset_rows = []
    for row_idx in range(14, 24):
        item = raw.iloc[row_idx, 0]
        if pd.isna(item):
            continue
        calc_asset_rows.append({
            "component": str(item).strip(),
            "amount": safe_number(raw.iloc[row_idx, 1])
        })

    planning["calc_assets"] = pd.DataFrame(calc_asset_rows)

    calc_summary_rows = []
    summary_map = {
        "Starting Portfolio": 25,
        "12 Month Salary Savings": 26,
        "Projected 2 Year Capital": 27,
        "Projected 4 Year Capital": 28
    }

    for label, row_idx in summary_map.items():
        calc_summary_rows.append({
            "metric": label,
            "with_all_investments": safe_number(raw.iloc[row_idx, 1]),
            "without_indian_savings": safe_number(raw.iloc[row_idx, 2]),
            "without_indian_and_real_estate": safe_number(raw.iloc[row_idx, 3])
        })

    planning["calc_summary"] = pd.DataFrame(calc_summary_rows)

    return planning

# -----------------------------
# Chart Styling
# -----------------------------
CHART_BG = "#111827"
CHART_TEXT = "#ffffff"
CHART_ORANGE = "#f97316"
CHART_GRID = "#374151"


def style_chart_base(fig):
    fig.update_layout(
        paper_bgcolor=CHART_BG,
        plot_bgcolor=CHART_BG,
        font=dict(color=CHART_TEXT),
        title=dict(
            font=dict(color=CHART_TEXT, size=20)
        ),
        xaxis=dict(
            color=CHART_TEXT,
            gridcolor=CHART_GRID,
            zerolinecolor=CHART_GRID,
            tickfont=dict(color=CHART_TEXT),
            title=dict(font=dict(color=CHART_TEXT))
        ),
        yaxis=dict(
            color=CHART_TEXT,
            gridcolor=CHART_GRID,
            zerolinecolor=CHART_GRID,
            tickfont=dict(color=CHART_TEXT),
            title=dict(font=dict(color=CHART_TEXT))
        ),
        legend=dict(
            font=dict(color=CHART_TEXT),
            bgcolor="rgba(0,0,0,0)"
        ),
        margin=dict(l=35, r=35, t=75, b=45)
    )
    return fig


def style_bar_chart(fig):
    fig.update_traces(
        marker_color=CHART_ORANGE,
        textposition="outside",
        textfont=dict(color=CHART_TEXT, size=12),
        outsidetextfont=dict(color=CHART_TEXT, size=12),
        cliponaxis=False
    )
    fig.update_layout(uniformtext_minsize=8, uniformtext_mode="hide")
    return style_chart_base(fig)


def style_line_chart(fig):
    fig.update_traces(
        line=dict(color=CHART_ORANGE, width=3),
        marker=dict(color=CHART_ORANGE, size=8),
        textfont=dict(color=CHART_TEXT, size=12),
        textposition="top center"
    )
    return style_chart_base(fig)


def style_stacked_bar_chart(fig):
    fig.update_traces(
        texttemplate="%{y:,.0f}",
        textposition="inside",
        textfont=dict(color=CHART_TEXT, size=11),
        insidetextfont=dict(color=CHART_TEXT, size=11)
    )
    return style_chart_base(fig)


def style_scatter_chart(fig):
    fig.update_traces(
        marker=dict(size=9),
        textfont=dict(color=CHART_TEXT, size=12)
    )
    return style_chart_base(fig)


# -----------------------------
# Helpers
# -----------------------------
def add_change_labels(df, value_col, label_col, suffix="", decimals=1):
    df = df.copy()
    df["change_pct"] = (df[value_col].pct_change() * 100).round(1)

    def make_label(row):
        value = row[value_col]
        change = row["change_pct"]

        if pd.isna(value):
            return ""

        value_text = f"{value:,.{decimals}f}{suffix}"

        if pd.isna(change):
            return f"{value_text} (0%)"

        sign = "+" if change >= 0 else ""
        return f"{value_text} ({sign}{change}%)"

    df[label_col] = df.apply(make_label, axis=1)
    return df


def insight_arrow(current, previous, higher_is_better=True):
    if pd.isna(previous) or previous == 0:
        return "No previous month to compare."

    change = ((current - previous) / previous) * 100
    good = change > 0 if higher_is_better else change < 0
    direction = "improved" if good else "dropped"

    return f"{direction} by {abs(change):.1f}% vs last month."


def section_header(title, subtitle):
    st.markdown(
        f"""
        <div class="section-card">
            <div class="section-title">{title}</div>
            <div class="section-subtitle">{subtitle}</div>
        </div>
        """,
        unsafe_allow_html=True
    )


def insight_card(title, text):
    st.markdown(
        f"""
        <div class="insight-card">
            <h4>💡 {title}</h4>
            <p>{text}</p>
        </div>
        """,
        unsafe_allow_html=True
    )


def fancy_card(icon, label, value):
    st.markdown(
        f"""
        <div class="fancy-card">
            <div class="icon">{icon}</div>
            <div class="label">{label}</div>
            <div class="value">{value}</div>
        </div>
        """,
        unsafe_allow_html=True
    )


# -----------------------------
# Cleaning
# -----------------------------
def clean_ringconn_files(sleep, activity, vitals):
    sleep["date"] = pd.to_datetime(sleep["Wake-up time"]).dt.date
    sleep["month"] = pd.to_datetime(sleep["date"]).dt.to_period("M").astype(str)

    sleep["Time Asleep(min)"] = pd.to_numeric(sleep["Time Asleep(min)"], errors="coerce")
    sleep["sleep_hours"] = sleep["Time Asleep(min)"] / 60

    sleep["sleep_efficiency"] = (
        sleep["Sleep Time Ratio(%)"]
        .astype(str)
        .str.replace("%", "", regex=False)
        .astype(float)
    )

    sleep_stage_cols = [
        "Sleep Stages - Awake(min)",
        "Sleep Stages - REM(min)",
        "Sleep Stages - Light Sleep(min)",
        "Sleep Stages - Deep Sleep(min)"
    ]

    for col in sleep_stage_cols:
        if col in sleep.columns:
            sleep[col] = pd.to_numeric(sleep[col], errors="coerce")

    activity["date"] = pd.to_datetime(activity["Date"]).dt.date
    activity["month"] = pd.to_datetime(activity["date"]).dt.to_period("M").astype(str)
    activity["Steps"] = pd.to_numeric(activity["Steps"], errors="coerce")

    if "Calories(kcal)" in activity.columns:
        activity["Calories(kcal)"] = pd.to_numeric(activity["Calories(kcal)"], errors="coerce")

    vitals["date"] = pd.to_datetime(vitals["Date"]).dt.date
    vitals["month"] = pd.to_datetime(vitals["date"]).dt.to_period("M").astype(str)

    vitals["Avg. HRV(ms)"] = pd.to_numeric(vitals["Avg. HRV(ms)"], errors="coerce")
    vitals["Avg. Heart Rate(bpm)"] = pd.to_numeric(vitals["Avg. Heart Rate(bpm)"], errors="coerce")

    vitals["Avg. Spo2(%)"] = (
        vitals["Avg. Spo2(%)"]
        .astype(str)
        .str.replace("%", "", regex=False)
        .astype(float)
    )

    return sleep, activity, vitals


def clean_workouts(workouts):
    workouts = workouts.copy()
    workouts.columns = workouts.columns.str.strip()

    required_cols = ["date", "steps", "distance", "runDistance", "calories"]
    missing_cols = [col for col in required_cols if col not in workouts.columns]

    if missing_cols:
        st.warning(f"Workout file is missing these columns: {missing_cols}")
        return None

    workouts["date"] = pd.to_datetime(
        workouts["date"],
        dayfirst=True,
        errors="coerce"
    )

    for col in ["steps", "distance", "runDistance", "calories"]:
        workouts[col] = pd.to_numeric(workouts[col], errors="coerce").fillna(0)

    workouts = workouts.dropna(subset=["date"])
    workouts["date_only"] = workouts["date"].dt.date
    workouts["month"] = workouts["date"].dt.to_period("M").astype(str)
    workouts["distance_km"] = workouts["distance"] / 1000
    workouts["run_distance_km"] = workouts["runDistance"] / 1000
    workouts["walking_distance_km"] = (workouts["distance"] - workouts["runDistance"]).clip(lower=0) / 1000
    workouts["active_day"] = workouts["steps"] > 0
    workouts["step_goal_hit"] = workouts["steps"] >= 8000
    workouts["high_activity_day"] = workouts["steps"] >= 10000

    return workouts


def clean_expenses(expenses):
    expenses.columns = expenses.columns.str.strip()

    baseline = expenses[expenses["expense_file_type"] == "Baseline"].copy()
    transactions = expenses[expenses["expense_file_type"] == "Transactions"].copy()

    cleaned_parts = []

    if not baseline.empty:
        required_cols = ["Category", "Sub-Category", "Amount", "% Spend"]
        missing_cols = [col for col in required_cols if col not in baseline.columns]

        if missing_cols:
            st.error(f"Baseline expenses file is missing these columns: {missing_cols}")
            st.stop()

        baseline["amount"] = pd.to_numeric(baseline["Amount"], errors="coerce").abs()
        baseline["net_amount"] = baseline["amount"]
        baseline["category"] = baseline["Category"].fillna("Uncategorized")
        baseline["sub_category"] = baseline["Sub-Category"].fillna("Uncategorized")
        baseline["description"] = baseline["sub_category"]

        baseline["percent_spend"] = (
            baseline["% Spend"]
            .astype(str)
            .str.replace("%", "", regex=False)
            .astype(float)
        )

        baseline["date"] = pd.NaT
        baseline["bank"] = "Baseline"
        baseline["card"] = "Baseline"
        baseline["transaction_type"] = "Baseline"
        baseline["currency"] = "AED"
        baseline["day_of_week"] = ""
        baseline["week"] = ""
        baseline["is_transaction"] = False

        cleaned_parts.append(baseline)

    if not transactions.empty:
        required_cols = [
            "Date",
            "Bank",
            "Card / Account",
            "Description",
            "Category",
            "Sub-Category",
            "Type",
            "Amount (AED)",
            "Net Amount (AED)",
            "Month",
            "Week",
            "Day of Week",
            "Currency"
        ]

        missing_cols = [col for col in required_cols if col not in transactions.columns]

        if missing_cols:
            st.error(f"Monthly transaction file is missing these columns: {missing_cols}")
            st.stop()

        if pd.api.types.is_numeric_dtype(transactions["Date"]):
            transactions["date"] = pd.to_datetime(
                transactions["Date"],
                unit="D",
                origin="1899-12-30",
                errors="coerce"
            )
        else:
            transactions["date"] = pd.to_datetime(transactions["Date"], errors="coerce")

        transactions["amount"] = pd.to_numeric(transactions["Amount (AED)"], errors="coerce").abs()
        transactions["net_amount"] = pd.to_numeric(transactions["Net Amount (AED)"], errors="coerce")
        transactions["bank"] = transactions["Bank"].fillna("Unknown")
        transactions["card"] = transactions["Card / Account"].fillna("Unknown")
        transactions["description"] = transactions["Description"].fillna("Unknown")
        transactions["category"] = transactions["Category"].fillna("Uncategorized")
        transactions["sub_category"] = transactions["Sub-Category"].fillna("Uncategorized")
        transactions["transaction_type"] = transactions["Type"].fillna("Unknown")
        transactions["currency"] = transactions["Currency"].fillna("AED")
        transactions["week"] = transactions["Week"].fillna("")
        transactions["day_of_week"] = transactions["Day of Week"].fillna("")
        transactions["percent_spend"] = None
        transactions["is_transaction"] = True

        transactions = transactions.dropna(subset=["date", "amount"])

        cleaned_parts.append(transactions)

    if not cleaned_parts:
        return None

    return pd.concat(cleaned_parts, ignore_index=True)


# -----------------------------
# Score logic
# -----------------------------
def score_sleep_quality(avg_sleep, avg_efficiency):
    sleep_hours_score = min(40, avg_sleep / 8 * 40)
    efficiency_score = min(40, avg_efficiency / 100 * 40)
    base_bonus = 20
    score = sleep_hours_score + efficiency_score + base_bonus
    return max(0, min(100, round(score)))


def score_recovery(avg_hrv, avg_hr, avg_spo2):
    hrv_score = min(45, avg_hrv / 60 * 45)
    hr_score = min(35, max(0, (80 - avg_hr) / 30 * 35))
    spo2_score = min(20, avg_spo2 / 98 * 20)
    score = hrv_score + hr_score + spo2_score
    return max(0, min(100, round(score)))


def score_sleep_consistency(sleep_df):
    daily_sleep = sleep_df.groupby("date", as_index=False)["sleep_hours"].mean()
    sleep_std = daily_sleep["sleep_hours"].std()

    if pd.isna(sleep_std):
        return 100

    score = 100 - (sleep_std * 20)
    return max(0, min(100, round(score)))


def score_activity(avg_steps):
    return max(0, min(100, round(avg_steps / 8000 * 100)))


# -----------------------------
# Load data
# -----------------------------
sleep, activity, vitals, sleep_files, activity_files, vitals_files = load_ringconn_data()
expenses, expense_files = load_all_expense_files()
investments, investment_files = load_all_investment_files()
planning, planning_files = load_all_financial_planning_files()
workouts, workout_files = load_all_workout_files()

if sleep is None or activity is None or vitals is None:
    st.error("Data loading stopped before dashboard could open.")

    st.write("DATA_SOURCE_LABEL:", DATA_SOURCE_LABEL)
    st.write("DATA_SOURCE_DETAIL:", DATA_SOURCE_DETAIL)
    st.write("GOOGLE_DRIVE_STATUS:", GOOGLE_DRIVE_STATUS)
    st.write("ACTIVE DATA_FOLDER:", DATA_FOLDER)

    if DATA_SOURCE_LABEL != "Google Drive":
        st.warning("Google Drive did not sync, so the app fell back to the local C:\\LifeDashboard\\data folder. On Streamlit Cloud, that folder does not exist.")
    else:
        st.warning("Google Drive synced, but required RingConn files were not found in the synced folder.")

    st.info("Required files: sleep_YYYY_MM.csv, activity_YYYY_MM.csv, vitals_YYYY_MM.csv")
    st.code("""
sleep_2026_06.csv
activity_2026_06.csv
vitals_2026_06.csv
    """)
    st.stop()

sleep, activity, vitals = clean_ringconn_files(sleep, activity, vitals)

if workouts is not None:
    workouts = clean_workouts(workouts)

if expenses is not None:
    expenses = clean_expenses(expenses)

st.markdown(
    f"""
    <span class="success-pill">
        ✅ Data loaded: {len(sleep_files)} sleep files · {len(activity_files)} activity files · {len(vitals_files)} vitals files · {len(workout_files)} workout files
    </span>
    """,
    unsafe_allow_html=True
)


# -----------------------------
# Sidebar Navigation
# -----------------------------
with st.sidebar:
    st.markdown("## 🚀 Life OS")
    st.markdown("**Your local personal analytics cockpit.**")
    st.caption("Built for clarity. Designed for action.")
    st.divider()

    page = st.radio(
        "🧭 Navigate",
        [
            "Health Tracking",
            "Expense Tracking",
            "Planning",
            "Investments"
        ]
    )

    st.divider()
    st.markdown("### ☁️ Data Source")
    st.markdown(f"**{DATA_SOURCE_LABEL}**")
    st.caption(DATA_SOURCE_DETAIL)
    st.caption(GOOGLE_DRIVE_STATUS)

    if st.button("🔄 Refresh cloud data"):
        st.cache_data.clear()
        st.rerun()

    st.markdown("### 🗂️ Active Data Folder")
    st.code(str(DATA_FOLDER))

    st.markdown("### 📦 Loaded Sources")
    st.write(f"🛌 Sleep: {len(sleep_files)} files")
    st.write(f"👣 Activity: {len(activity_files)} files")
    st.write(f"❤️ Vitals: {len(vitals_files)} files")

    if workout_files:
        st.write(f"🏃 Workouts: {len(workout_files)} files")

    if expense_files:
        st.write(f"💳 Expenses: {len(expense_files)} files")

    if investment_files:
        st.write(f"📈 Investments: {len(investment_files)} files")

    if planning_files:
        st.write(f"🗓️ Planning: {len(planning_files)} files")

    st.divider()
    st.caption("⚡ Tip: Drop new monthly files into the data folder and refresh.")


# -----------------------------
# HEALTH TRACKING
# -----------------------------
if page == "Health Tracking":
    section_header(
        "🏥 Health Tracking",
        "Sleep, recovery, activity, workouts, vitals, and monthly health signals."
    )

    st.markdown(
        """
        <div class="fancy-card-grid">
            <div class="fancy-card"><div class="icon">🛌</div><div class="label">Sleep engine</div><div class="value">Recovery</div></div>
            <div class="fancy-card"><div class="icon">👣</div><div class="label">Movement</div><div class="value">Steps</div></div>
            <div class="fancy-card"><div class="icon">🏃</div><div class="label">Training</div><div class="value">Workouts</div></div>
            <div class="fancy-card"><div class="icon">❤️</div><div class="label">Vitals</div><div class="value">HRV</div></div>
        </div>
        """,
        unsafe_allow_html=True
    )

    scorecard_tab, sleep_tab, activity_tab, workout_tab, vitals_tab, relationships_tab, health_insights_tab = st.tabs([
        "🏆 Scorecard",
        "🛌 Sleep",
        "👣 Activity",
        "🏃 Workouts",
        "❤️ Vitals",
        "🔗 Relationships",
        "💡 Monthly Insights"
    ])

    monthly_sleep = sleep.groupby("month", as_index=False).agg({
        "sleep_hours": "mean",
        "sleep_efficiency": "mean",
        "Sleep Stages - Awake(min)": "mean",
        "Sleep Stages - REM(min)": "mean",
        "Sleep Stages - Light Sleep(min)": "mean",
        "Sleep Stages - Deep Sleep(min)": "mean"
    })

    monthly_sleep["sleep_quality_score"] = monthly_sleep.apply(
        lambda x: score_sleep_quality(x["sleep_hours"], x["sleep_efficiency"]),
        axis=1
    )

    monthly_sleep = add_change_labels(monthly_sleep, "sleep_hours", "sleep_hours_label", "h", 1)
    monthly_sleep = add_change_labels(monthly_sleep, "sleep_efficiency", "sleep_efficiency_label", "%", 1)
    monthly_sleep = add_change_labels(monthly_sleep, "sleep_quality_score", "sleep_quality_label", "", 0)

    monthly_activity = activity.groupby("month", as_index=False).agg({
        "Steps": "mean",
        "Calories(kcal)": "mean"
    })

    monthly_activity["activity_score"] = monthly_activity["Steps"].apply(score_activity)

    monthly_activity = add_change_labels(monthly_activity, "Steps", "steps_label", "", 0)
    monthly_activity = add_change_labels(monthly_activity, "Calories(kcal)", "calories_label", "", 0)
    monthly_activity = add_change_labels(monthly_activity, "activity_score", "activity_score_label", "", 0)

    if workouts is not None and not workouts.empty:
        monthly_workouts = workouts.groupby("month", as_index=False).agg({
            "steps": "mean",
            "distance_km": "sum",
            "run_distance_km": "sum",
            "walking_distance_km": "sum",
            "calories": "sum",
            "active_day": "sum",
            "step_goal_hit": "sum",
            "high_activity_day": "sum"
        })

        monthly_workouts["activity_days"] = workouts.groupby("month")["date_only"].nunique().values
        monthly_workouts["avg_daily_steps"] = monthly_workouts["steps"]
        monthly_workouts["goal_hit_rate"] = (monthly_workouts["step_goal_hit"] / monthly_workouts["activity_days"] * 100).round(1)
        monthly_workouts["run_share_pct"] = (monthly_workouts["run_distance_km"] / monthly_workouts["distance_km"] * 100).replace([float("inf"), -float("inf")], 0).fillna(0).round(1)
        monthly_workouts = add_change_labels(monthly_workouts, "avg_daily_steps", "steps_label", "", 0)
        monthly_workouts = add_change_labels(monthly_workouts, "distance_km", " km", "", 1) if False else monthly_workouts
    else:
        monthly_workouts = pd.DataFrame()

    monthly_vitals = vitals.groupby("month", as_index=False).agg({
        "Avg. Heart Rate(bpm)": "mean",
        "Avg. HRV(ms)": "mean",
        "Avg. Spo2(%)": "mean"
    })

    monthly_vitals["recovery_score"] = monthly_vitals.apply(
        lambda x: score_recovery(
            x["Avg. HRV(ms)"],
            x["Avg. Heart Rate(bpm)"],
            x["Avg. Spo2(%)"]
        ),
        axis=1
    )

    monthly_vitals = add_change_labels(monthly_vitals, "recovery_score", "recovery_label", "", 0)
    monthly_vitals = add_change_labels(monthly_vitals, "Avg. Heart Rate(bpm)", "hr_label", " bpm", 0)
    monthly_vitals = add_change_labels(monthly_vitals, "Avg. HRV(ms)", "hrv_label", " ms", 0)
    monthly_vitals = add_change_labels(monthly_vitals, "Avg. Spo2(%)", "spo2_label", "%", 1)

    with scorecard_tab:
        avg_sleep = sleep["sleep_hours"].mean()
        avg_efficiency = sleep["sleep_efficiency"].mean()
        avg_steps = activity["Steps"].mean()
        avg_hrv = vitals["Avg. HRV(ms)"].mean()
        avg_hr = vitals["Avg. Heart Rate(bpm)"].mean()
        avg_spo2 = vitals["Avg. Spo2(%)"].mean()

        sleep_quality_score = score_sleep_quality(avg_sleep, avg_efficiency)
        recovery_score = score_recovery(avg_hrv, avg_hr, avg_spo2)
        sleep_consistency_score = score_sleep_consistency(sleep)
        activity_score = score_activity(avg_steps)

        health_score = round(
            (
                sleep_quality_score
                + recovery_score
                + sleep_consistency_score
                + activity_score
            ) / 4
        )

        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Health Score", f"{health_score}/100")
        col2.metric("Sleep Quality", f"{sleep_quality_score}/100")
        col3.metric("Recovery", f"{recovery_score}/100")
        col4.metric("Sleep Consistency", f"{sleep_consistency_score}/100")
        col5.metric("Activity", f"{activity_score}/100")

        st.divider()

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Avg Sleep", f"{avg_sleep:.2f} hrs")
        c2.metric("Sleep Efficiency", f"{avg_efficiency:.1f}%")
        c3.metric("Avg Steps", f"{avg_steps:,.0f}")
        c4.metric("Avg HRV", f"{avg_hrv:.0f} ms")
        c5.metric("Avg Heart Rate", f"{avg_hr:.0f} bpm")

    with sleep_tab:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Current Month Sleep", f"{monthly_sleep['sleep_hours'].iloc[-1]:.2f} hrs")
        c2.metric("Average Sleep", f"{monthly_sleep['sleep_hours'].mean():.2f} hrs")
        c3.metric("Best Sleep Month", monthly_sleep.loc[monthly_sleep["sleep_hours"].idxmax(), "month"])
        c4.metric("Lowest Sleep Month", monthly_sleep.loc[monthly_sleep["sleep_hours"].idxmin(), "month"])

        fig_quality = px.line(
            monthly_sleep,
            x="month",
            y="sleep_quality_score",
            text="sleep_quality_label",
            markers=True,
            title="Monthly Sleep Quality Score"
        )
        st.plotly_chart(style_line_chart(fig_quality), use_container_width=True)

        fig_sleep = px.line(
            monthly_sleep,
            x="month",
            y="sleep_hours",
            text="sleep_hours_label",
            markers=True,
            title="Monthly Sleep Hours"
        )
        st.plotly_chart(style_line_chart(fig_sleep), use_container_width=True)

        fig_eff = px.line(
            monthly_sleep,
            x="month",
            y="sleep_efficiency",
            text="sleep_efficiency_label",
            markers=True,
            title="Monthly Sleep Efficiency"
        )
        st.plotly_chart(style_line_chart(fig_eff), use_container_width=True)

        stage_cols = [
            "Sleep Stages - Awake(min)",
            "Sleep Stages - REM(min)",
            "Sleep Stages - Light Sleep(min)",
            "Sleep Stages - Deep Sleep(min)"
        ]

        stage_data = monthly_sleep.melt(
            id_vars="month",
            value_vars=stage_cols,
            var_name="Sleep Stage",
            value_name="Minutes"
        )

        stage_data["Stage Total"] = stage_data.groupby("month")["Minutes"].transform("sum")
        stage_data["Stage %"] = ((stage_data["Minutes"] / stage_data["Stage Total"]) * 100).round(1)

        stage_data["Label"] = (
            stage_data["Minutes"].round(0).astype(int).astype(str)
            + "m ("
            + stage_data["Stage %"].astype(str)
            + "%)"
        )

        fig_stages = px.bar(
            stage_data,
            x="month",
            y="Minutes",
            color="Sleep Stage",
            text="Label",
            title="Sleep Stage Breakdown by Month",
            barmode="stack"
        )
        st.plotly_chart(style_stacked_bar_chart(fig_stages), use_container_width=True)

        st.dataframe(monthly_sleep, use_container_width=True)

    with activity_tab:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Current Month Steps", f"{monthly_activity['Steps'].iloc[-1]:,.0f}")
        c2.metric("Average Steps", f"{monthly_activity['Steps'].mean():,.0f}")
        c3.metric("Best Steps Month", monthly_activity.loc[monthly_activity["Steps"].idxmax(), "month"])
        c4.metric("Lowest Steps Month", monthly_activity.loc[monthly_activity["Steps"].idxmin(), "month"])

        fig_activity_score = px.line(
            monthly_activity,
            x="month",
            y="activity_score",
            text="activity_score_label",
            markers=True,
            title="Monthly Activity Score"
        )
        st.plotly_chart(style_line_chart(fig_activity_score), use_container_width=True)

        fig_steps = px.bar(
            monthly_activity,
            x="month",
            y="Steps",
            text="steps_label",
            title="Average Daily Steps by Month"
        )
        st.plotly_chart(style_bar_chart(fig_steps), use_container_width=True)

        fig_calories = px.line(
            monthly_activity,
            x="month",
            y="Calories(kcal)",
            text="calories_label",
            markers=True,
            title="Average Calories by Month"
        )
        st.plotly_chart(style_line_chart(fig_calories), use_container_width=True)

        st.dataframe(monthly_activity, use_container_width=True)

    with workout_tab:
        if workouts is None or workouts.empty:
            st.info("Add workout CSV files to C:\\LifeDashboard\\data using this name format:")
            st.code("workouts_2026_06.csv")
        else:
            available_workout_months = sorted(workouts["month"].dropna().unique(), reverse=True)

            selected_workout_month = st.selectbox(
                "Select workout month",
                ["All Months"] + available_workout_months,
                index=0
            )

            if selected_workout_month == "All Months":
                filtered_workouts = workouts.copy()
                filtered_monthly_workouts = monthly_workouts.copy()
                workout_period_label = "All Months"
            else:
                filtered_workouts = workouts[workouts["month"] == selected_workout_month].copy()
                filtered_monthly_workouts = monthly_workouts[monthly_workouts["month"] == selected_workout_month].copy()
                workout_period_label = selected_workout_month

            if filtered_workouts.empty:
                st.info("No workout records found for the selected month.")
            else:
                total_steps = filtered_workouts["steps"].sum()
                avg_steps = filtered_workouts["steps"].mean()
                total_distance = filtered_workouts["distance_km"].sum()
                total_run_distance = filtered_workouts["run_distance_km"].sum()
                total_calories = filtered_workouts["calories"].sum()
                goal_days = int(filtered_workouts["step_goal_hit"].sum())
                active_days = filtered_workouts["date_only"].nunique()

                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("Selected Period", workout_period_label)
                c2.metric("Avg Steps", f"{avg_steps:,.0f}")
                c3.metric("Total Distance", f"{total_distance:,.1f} km")
                c4.metric("Run Distance", f"{total_run_distance:,.1f} km")
                c5.metric("Calories", f"{total_calories:,.0f}")

                c6, c7, c8 = st.columns(3)
                c6.metric("Total Steps", f"{total_steps:,.0f}")
                c7.metric("8K Step Goal Days", f"{goal_days}")
                c8.metric("Active Days", f"{active_days}")

                chart_title_suffix = workout_period_label

                fig_monthly_workout_steps = px.line(
                    filtered_monthly_workouts.sort_values("month"),
                    x="month",
                    y="avg_daily_steps",
                    text="steps_label",
                    markers=True,
                    title=f"Average Daily Steps by Month - {chart_title_suffix}"
                )
                st.plotly_chart(style_line_chart(fig_monthly_workout_steps), use_container_width=True)

                distance_mix = filtered_monthly_workouts[["month", "run_distance_km", "walking_distance_km"]].melt(
                    id_vars="month",
                    value_vars=["run_distance_km", "walking_distance_km"],
                    var_name="Distance Type",
                    value_name="Kilometers"
                )
                distance_mix["Distance Type"] = distance_mix["Distance Type"].replace({
                    "run_distance_km": "Running",
                    "walking_distance_km": "Walking / Other"
                })
                distance_mix["label"] = distance_mix["Kilometers"].round(1).astype(str) + " km"

                fig_distance_mix = px.bar(
                    distance_mix,
                    x="month",
                    y="Kilometers",
                    color="Distance Type",
                    text="label",
                    title=f"Distance Mix - {chart_title_suffix}",
                    barmode="stack"
                )
                st.plotly_chart(style_stacked_bar_chart(fig_distance_mix), use_container_width=True)

                daily_workout = filtered_workouts.sort_values("date").copy()

                if selected_workout_month == "All Months":
                    daily_workout["date_label"] = daily_workout["date"].dt.strftime("%d %b %Y")
                else:
                    daily_workout["date_label"] = daily_workout["date"].dt.strftime("%d %b")

                fig_daily_steps = px.bar(
                    daily_workout,
                    x="date_label",
                    y="steps",
                    text="steps",
                    title=f"Daily Steps - {chart_title_suffix}"
                )
                st.plotly_chart(style_bar_chart(fig_daily_steps), use_container_width=True)

                fig_daily_calories = px.line(
                    daily_workout,
                    x="date_label",
                    y="calories",
                    text="calories",
                    markers=True,
                    title=f"Daily Workout Calories - {chart_title_suffix}"
                )
                st.plotly_chart(style_line_chart(fig_daily_calories), use_container_width=True)

                st.subheader("Workout Insights")
                best_day = filtered_workouts.loc[filtered_workouts["steps"].idxmax()]
                best_calorie_day = filtered_workouts.loc[filtered_workouts["calories"].idxmax()]

                if total_distance > 0:
                    running_share = (total_run_distance / total_distance) * 100
                else:
                    running_share = 0

                insight_card(
                    "Best Step Day",
                    f"{best_day['date'].strftime('%d %b %Y')} was your strongest step day with {best_day['steps']:,.0f} steps."
                )
                insight_card(
                    "Best Calorie Day",
                    f"{best_calorie_day['date'].strftime('%d %b %Y')} burned the most workout calories at {best_calorie_day['calories']:,.0f}."
                )
                insight_card(
                    "Running Share",
                    f"Running made up {running_share:.1f}% of your tracked distance for {workout_period_label}."
                )

                st.dataframe(
                    filtered_workouts.sort_values("date", ascending=False),
                    use_container_width=True
                )

    with vitals_tab:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Current HRV", f"{monthly_vitals['Avg. HRV(ms)'].iloc[-1]:.0f} ms")
        c2.metric("Current HR", f"{monthly_vitals['Avg. Heart Rate(bpm)'].iloc[-1]:.0f} bpm")
        c3.metric("Current SpO2", f"{monthly_vitals['Avg. Spo2(%)'].iloc[-1]:.1f}%")
        c4.metric("Recovery Score", f"{monthly_vitals['recovery_score'].iloc[-1]:.0f}/100")

        fig_recovery = px.line(
            monthly_vitals,
            x="month",
            y="recovery_score",
            text="recovery_label",
            markers=True,
            title="Monthly Recovery Score"
        )
        st.plotly_chart(style_line_chart(fig_recovery), use_container_width=True)

        fig_hr = px.line(
            monthly_vitals,
            x="month",
            y="Avg. Heart Rate(bpm)",
            text="hr_label",
            markers=True,
            title="Average Heart Rate by Month"
        )
        st.plotly_chart(style_line_chart(fig_hr), use_container_width=True)

        fig_hrv = px.line(
            monthly_vitals,
            x="month",
            y="Avg. HRV(ms)",
            text="hrv_label",
            markers=True,
            title="Average HRV by Month"
        )
        st.plotly_chart(style_line_chart(fig_hrv), use_container_width=True)

        fig_spo2 = px.line(
            monthly_vitals,
            x="month",
            y="Avg. Spo2(%)",
            text="spo2_label",
            markers=True,
            title="Average SpO2 by Month"
        )
        st.plotly_chart(style_line_chart(fig_spo2), use_container_width=True)

        st.dataframe(monthly_vitals, use_container_width=True)

    with relationships_tab:
        daily = sleep.merge(activity, on="date", how="outer", suffixes=("_sleep", "_activity"))
        daily = daily.merge(vitals, on="date", how="outer", suffixes=("", "_vitals"))

        scatter_data = daily.dropna(subset=["Steps", "sleep_hours", "sleep_efficiency"]).copy()

        fig_sleep_steps = px.scatter(
            scatter_data,
            x="Steps",
            y="sleep_hours",
            color="sleep_efficiency",
            hover_data=["date"],
            title="Sleep Hours vs Steps"
        )
        st.plotly_chart(style_scatter_chart(fig_sleep_steps), use_container_width=True)

        hrv_sleep = daily.dropna(subset=["Avg. HRV(ms)", "sleep_hours"]).copy()

        fig_hrv_sleep = px.scatter(
            hrv_sleep,
            x="sleep_hours",
            y="Avg. HRV(ms)",
            color="sleep_efficiency",
            hover_data=["date"],
            title="HRV vs Sleep Hours"
        )
        st.plotly_chart(style_scatter_chart(fig_hrv_sleep), use_container_width=True)

        st.dataframe(daily, use_container_width=True)

    with health_insights_tab:
        monthly_health = (
            monthly_sleep[["month", "sleep_hours", "sleep_efficiency", "sleep_quality_score"]]
            .merge(
                monthly_activity[["month", "Steps", "Calories(kcal)", "activity_score"]],
                on="month",
                how="outer"
            )
            .merge(
                monthly_vitals[["month", "Avg. HRV(ms)", "Avg. Heart Rate(bpm)", "Avg. Spo2(%)", "recovery_score"]],
                on="month",
                how="outer"
            )
        )

        if workouts is not None and not workouts.empty and not monthly_workouts.empty:
            monthly_health = monthly_health.merge(
                monthly_workouts[["month", "avg_daily_steps", "distance_km", "run_distance_km", "calories", "goal_hit_rate"]],
                on="month",
                how="outer"
            )

        monthly_health["sleep_consistency_score"] = monthly_health["month"].apply(
            lambda m: score_sleep_consistency(sleep[sleep["month"] == m])
        )

        monthly_health["health_score"] = monthly_health.apply(
            lambda x: round(
                (
                    x["sleep_quality_score"]
                    + x["recovery_score"]
                    + x["sleep_consistency_score"]
                    + x["activity_score"]
                ) / 4
            ),
            axis=1
        )

        monthly_health = monthly_health.sort_values("month").reset_index(drop=True)

        if len(monthly_health) >= 2:
            current = monthly_health.iloc[-1]
            previous = monthly_health.iloc[-2]

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Health Score", f"{current['health_score']:.0f}/100")
            col2.metric("Sleep Quality", f"{current['sleep_quality_score']:.0f}/100")
            col3.metric("Recovery", f"{current['recovery_score']:.0f}/100")
            col4.metric("Activity", f"{current['activity_score']:.0f}/100")

            st.divider()

            insight_card("Sleep", insight_arrow(current["sleep_hours"], previous["sleep_hours"], True))
            insight_card("Sleep Efficiency", insight_arrow(current["sleep_efficiency"], previous["sleep_efficiency"], True))
            insight_card("Steps", insight_arrow(current["Steps"], previous["Steps"], True))

            if "distance_km" in monthly_health.columns and not pd.isna(current.get("distance_km", None)) and not pd.isna(previous.get("distance_km", None)):
                insight_card("Workout Distance", insight_arrow(current["distance_km"], previous["distance_km"], True))

            if "goal_hit_rate" in monthly_health.columns and not pd.isna(current.get("goal_hit_rate", None)) and not pd.isna(previous.get("goal_hit_rate", None)):
                insight_card("8K Step Goal Rate", insight_arrow(current["goal_hit_rate"], previous["goal_hit_rate"], True))

            insight_card("HRV", insight_arrow(current["Avg. HRV(ms)"], previous["Avg. HRV(ms)"], True))
            insight_card("Heart Rate", insight_arrow(current["Avg. Heart Rate(bpm)"], previous["Avg. Heart Rate(bpm)"], False))
            insight_card("Recovery", insight_arrow(current["recovery_score"], previous["recovery_score"], True))

        else:
            st.info("Need at least 2 months of data to generate month-over-month insights.")

        st.dataframe(monthly_health, use_container_width=True)


# -----------------------------
# EXPENSE TRACKING
# -----------------------------
if page == "Expense Tracking":
    section_header(
        "💳 Expense Tracking",
        "Baseline summary, monthly transactions, merchant behavior, and spend insights."
    )

    summary_tab, monthly_tab, transactions_tab, expense_insights_tab = st.tabs([
        "📊 12 Months Summary",
        "📅 Monthly View",
        "🧾 Transactions",
        "💡 Insights"
    ])

    if expenses is None:
        st.info(f"Add expense files to {DATA_FOLDER}.")
        st.code("""
expenses_last12_apr_2026.xlsx
expenses_2026_05.xlsx
expenses_2026_06.xlsx
        """)
    else:
        baseline_expenses = expenses[expenses["expense_period_type"] == "Last 12 Months"].copy()
        monthly_transactions = expenses[expenses["expense_file_type"] == "Transactions"].copy()

        debit_transactions = monthly_transactions[
            monthly_transactions["transaction_type"].str.lower() == "debit"
        ].copy()

        credit_transactions = monthly_transactions[
            monthly_transactions["transaction_type"].str.lower().isin(["credit", "refund"])
        ].copy()

        with summary_tab:
            total_baseline_spend = baseline_expenses["amount"].sum()

            c1, c2, c3 = st.columns(3)
            c1.metric("Last 12 Months Spend", f"AED {total_baseline_spend:,.0f}")
            c2.metric("Baseline Records", f"{len(baseline_expenses)}")
            c3.metric("Files Loaded", len(expense_files))

            if not baseline_expenses.empty:
                baseline_category = baseline_expenses.groupby(
                    "category",
                    as_index=False
                )["amount"].sum().sort_values("amount", ascending=False)

                baseline_category["amount_label"] = (
                    "AED "
                    + baseline_category["amount"].round(0).astype(int).map("{:,}".format)
                )

                fig_baseline_category = px.bar(
                    baseline_category,
                    x="category",
                    y="amount",
                    text="amount_label",
                    title="Last 12 Months Spend by Category"
                )
                st.plotly_chart(style_bar_chart(fig_baseline_category), use_container_width=True)

                baseline_sub_category = baseline_expenses.groupby(
                    "sub_category",
                    as_index=False
                )["amount"].sum().sort_values("amount", ascending=False).head(15)

                baseline_sub_category["amount_label"] = (
                    "AED "
                    + baseline_sub_category["amount"].round(0).astype(int).map("{:,}".format)
                )

                fig_baseline_sub_category = px.bar(
                    baseline_sub_category,
                    x="sub_category",
                    y="amount",
                    text="amount_label",
                    title="Top 15 Sub-Categories - Last 12 Months"
                )
                st.plotly_chart(style_bar_chart(fig_baseline_sub_category), use_container_width=True)

                st.dataframe(baseline_expenses, use_container_width=True)
            else:
                st.info("No 12 months baseline file found yet.")

        with monthly_tab:
            if not debit_transactions.empty:
                available_months = sorted(debit_transactions["month"].dropna().unique(), reverse=True)

                selected_months = st.multiselect(
                    "Select month(s)",
                    available_months,
                    default=available_months
                )

                filtered_debits = debit_transactions[
                    debit_transactions["month"].isin(selected_months)
                ].copy()

                filtered_credits = credit_transactions[
                    credit_transactions["month"].isin(selected_months)
                ].copy()

                total_monthly_spend = filtered_debits["amount"].sum()
                total_credits = filtered_credits["amount"].sum()
                net_monthly_spend = total_monthly_spend - total_credits

                transaction_count = len(filtered_debits)
                avg_transaction = filtered_debits["amount"].mean() if transaction_count > 0 else 0
                largest_transaction = filtered_debits["amount"].max() if transaction_count > 0 else 0

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Gross Spend", f"AED {total_monthly_spend:,.0f}")
                c2.metric("Credits / Refunds", f"AED {total_credits:,.0f}")
                c3.metric("Net Spend", f"AED {net_monthly_spend:,.0f}")
                c4.metric("Transactions", f"{transaction_count}")

                c5, c6, c7 = st.columns(3)
                c5.metric("Avg Transaction", f"AED {avg_transaction:,.0f}")
                c6.metric("Largest Transaction", f"AED {largest_transaction:,.0f}")
                c7.metric("Months Selected", f"{len(selected_months)}")

                monthly_expenses = filtered_debits.groupby(
                    "month",
                    as_index=False
                )["amount"].sum().sort_values("month")

                monthly_expenses = add_change_labels(
                    monthly_expenses,
                    "amount",
                    "amount_label",
                    " AED",
                    0
                )

                fig_monthly_expenses = px.bar(
                    monthly_expenses,
                    x="month",
                    y="amount",
                    text="amount_label",
                    title="Monthly Gross Expenses"
                )
                st.plotly_chart(style_bar_chart(fig_monthly_expenses), use_container_width=True)

                monthly_category = filtered_debits.groupby(
                    ["month", "category"],
                    as_index=False
                )["amount"].sum()

                fig_monthly_category = px.bar(
                    monthly_category,
                    x="month",
                    y="amount",
                    color="category",
                    text="amount",
                    title="Monthly Category Breakdown",
                    barmode="stack"
                )
                st.plotly_chart(style_stacked_bar_chart(fig_monthly_category), use_container_width=True)

                daily_spend = filtered_debits.groupby(
                    "date",
                    as_index=False
                )["amount"].sum().sort_values("date")

                daily_spend["amount_label"] = (
                    "AED "
                    + daily_spend["amount"].round(0).astype(int).map("{:,}".format)
                )

                fig_daily_spend = px.line(
                    daily_spend,
                    x="date",
                    y="amount",
                    text="amount_label",
                    markers=True,
                    title="Daily Spend Timeline"
                )
                st.plotly_chart(style_line_chart(fig_daily_spend), use_container_width=True)

                card_spend = filtered_debits.groupby(
                    ["bank", "card"],
                    as_index=False
                )["amount"].sum().sort_values("amount", ascending=False)

                card_spend["card_label"] = card_spend["bank"] + " - " + card_spend["card"]
                card_spend["amount_label"] = (
                    "AED "
                    + card_spend["amount"].round(0).astype(int).map("{:,}".format)
                )

                fig_card_spend = px.bar(
                    card_spend,
                    x="card_label",
                    y="amount",
                    text="amount_label",
                    title="Spend by Card"
                )
                st.plotly_chart(style_bar_chart(fig_card_spend), use_container_width=True)

            else:
                st.info("No monthly transaction files found yet.")

        with transactions_tab:
            if not debit_transactions.empty:
                available_months = sorted(debit_transactions["month"].dropna().unique(), reverse=True)

                selected_month = st.selectbox(
                    "Select month",
                    available_months
                )

                month_debits = debit_transactions[
                    debit_transactions["month"] == selected_month
                ].copy()

                top_merchants = month_debits.groupby(
                    "description",
                    as_index=False
                )["amount"].sum().sort_values("amount", ascending=False).head(15)

                top_merchants["amount_label"] = (
                    "AED "
                    + top_merchants["amount"].round(0).astype(int).map("{:,}".format)
                )

                fig_top_merchants = px.bar(
                    top_merchants,
                    x="description",
                    y="amount",
                    text="amount_label",
                    title=f"Top 15 Merchants by Spend - {selected_month}"
                )
                st.plotly_chart(style_bar_chart(fig_top_merchants), use_container_width=True)

                merchant_count = month_debits.groupby(
                    "description",
                    as_index=False
                ).size().rename(columns={"size": "transaction_count"})

                merchant_count = merchant_count.sort_values(
                    "transaction_count",
                    ascending=False
                ).head(15)

                fig_merchant_count = px.bar(
                    merchant_count,
                    x="description",
                    y="transaction_count",
                    text="transaction_count",
                    title=f"Top 15 Merchants by Transaction Count - {selected_month}"
                )
                st.plotly_chart(style_bar_chart(fig_merchant_count), use_container_width=True)

                st.dataframe(month_debits, use_container_width=True)

                st.subheader("Credits / Refunds")
                month_credits = credit_transactions[
                    credit_transactions["month"] == selected_month
                ].copy()
                st.dataframe(month_credits, use_container_width=True)

            else:
                st.info("No transaction data found yet.")

        with expense_insights_tab:
            if not debit_transactions.empty:
                available_months = sorted(debit_transactions["month"].dropna().unique(), reverse=True)

                selected_month = st.selectbox(
                    "Select month for insights",
                    available_months,
                    key="expense_insights_month"
                )

                latest_data = debit_transactions[
                    debit_transactions["month"] == selected_month
                ].copy()

                latest_total = latest_data["amount"].sum()
                latest_count = len(latest_data)
                latest_avg = latest_data["amount"].mean()
                latest_largest = latest_data.loc[latest_data["amount"].idxmax()]

                latest_top_category = latest_data.groupby(
                    "category",
                    as_index=False
                )["amount"].sum().sort_values("amount", ascending=False).iloc[0]

                latest_top_merchant = latest_data.groupby(
                    "description",
                    as_index=False
                )["amount"].sum().sort_values("amount", ascending=False).iloc[0]

                highest_day = latest_data.groupby(
                    "date",
                    as_index=False
                )["amount"].sum().sort_values("amount", ascending=False).iloc[0]

                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Total Spend", f"AED {latest_total:,.0f}")
                col2.metric("Transactions", f"{latest_count}")
                col3.metric("Avg Transaction", f"AED {latest_avg:,.0f}")
                col4.metric("Largest Txn", f"AED {latest_largest['amount']:,.0f}")

                st.divider()

                insight_card(
                    "Top Category",
                    f"{latest_top_category['category']} drove AED {latest_top_category['amount']:,.0f} of spend."
                )

                insight_card(
                    "Top Merchant",
                    f"{latest_top_merchant['description']} was the highest merchant at AED {latest_top_merchant['amount']:,.0f}."
                )

                insight_card(
                    "Highest Spend Day",
                    f"{highest_day['date'].date()} had the highest daily spend at AED {highest_day['amount']:,.0f}."
                )

                monthly_expenses = debit_transactions.groupby(
                    "month",
                    as_index=False
                )["amount"].sum().sort_values("month")

                selected_index = monthly_expenses[
                    monthly_expenses["month"] == selected_month
                ].index

                if len(selected_index) > 0 and selected_index[0] > 0:
                    idx = selected_index[0]
                    current = monthly_expenses.iloc[idx]
                    previous = monthly_expenses.iloc[idx - 1]

                    insight_card(
                        "Month-over-Month",
                        insight_arrow(current["amount"], previous["amount"], higher_is_better=False)
                    )
                else:
                    st.info("Add another monthly expense file to see month-over-month insights.")

            else:
                st.info("No monthly transaction files found yet.")


# -----------------------------
# PLANNING
# -----------------------------
if page == "Planning":
    section_header(
        "🗓️ Financial Planning",
        "Scenario planning, future portfolio views, lifestyle costs, and calculation engine."
    )

    if planning is None:
        st.info(f"Add your financial planning Excel file to {DATA_FOLDER}.")
        st.code("""
Venkat_FinancialPlanning.xlsx
        """)
    else:
        scenarios_df = planning.get("scenarios", pd.DataFrame()).copy()
        living_costs_df = planning.get("living_costs", pd.DataFrame()).copy()
        savings_only_df = planning.get("savings_only", pd.DataFrame()).copy()
        calc_main_df = planning.get("calc_main", pd.DataFrame()).copy()
        calc_assets_df = planning.get("calc_assets", pd.DataFrame()).copy()
        calc_summary_df = planning.get("calc_summary", pd.DataFrame()).copy()

        st.markdown(
            f"""
            <span class="success-pill">
                🗂️ Planning file loaded: {planning.get('source_file', 'Financial Planning Workbook')}
            </span>
            """,
            unsafe_allow_html=True
        )

        scenario_tab, calculation_tab = st.tabs([
            "🌍 Scenarios",
            "🧮 Calculations"
        ])

        with scenario_tab:
            if scenarios_df.empty:
                st.warning("Planning file found, but no scenario rows could be parsed.")
            else:
                period_order = {
                    "current": 0,
                    "one year": 1,
                    "1 year": 1,
                    "2 years": 2,
                    "3 years": 3,
                    "4 years - until 2030": 4,
                    "4 years": 4
                }

                scenarios_df["period_sort"] = (
                    scenarios_df["period"]
                    .astype(str)
                    .str.lower()
                    .map(period_order)
                    .fillna(99)
                )
                scenarios_df = scenarios_df.sort_values(["scenario", "period_sort"])

                scenario_names = list(scenarios_df["scenario"].dropna().unique())

                selected_planning_scenario = st.selectbox(
                    "Select scenario",
                    ["All Scenarios"] + scenario_names,
                    index=0,
                    key="planning_scenario_filter"
                )

                if selected_planning_scenario == "All Scenarios":
                    filtered_scenarios = scenarios_df.copy()
                    filtered_living_costs = living_costs_df.copy()
                    selected_label = "All Scenarios"
                else:
                    filtered_scenarios = scenarios_df[
                        scenarios_df["scenario"] == selected_planning_scenario
                    ].copy()
                    filtered_living_costs = living_costs_df[
                        living_costs_df["scenario"] == selected_planning_scenario
                    ].copy() if not living_costs_df.empty and "scenario" in living_costs_df.columns else living_costs_df.copy()
                    selected_label = selected_planning_scenario

                latest_scenarios = (
                    filtered_scenarios
                    .sort_values(["scenario", "period_sort"])
                    .groupby("scenario", as_index=False)
                    .tail(1)
                )

                current_scenarios = filtered_scenarios[
                    filtered_scenarios["period"].astype(str).str.lower() == "current"
                ].copy()

                st.markdown("### Scenario Snapshot")

                if selected_planning_scenario == "All Scenarios":
                    best_row = latest_scenarios.sort_values("total_value", ascending=False).iloc[0]
                    highest_income_row = latest_scenarios.sort_values("monthly_avg_12", ascending=False).iloc[0]
                    total_current_value = current_scenarios["total_value"].sum() if not current_scenarios.empty else 0
                    scenario_count = filtered_scenarios["scenario"].nunique()
                    final_period = str(best_row["period"])

                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("View", "All Scenarios")
                    c2.metric("Scenarios", f"{scenario_count}")
                    c3.metric("Best 4-Year Value", money_text(best_row["total_value"]))
                    c4.metric("Best Monthly @ 12%", money_text(highest_income_row["monthly_avg_12"]))
                    c5.metric("Planning Horizon", final_period)
                else:
                    scenario_data = filtered_scenarios.sort_values("period_sort").copy()
                    current_row = scenario_data.iloc[0]
                    final_row = scenario_data.iloc[-1]
                    portfolio_growth = final_row["total_value"] - current_row["total_value"]

                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("Current Portfolio", money_text(current_row["total_value"]))
                    c2.metric("4-Year Portfolio", money_text(final_row["total_value"]))
                    c3.metric("Portfolio Growth", money_text(portfolio_growth))
                    c4.metric("Monthly @ 12%", money_text(final_row["monthly_avg_12"]))
                    c5.metric("Monthly @ 20%", money_text(final_row["monthly_best_20"]))

                st.divider()

                chart_df = filtered_scenarios.copy()
                chart_df["period_label"] = chart_df["period"].astype(str)

                fig_portfolio_scenarios = px.line(
                    chart_df,
                    x="period_label",
                    y="total_value",
                    color="scenario",
                    markers=True,
                    title=f"Scenario Portfolio Value Growth - {selected_label}",
                    text="total_value"
                )
                fig_portfolio_scenarios.update_traces(
                    texttemplate="AED %{y:,.0f}",
                    textposition="top center"
                )
                st.plotly_chart(style_chart_base(fig_portfolio_scenarios), use_container_width=True)

                st.markdown("### Monthly Lifestyle Cost")
                if not filtered_living_costs.empty:
                    living_display = filtered_living_costs.copy()
                    st.dataframe(
                        living_display,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "scenario": "Scenario",
                            "cost_item": "Cost Item",
                            "monthly_amount": st.column_config.NumberColumn("Monthly Amount", format="AED %.0f")
                        }
                    )
                else:
                    st.info("No monthly lifestyle cost data found for the selected scenario.")

                st.markdown("### Scenario Table")
                scenario_display = filtered_scenarios[[
                    "scenario",
                    "period",
                    "total_value",
                    "monthly_low_7",
                    "monthly_avg_12",
                    "monthly_best_20",
                    "experiment_loss_pct",
                    "freedom_cost_pct"
                ]].copy()

                st.dataframe(
                    scenario_display,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "scenario": "Scenario",
                        "period": "Period",
                        "total_value": st.column_config.NumberColumn("Total Value", format="AED %.0f"),
                        "monthly_low_7": st.column_config.NumberColumn("Monthly @ 7%", format="AED %.0f"),
                        "monthly_avg_12": st.column_config.NumberColumn("Monthly @ 12%", format="AED %.0f"),
                        "monthly_best_20": st.column_config.NumberColumn("Monthly @ 20%", format="AED %.0f"),
                        "experiment_loss_pct": st.column_config.NumberColumn("Experiment Loss %", format="%.2f%%"),
                        "freedom_cost_pct": st.column_config.NumberColumn("Freedom Cost %", format="%.2f%%")
                    }
                )

        with calculation_tab:
            st.markdown("### Financial Planning Calculation Engine")

            if not calc_summary_df.empty:
                summary_latest = calc_summary_df.copy()

                summary_melt = summary_latest.melt(
                    id_vars="metric",
                    value_vars=[
                        "with_all_investments",
                        "without_indian_savings",
                        "without_indian_and_real_estate"
                    ],
                    var_name="scenario",
                    value_name="amount"
                )

                summary_melt["scenario"] = summary_melt["scenario"].replace({
                    "with_all_investments": "With All Investments",
                    "without_indian_savings": "Without Indian Savings",
                    "without_indian_and_real_estate": "Without Indian + Real Estate"
                })

                fig_calc_summary = px.bar(
                    summary_melt,
                    x="metric",
                    y="amount",
                    color="scenario",
                    text="amount",
                    barmode="group",
                    title="Capital Build-Up Summary"
                )
                fig_calc_summary.update_traces(texttemplate="AED %{y:,.0f}", textposition="outside")
                st.plotly_chart(style_chart_base(fig_calc_summary), use_container_width=True)

                st.dataframe(
                    calc_summary_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "metric": "Metric",
                        "with_all_investments": st.column_config.NumberColumn("With All Investments", format="AED %.0f"),
                        "without_indian_savings": st.column_config.NumberColumn("Without Indian Savings", format="AED %.0f"),
                        "without_indian_and_real_estate": st.column_config.NumberColumn("Without Indian + Real Estate", format="AED %.0f")
                    }
                )

            st.markdown("### Salary, Bonus and Benefit Build-Up")
            if not calc_main_df.empty:
                st.dataframe(
                    calc_main_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "component": "Component",
                        "2_years_value": st.column_config.NumberColumn("2 Years Value", format="AED %.0f"),
                        "2_years_alt": st.column_config.NumberColumn("2 Years Alt", format="AED %.0f"),
                        "3_years_value": st.column_config.NumberColumn("3 Years Value", format="AED %.0f"),
                        "3_years_alt": st.column_config.NumberColumn("3 Years Alt", format="AED %.0f"),
                        "4_years_value": st.column_config.NumberColumn("4 Years Value", format="AED %.0f"),
                        "4_years_alt": st.column_config.NumberColumn("4 Years Alt", format="AED %.0f")
                    }
                )

            st.markdown("### Asset and Payout Assumptions")
            if not calc_assets_df.empty:
                asset_chart = calc_assets_df.copy().dropna(subset=["amount"])
                asset_chart = asset_chart.sort_values("amount", ascending=False)

                fig_assets = px.bar(
                    asset_chart,
                    x="component",
                    y="amount",
                    text="amount",
                    title="Asset and Payout Assumptions"
                )
                fig_assets.update_traces(texttemplate="AED %{y:,.0f}")
                st.plotly_chart(style_bar_chart(fig_assets), use_container_width=True)

                st.dataframe(
                    calc_assets_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "component": "Component",
                        "amount": st.column_config.NumberColumn("Amount", format="AED %.0f")
                    }
                )

            st.markdown("### Savings Only View")
            if not savings_only_df.empty:
                st.dataframe(
                    savings_only_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "scenario": "Scenario",
                        "period": "Period",
                        "monthly_savings": st.column_config.NumberColumn("Monthly Savings", format="AED %.0f"),
                        "savings_per_day": st.column_config.NumberColumn("Savings Per Day", format="AED %.0f"),
                        "one_month": st.column_config.NumberColumn("1 Month", format="AED %.0f"),
                        "two_months": st.column_config.NumberColumn("2 Months", format="AED %.0f"),
                        "twelve_months": st.column_config.NumberColumn("12 Months", format="AED %.0f")
                    }
                )


# -----------------------------
# INVESTMENTS
# -----------------------------
if page == "Investments":
    section_header(
        "Investments",
        "Portfolio view of savings, real estate, stocks, contributions, and expected growth."
    )

    if investments is None:
        st.info("Add your investment Excel file to C:\\LifeDashboard\\data.")
        st.code("""
Investment Plan.xlsx
        """)
    else:
        if "is_total_row" not in investments.columns:
            investments["is_total_row"] = False

        investment_rows = investments[~investments["is_total_row"]].copy()

        if investment_rows.empty:
            st.warning("Investment file was found, but no investment rows could be parsed. Check the Raw Data tab or confirm the Excel layout.")
            st.dataframe(investments, use_container_width=True)
            st.stop()

        latest_values = (
            investment_rows
            .dropna(subset=["ending_balance"])
            .sort_values(["section_order", "period_sort"])
            .groupby(["investment_name", "investment_type"], as_index=False)
            .tail(1)
        )

        current_portfolio_value = latest_values["ending_balance"].sum()

        starting_values = (
            investment_rows
            .dropna(subset=["starting_balance"])
            .sort_values(["section_order", "period_sort"])
            .groupby(["investment_name", "investment_type"], as_index=False)
            .head(1)
        )

        initial_capital = starting_values["starting_balance"].sum()
        total_contributions = investment_rows["monthly_deposit"].fillna(0).sum()
        total_expected_growth = investment_rows["gain_or_interest"].fillna(0).sum()
        estimated_profit = current_portfolio_value - initial_capital - total_contributions

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Current Portfolio Value", f"AED {current_portfolio_value:,.0f}")
        c2.metric("Initial Capital", f"AED {initial_capital:,.0f}")
        c3.metric("Planned Contributions", f"AED {total_contributions:,.0f}")
        c4.metric("Expected Growth / Interest", f"AED {total_expected_growth:,.0f}")

        c5, c6, c7 = st.columns(3)
        c5.metric("Estimated Net Profit", f"AED {estimated_profit:,.0f}")
        c6.metric("Investment Buckets", f"{latest_values['investment_type'].nunique()}")
        c7.metric("Files Loaded", f"{len(investment_files)}")

        st.divider()

        portfolio_tab, savings_tab, real_estate_tab, stocks_tab, raw_tab = st.tabs([
            "Portfolio Summary",
            "Bank Savings",
            "Real Estate",
            "Stocks",
            "Raw Data"
        ])

        with portfolio_tab:
            portfolio_by_type = latest_values.groupby(
                "investment_type",
                as_index=False
            )["ending_balance"].sum().sort_values("ending_balance", ascending=False)

            portfolio_by_type["value_label"] = (
                "AED "
                + portfolio_by_type["ending_balance"].round(0).astype(int).map("{:,}".format)
            )

            fig_portfolio = px.bar(
                portfolio_by_type,
                x="investment_type",
                y="ending_balance",
                text="value_label",
                title="Current Portfolio Value by Investment Type"
            )
            st.plotly_chart(style_bar_chart(fig_portfolio), use_container_width=True)

            growth_by_type = investment_rows.groupby(
                "investment_type",
                as_index=False
            )["gain_or_interest"].sum().sort_values("gain_or_interest", ascending=False)

            growth_by_type["growth_label"] = (
                "AED "
                + growth_by_type["gain_or_interest"].round(0).astype(int).map("{:,}".format)
            )

            fig_growth = px.bar(
                growth_by_type,
                x="investment_type",
                y="gain_or_interest",
                text="growth_label",
                title="Expected Interest / Growth by Investment Type"
            )
            st.plotly_chart(style_bar_chart(fig_growth), use_container_width=True)

            trend_data = investment_rows[
                investment_rows["investment_type"].isin(["Bank Savings", "Stocks"])
            ].dropna(subset=["period_sort", "ending_balance"]).copy()

            if not trend_data.empty:
                trend_data["ending_label"] = (
                    "AED "
                    + trend_data["ending_balance"].round(0).astype(int).map("{:,}".format)
                )

                fig_trend = px.line(
                    trend_data,
                    x="period_sort",
                    y="ending_balance",
                    color="investment_type",
                    markers=True,
                    text="ending_label",
                    title="Projected Monthly Ending Balance"
                )
                st.plotly_chart(style_line_chart(fig_trend), use_container_width=True)

            st.dataframe(latest_values, use_container_width=True)

        with savings_tab:
            savings_data = investment_rows[
                investment_rows["investment_type"] == "Bank Savings"
            ].copy()

            if savings_data.empty:
                st.info("No bank savings investment data found.")
            else:
                latest_savings = savings_data.dropna(subset=["ending_balance"]).sort_values("period_sort").iloc[-1]
                total_savings_interest = savings_data["gain_or_interest"].sum()
                total_savings_deposit = savings_data["monthly_deposit"].sum()

                c1, c2, c3 = st.columns(3)
                c1.metric("Projected Ending Balance", f"AED {latest_savings['ending_balance']:,.0f}")
                c2.metric("Total Interest", f"AED {total_savings_interest:,.0f}")
                c3.metric("Total Deposits", f"AED {total_savings_deposit:,.0f}")

                savings_data["ending_label"] = (
                    "AED "
                    + savings_data["ending_balance"].round(0).astype(int).map("{:,}".format)
                )

                fig_savings = px.line(
                    savings_data,
                    x="period_sort",
                    y="ending_balance",
                    text="ending_label",
                    markers=True,
                    title="Bank Savings Balance Growth"
                )
                st.plotly_chart(style_line_chart(fig_savings), use_container_width=True)

                savings_data["interest_label"] = savings_data["gain_or_interest"].round(0)

                fig_savings_interest = px.bar(
                    savings_data,
                    x="period_sort",
                    y="gain_or_interest",
                    text="interest_label",
                    title="Monthly Interest Earned"
                )
                st.plotly_chart(style_bar_chart(fig_savings_interest), use_container_width=True)

                st.dataframe(savings_data, use_container_width=True)

        with real_estate_tab:
            real_estate_data = investment_rows[
                investment_rows["investment_type"] == "Real Estate"
            ].copy()

            if real_estate_data.empty:
                st.info("No real estate investment data found.")
            else:
                latest_real_estate = real_estate_data.dropna(subset=["ending_balance"]).sort_values("period_sort").iloc[-1]
                real_estate_growth = real_estate_data["gain_or_interest"].sum()
                real_estate_start = real_estate_data["starting_balance"].sum()

                c1, c2, c3 = st.columns(3)
                c1.metric("Real Estate Value", f"AED {latest_real_estate['ending_balance']:,.0f}")
                c2.metric("Starting Balance", f"AED {real_estate_start:,.0f}")
                c3.metric("Expected Growth", f"AED {real_estate_growth:,.0f}")

                insight_card(
                    "Data Check",
                    "Your real estate row shows AED 247,000 starting balance and AED 74,100 growth. The ending balance in the sheet is AED 675,235. Please confirm if this is intentional."
                )

                real_estate_chart = real_estate_data.copy()
                real_estate_chart["value_label"] = (
                    "AED "
                    + real_estate_chart["ending_balance"].round(0).astype(int).map("{:,}".format)
                )

                fig_real_estate = px.bar(
                    real_estate_chart,
                    x="period_label",
                    y="ending_balance",
                    text="value_label",
                    title="Real Estate Projected Value"
                )
                st.plotly_chart(style_bar_chart(fig_real_estate), use_container_width=True)

                st.dataframe(real_estate_data, use_container_width=True)

        with stocks_tab:
            stocks_data = investment_rows[
                investment_rows["investment_type"] == "Stocks"
            ].copy()

            if stocks_data.empty:
                st.info("No stocks investment data found.")
            else:
                latest_stocks = stocks_data.dropna(subset=["ending_balance"]).sort_values("period_sort").iloc[-1]
                total_stock_growth = stocks_data["gain_or_interest"].sum()
                total_stock_deposit = stocks_data["monthly_deposit"].sum()

                c1, c2, c3 = st.columns(3)
                c1.metric("Projected Stock Value", f"AED {latest_stocks['ending_balance']:,.0f}")
                c2.metric("Expected Stock Growth", f"AED {total_stock_growth:,.0f}")
                c3.metric("Stock Contributions", f"AED {total_stock_deposit:,.0f}")

                stocks_data["ending_label"] = (
                    "AED "
                    + stocks_data["ending_balance"].round(0).astype(int).map("{:,}".format)
                )

                fig_stocks = px.line(
                    stocks_data,
                    x="period_sort",
                    y="ending_balance",
                    text="ending_label",
                    markers=True,
                    title="Stocks Balance Growth"
                )
                st.plotly_chart(style_line_chart(fig_stocks), use_container_width=True)

                stocks_data["growth_label"] = stocks_data["gain_or_interest"].round(0)

                fig_stock_growth = px.bar(
                    stocks_data,
                    x="period_sort",
                    y="gain_or_interest",
                    text="growth_label",
                    title="Monthly Stock Growth Earned"
                )
                st.plotly_chart(style_bar_chart(fig_stock_growth), use_container_width=True)

                st.dataframe(stocks_data, use_container_width=True)

        with raw_tab:
            st.markdown("### Loaded Investment Files")
            for file in investment_files:
                st.write(file.name)

            st.markdown("### Parsed Investment Data")
            st.dataframe(investments, use_container_width=True)
