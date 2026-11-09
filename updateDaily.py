"""
updateDaily.py

Automated daily resume refresher for Naukri.com.
Downloads your latest resume from Google Drive and updates your Naukri profile
to keep your profile fresh and highly ranked in recruiter searches.
"""

import os
from io import BytesIO
from datetime import datetime
import requests
from dotenv import load_dotenv
from colorama import Fore, Style, init

from src.client.naukri_client import NaukriLoginClient
from src.exceptions.exceptions import NaukriAuthError, NaukriUploadError

load_dotenv()
init(autoreset=True)

# Google Drive File ID (loaded from .env or default)
DEFAULT_FILE_ID = os.getenv("RESUME_DRIVE_ID", "1S8gzcK5pilmAAjUTTAlp2l3RV4tumgv8")


def download_resume_from_drive(file_id: str, save_path: str = "resume.pdf") -> str:
    """Downloads resume PDF from Google Drive and saves it locally."""
    drive_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    print(f"{Fore.CYAN}Downloading resume from Google Drive (ID: {file_id})...{Style.RESET_ALL}")
    
    response = requests.get(drive_url, timeout=30)
    response.raise_for_status()

    if response.content[:4] != b"%PDF":
        raise ValueError("Downloaded file is not a valid PDF document.")

    with open(save_path, "wb") as f:
        f.write(response.content)

    print(f"{Fore.GREEN}✔ Downloaded successfully ({len(response.content):,} bytes) -> {save_path}{Style.RESET_ALL}")
    return save_path


def update_resume_daily(file_id: str = None) -> dict:
    """Logs in and updates the resume on Naukri profile."""
    username = os.getenv("USERNAME")
    password = os.getenv("PASSWORD")
    file_id = file_id or DEFAULT_FILE_ID

    if not username or not password:
        raise ValueError("USERNAME and PASSWORD must be set in .env")

    print(f"\n{Fore.CYAN}{'═' * 55}{Style.RESET_ALL}")
    print(f"{Fore.YELLOW}{Style.BRIGHT}  Naukri Daily Resume Updater{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'═' * 55}{Style.RESET_ALL}")

    # 1. Download latest resume
    today_str = datetime.now().strftime("%d_%b_%Y").lower()
    local_resume = f"resume_{today_str}.pdf"
    download_resume_from_drive(file_id, local_resume)

    # 2. Login to Naukri
    print(f"{Fore.CYAN}Logging in to Naukri as {username}...{Style.RESET_ALL}")
    client = NaukriLoginClient(username, password)
    client.login()
    print(f"{Fore.GREEN}✔ Login successful! Profile ID: {client.profile_id}{Style.RESET_ALL}")

    # 3. Upload & update resume
    print(f"{Fore.CYAN}Uploading and updating resume on Naukri...{Style.RESET_ALL}")
    result = client.update_resume(local_resume)

    if result.status_code in (200, 201):
        print(f"{Fore.GREEN}{Style.BRIGHT}✔ Resume updated successfully! (Status: {result.status_code}){Style.RESET_ALL}")
    else:
        print(f"{Fore.YELLOW}Resume update returned response: {result.raw_response}{Style.RESET_ALL}")

    return {
        "success": True,
        "profile_id": result.profile_id,
        "status_code": result.status_code,
        "response": result.raw_response,
    }


def handler(event=None, context=None):
    """Entry point for serverless/cron workers (e.g., AWS Lambda)."""
    return update_resume_daily()


if __name__ == "__main__":
    try:
        res = update_resume_daily()
    except Exception as e:
        print(f"{Fore.RED}✘ Error updating resume: {e}{Style.RESET_ALL}")
