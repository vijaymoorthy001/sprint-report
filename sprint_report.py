# -*- coding: utf-8 -*-
# Disable specific pylint warnings that might be noisy in this context
# pylint: disable=line-too-long, unspecified-encoding, invalid-name, too-many-lines, too-many-locals, too-many-statements
import requests
import base64
import gspread
# Use google.oauth2.service_account for credentials from dict/file
# oauth2client is deprecated for service accounts in newer gspread versions
from google.oauth2.service_account import Credentials
from collections import defaultdict, Counter
import datetime
import math
import sys
import pandas as pd
from dateutil import parser as date_parser # More flexible date parsing
import time
import json
import logging # Use logging for better output control
import concurrent.futures
from collections import defaultdict

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger("requests").setLevel(logging.WARNING) # Silence excessive requests logging
logging.getLogger("urllib3").setLevel(logging.WARNING) # Silence excessive urllib3 logging


# --- CONFIGURATION ---
# Jira Configuration
JIRA_SERVER = "" # Include https://
JIRA_EMAIL = "" # The email used for the API Token
# IMPORTANT: Use the Base64 encoded token directly for the header
JIRA_AUTH_TOKEN_BASE64 = "" # This IS the Base64 encoded value "email:api_token"

# --- !!! IMPORTANT: Board ID !!! ---
JIRA_BOARD_ID = 3802  # <<< REPLACE WITH YOUR BOARD ID if different

# Sprint Configuration
SPRINT_NAME = "" # Exact name

# --- !!! IMPORTANT: Custom Field Name !!! ---
EPIC_DESIGNATION_FIELD_NAME = "" # <<< ADJUST THIS CAREFULLY

JIRA_TICKETS = {} # Cache for Jira ticket details fetched via worklogs

# Tempo Configuration
# TEMPO_SERVER API base URL (e.g., for Cloud API v4)
TEMPO_SERVER = "https://api.tempo.io/4" # <<< VERIFY this is correct for your Tempo Cloud instance
TEMPO_API_TOKEN = "" # <<< Your Tempo API Bearer Token

# Google Sheets Configuration
GOOGLE_SHEET_KEY = "" 

# Using the credentials dictionary directly
GSHEET_CREDENTIALS_DICT = {}

# API Request Settings
REQUEST_TIMEOUT = 45
MAX_RETRIES = 2
RETRY_DELAY = 3

# Status Column Configuration
PRIMARY_STATUS_ORDER = ['To Do', 'In Progress', 'Merge', 'QA', 'Deploy'] # Base statuses for ordering
COMBINED_DONE_STATUSES = ['Done', 'Resolved'] # Statuses to sum into '% Done'
STATUS_COLUMN_COLORS = { # Hex colors for specific status columns in the '%' table
    '% To Do': '#d8d8d8',
    '% In Progress': '#fff2cc',
    '% Merge': '#cfe2f3',
    '% QA': '#cfe2f3',
    '% Deploy': '#cfe2f3',
    '% Done': '#d9ead3' # Color for the combined Done/Resolved column
}
# --- END CONFIGURATION ---


# --- Authentication Headers ---
def get_jira_headers():
    """Creates Jira authentication headers."""
    return {
        "Authorization": f"Basic {JIRA_AUTH_TOKEN_BASE64}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

def get_tempo_headers():
    """Creates Tempo authentication headers."""
    return {
        "Authorization": f"Bearer {TEMPO_API_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

# --- Google Sheets Setup ---
SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
          'https://www.googleapis.com/auth/drive']
try:
    creds = Credentials.from_service_account_info(GSHEET_CREDENTIALS_DICT, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_KEY)
    logging.info(f"Successfully connected to Google Sheet: {sh.title}")
except Exception as e:
    logging.error(f"Error connecting to Google Sheets: {e}", exc_info=True)
    sys.exit(1)

# --- Helper Functions ---
def make_request(method, url, headers, params=None, json_payload=None, retries=MAX_RETRIES, is_tempo=False):
    """Makes an HTTP request with retries and error handling."""
    for attempt in range(retries + 1):
        try:
            logging.debug(f"Requesting ({attempt+1}): {method} {url} Params: {params} Payload: {json_payload}")
            response = requests.request(
                method, url, headers=headers, params=params, json=json_payload, timeout=REQUEST_TIMEOUT
            )
            logging.debug(f"Response Status: {response.status_code}")
            response.raise_for_status()

            if is_tempo and response.status_code == 200:
                 try:
                      data = response.json()
                      if isinstance(data, dict) and ('errorMessages' in data or 'errors' in data or 'message' in data):
                           logging.warning(f"Tempo API returned 200 but with message: {data}")
                 except json.JSONDecodeError: pass # Ignore if not JSON

            return response.json()
        except requests.exceptions.HTTPError as e:
            logging.warning(f"HTTP Error ({attempt + 1}/{retries + 1}): {method} {url} - Status: {e.response.status_code} - Response: {e.response.text[:500]}")
            if e.response.status_code in [401, 403]: return None # Don't retry auth/permission errors
        except requests.exceptions.RequestException as e:
            logging.warning(f"Request Exception ({attempt + 1}/{retries + 1}): {method} {url} - {e}")

        if attempt < retries: time.sleep(RETRY_DELAY)
        else: logging.error(f"Final request attempt failed for {method} {url}.")
    return None

def seconds_to_hours(seconds):
    """Converts seconds to hours, returns 0 if input is None or invalid."""
    if not isinstance(seconds, (int, float)) or seconds < 0: return 0.0
    return round(seconds / 3600.0, 2)

def find_custom_field_id(field_name, jira_url, headers):
    """Finds the custom field ID for a given display name."""
    logging.info(f"Attempting to find custom field ID for: '{field_name}'")
    url = f"{jira_url}/rest/api/3/field"
    fields_data = make_request("GET", url, headers=headers)
    if fields_data:
        for field in fields_data:
            if isinstance(field, dict) and field.get('name') == field_name and field.get('custom', False):
                field_id = field['id']
                logging.info(f"Found custom field ID: {field_id} for '{field_name}'")
                return field_id
    logging.error(f"Custom field ID not found for '{field_name}'. Check the exact name in Jira.")
    return field_name # Fallback

def create_hyperlink_formula(url, link_text):
    """Creates a Google Sheets HYPERLINK formula string."""
    escaped_url = url.replace('"', '""')
    escaped_text = link_text.replace('"', '""')
    return f'=HYPERLINK("{escaped_url}", "{escaped_text}")'

def get_jira_api_data(endpoint, headers, params=None):
    """Simple wrapper for GET requests to Jira API."""
    try:
        response = requests.get(endpoint, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching data from Jira API ({endpoint}): {e}")
        return None

def fetch_jira_ticket_details_concurrently(issue_ids_to_fetch, jira_api_url, headers):
    """Fetches full details for Jira issue IDs concurrently."""
    global JIRA_TICKETS
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        # Only fetch IDs that are not already cached (as success or failure)
        ids_to_actually_fetch = [issue_id for issue_id in issue_ids_to_fetch if issue_id not in JIRA_TICKETS]
        if not ids_to_actually_fetch:
            logging.debug("No new issue IDs require fetching details.")
            return

        logging.debug(f"Fetching details for {len(ids_to_actually_fetch)} new issue IDs.")
        future_to_issue_id = {executor.submit(get_jira_api_data, f"{jira_api_url}/rest/api/3/issue/{issue_id}", headers): issue_id for issue_id in ids_to_actually_fetch}

        for future in concurrent.futures.as_completed(future_to_issue_id):
            issue_id = future_to_issue_id[future]
            try:
                data = future.result()
                if data and 'key' in data:
                    JIRA_TICKETS[issue_id] = data # Cache success
                    logging.debug(f"Successfully fetched details for issue ID {issue_id} (Key: {data['key']})")
                else:
                    JIRA_TICKETS[issue_id] = None # Cache failure (e.g., 404, permission error)
                    logging.warning(f"Failed to fetch valid details for issue ID {issue_id}. Caching as None.")
            except Exception as exc:
                logging.error(f"Error fetching details for issue ID {issue_id}: {exc}")
                JIRA_TICKETS[issue_id] = None # Cache exception case

def format_date(date_str, default="N/A"):
    """Parses ISO date string and returns YYYY-MM-DD, or default."""
    if not date_str: return default
    try: return date_parser.isoparse(date_str).strftime('%Y-%m-%d')
    except (ValueError, TypeError): return default

def calculate_days_remaining(end_date_str, default="N/A"):
    """Calculates days remaining until the end date."""
    if not end_date_str: return default
    try:
        end_date = date_parser.isoparse(end_date_str).date()
        today = datetime.date.today()
        return max(0, (end_date - today).days)
    except (ValueError, TypeError): return default

def hex_to_rgb_dict(hex_color):
    """Converts hex color string (e.g., #ffffff) to gspread RGB dict."""
    hex_color = hex_color.lstrip('#')
    lv = len(hex_color)
    if lv != 6: return None # Invalid hex
    try:
        return {
            "red": int(hex_color[0:2], 16) / 255.0,
            "green": int(hex_color[2:4], 16) / 255.0,
            "blue": int(hex_color[4:6], 16) / 255.0
        }
    except ValueError:
        return None # Invalid hex characters

# --- Main Script Logic ---

JIRA_HEADERS = get_jira_headers()
TEMPO_HEADERS = get_tempo_headers()

# 0. Find Custom Field ID
cf_epic_designation_id = find_custom_field_id(EPIC_DESIGNATION_FIELD_NAME, JIRA_SERVER, JIRA_HEADERS)
if not cf_epic_designation_id or not cf_epic_designation_id.startswith("customfield_"):
     logging.warning(f"Could not reliably find custom field ID for '{EPIC_DESIGNATION_FIELD_NAME}'. Will use field name as fallback.")
     # Keep cf_epic_designation_id as the name for potential use later, but log the issue.

# 1. Find the Sprint
logging.info(f"Searching for Sprint '{SPRINT_NAME}' on board ID {JIRA_BOARD_ID}...")
target_sprint = None
sprint_url = f"{JIRA_SERVER}/rest/agile/1.0/board/{JIRA_BOARD_ID}/sprint"
sprint_params = {'state': 'active,future,closed'}
start_at_sprint = 0
max_results_sprint = 50

while not target_sprint:
    sprint_params['startAt'] = start_at_sprint
    sprint_params['maxResults'] = max_results_sprint
    sprint_data = make_request("GET", sprint_url, params=sprint_params, headers=JIRA_HEADERS)
    if not sprint_data or 'values' not in sprint_data: sys.exit(f"Could not retrieve sprints from board {JIRA_BOARD_ID}.")

    values = sprint_data.get('values', [])
    for sprint in values:
        if isinstance(sprint, dict) and sprint.get('name') == SPRINT_NAME:
            target_sprint = sprint
            break
    if target_sprint: break

    if sprint_data.get('isLast', True): break
    start_at_sprint += len(values)
    if start_at_sprint > 2000: # Safety break for very large boards
        logging.error("Exceeded maximum sprint search limit (2000). Sprint not found.")
        break

if not target_sprint: sys.exit(f"Sprint '{SPRINT_NAME}' not found on board {JIRA_BOARD_ID}.")

sprint_id = target_sprint['id']
sprint_name_actual = target_sprint.get('name', SPRINT_NAME)
sprint_start_date_str = target_sprint.get('startDate')
sprint_end_date_str = target_sprint.get('endDate')
sprint_complete_date_str = target_sprint.get('completeDate') # Date sprint was actually closed

sprint_start_date_fmt = format_date(sprint_start_date_str)
sprint_end_date_fmt = format_date(sprint_end_date_str)
days_remaining = calculate_days_remaining(sprint_end_date_str)
current_timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# Determine date range for Tempo query (use actual completion date if closed)
sprint_start_date_tempo, sprint_end_date_tempo = None, None
try:
    if sprint_start_date_str: sprint_start_date_tempo = date_parser.isoparse(sprint_start_date_str).strftime('%Y-%m-%d')
    # Use completion date if available and sprint is closed, otherwise use end date or today
    effective_end_date_str = sprint_complete_date_str if target_sprint.get('state') == 'closed' and sprint_complete_date_str else sprint_end_date_str
    if effective_end_date_str:
        sprint_end_date_tempo = date_parser.isoparse(effective_end_date_str).strftime('%Y-%m-%d')
    elif target_sprint.get('state') != 'closed': # If active/future, use today as end date for logs so far
        sprint_end_date_tempo = datetime.date.today().strftime('%Y-%m-%d')
    # Fallback to original end date if it exists (e.g., future sprint with no logs yet)
    elif sprint_end_date_str:
         sprint_end_date_tempo = date_parser.isoparse(sprint_end_date_str).strftime('%Y-%m-%d')

except Exception as e: logging.warning(f"Could not parse sprint dates for Tempo query: {e}")

logging.info(f"Found Sprint: '{sprint_name_actual}' (ID: {sprint_id})")
logging.info(f"Dates - Start: {sprint_start_date_fmt}, End: {sprint_end_date_fmt}, Days Remaining: {days_remaining}")
if sprint_start_date_tempo and sprint_end_date_tempo:
    logging.info(f"Using date range for Tempo: {sprint_start_date_tempo} to {sprint_end_date_tempo}")
else:
    logging.warning("Could not determine a valid date range for Tempo query. Tempo logs will likely be skipped.")


# 2. Get Sprint Issues
logging.info(f"Fetching tickets for sprint ID {sprint_id}...")
sprint_issues_raw = []
issues_url = f"{JIRA_SERVER}/rest/agile/1.0/sprint/{sprint_id}/issue"
# Ensure cf_epic_designation_id is included even if it's just the name fallback
fields_to_request = ["summary", "status", "assignee", "parent", "timetracking", "reporter", "description", cf_epic_designation_id]
# Filter out None or empty strings just in case
fields_param = ','.join(filter(None, fields_to_request))
issue_params = {'fields': fields_param, 'startAt': 0, 'maxResults': 100} # Max results per page

while True:
    issues_data = make_request("GET", issues_url, params=issue_params, headers=JIRA_HEADERS)
    # Handle potential errors or empty responses
    if not isinstance(issues_data, dict):
        logging.error(f"Failed to fetch issues batch starting at {issue_params['startAt']}. Aborting issue fetch.")
        if issue_params['startAt'] == 0: sys.exit("Could not fetch initial batch of issues.")
        break # Stop if error occurs on subsequent pages

    current_issues = issues_data.get('issues', [])
    if not isinstance(current_issues, list):
        logging.warning(f"Received non-list 'issues' field at startAt {issue_params['startAt']}. Stopping issue fetch.")
        break

    sprint_issues_raw.extend(current_issues)
    logging.info(f"Fetched {len(current_issues)} issues... (Total: {len(sprint_issues_raw)})")

    # Check pagination
    total_available = issues_data.get('total', 0) # Total issues Jira thinks are in the sprint
    current_count = len(sprint_issues_raw)
    is_last_page = issues_data.get('isLast', False) # Check if Jira indicates this is the last page

    # Stop conditions: No more issues fetched OR current count reaches total OR isLast flag is true
    if not current_issues or current_count >= total_available or is_last_page:
        break
    else:
        issue_params['startAt'] = current_count # Set start for next page
    time.sleep(0.1) # Small delay between pages

logging.info(f"Total issues fetched from Jira for sprint: {len(sprint_issues_raw)}")

# --- Data Processing ---
processed_tickets = []
parent_keys_to_fetch = set()
assignee_account_ids = set() # Store account IDs found in sprint issues
accountid_to_displayname = {} # Map account IDs to display names
ticket_summaries = {}
sprint_statuses = set() # Track all unique statuses encountered
sprint_issue_keys = set() # Keep track of keys belonging to this sprint

logging.info("Processing ticket data...")
for issue in sprint_issues_raw:
    if not isinstance(issue, dict): continue
    issue_key = issue.get('key')
    if not issue_key: continue
    sprint_issue_keys.add(issue_key) # Add key to our set of sprint issues

    fields = issue.get('fields', {})
    if not isinstance(fields, dict): continue

    summary = fields.get('summary', 'No Summary')
    ticket_summaries[issue_key] = summary
    description = fields.get('description') # Keep raw description (might be ADF)

    # Assignee Info
    assignee_data = fields.get('assignee')
    assignee_name = "Unassigned"
    assignee_account_id = None
    if isinstance(assignee_data, dict):
        assignee_name = assignee_data.get('displayName', 'Unknown Assignee')
        assignee_account_id = assignee_data.get('accountId')
        # Validate accountId format slightly (basic check)
        if assignee_account_id and isinstance(assignee_account_id, str) and len(assignee_account_id) > 10:
            assignee_account_ids.add(assignee_account_id)
            # Store mapping if we have both ID and name
            if assignee_name != 'Unknown Assignee':
                accountid_to_displayname[assignee_account_id] = assignee_name
        else: assignee_account_id = None # Reset if invalid format

    # Reporter Info
    reporter_data = fields.get('reporter')
    reporter_name = reporter_data.get('displayName', 'Unknown Reporter') if isinstance(reporter_data, dict) else "Unknown Reporter"
    # Optionally capture reporter account ID if needed later
    # reporter_account_id = reporter_data.get('accountId') if isinstance(reporter_data, dict) else None

    # Status Info
    status_data = fields.get('status', {})
    status_name = status_data.get('name', 'Unknown Status') if isinstance(status_data, dict) else 'Unknown Status'
    sprint_statuses.add(status_name)

    # Time Tracking Info
    timetracking_data = fields.get('timetracking', {})
    original_estimate_seconds = timetracking_data.get('originalEstimateSeconds', 0) if isinstance(timetracking_data, dict) else 0
    time_spent_seconds = timetracking_data.get('timeSpentSeconds', 0) if isinstance(timetracking_data, dict) else 0
    original_estimate_hours = seconds_to_hours(original_estimate_seconds)
    time_spent_hours_jira = seconds_to_hours(time_spent_seconds)

    # Parent Info (for Epics)
    parent_data = fields.get('parent')
    parent_key = parent_data.get('key') if isinstance(parent_data, dict) else None
    if parent_key: parent_keys_to_fetch.add(parent_key)

    # Store processed data
    processed_tickets.append({
        'key': issue_key, 'summary': summary, 'description': description,
        'status': status_name, 'assignee': assignee_name,
        'assignee_account_id': assignee_account_id, 'reporter': reporter_name,
        'parent_key': parent_key, 'original_estimate_hours': original_estimate_hours,
        'time_spent_hours_jira': time_spent_hours_jira, 'epic_designation': "N/A", # Default
        'tempo_logged_hours_total': 0.0 # Default, filled later
    })

# Keep the full list of statuses found for overall summary and headers
all_sprint_statuses_sorted = sorted(list(sprint_statuses))

# 3. Fetch Parent Epic Designations
parent_epic_designations = {}
all_epic_designations = set() # Track unique designation values found
if parent_keys_to_fetch:
    logging.info(f"Fetching details for {len(parent_keys_to_fetch)} unique parent issues (potential Epics)...")
    parent_issue_url_base = f"{JIRA_SERVER}/rest/api/3/issue"
    # Request only summary and the specific custom field
    parent_fields = f"summary,{cf_epic_designation_id}"

    # Use concurrent fetching for parents as well
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_to_parent_key = {
            executor.submit(make_request, "GET", f"{parent_issue_url_base}/{key}", params={'fields': parent_fields}, headers=JIRA_HEADERS, retries=1): key
            for key in parent_keys_to_fetch
        }
        for future in concurrent.futures.as_completed(future_to_parent_key):
            key = future_to_parent_key[future]
            try:
                parent_data = future.result()
                designation = "Parent Not Found/No Access" # Default if fetch fails or no data
                if isinstance(parent_data, dict) and 'fields' in parent_data:
                    parent_fields_data = parent_data.get('fields', {})
                    # Handle various potential formats for custom fields (single select, multi, text, etc.)
                    designation_value = parent_fields_data.get(cf_epic_designation_id)
                    if designation_value:
                         if isinstance(designation_value, dict) and 'value' in designation_value: # Standard select list
                             designation = str(designation_value['value'])
                         elif isinstance(designation_value, list): # Multi-select or similar
                             designation = ', '.join(str(item['value']) for item in designation_value if isinstance(item, dict) and 'value' in item) or "Not Set"
                         elif isinstance(designation_value, (str, int, float)): # Simple text or number field
                             designation = str(designation_value)
                         elif designation_value is None: # Field exists but is empty
                             designation = "Not Set"
                         else: # Unexpected format
                             designation = "Unknown Format"
                             logging.debug(f"Unknown format for Epic Designation field on {key}: {designation_value}")
                         # Add valid designations to our set
                         if designation not in ["Not Set", "N/A", "Unknown Format", "Parent Not Found/No Access"]:
                             all_epic_designations.add(designation)
                    else: # Field doesn't exist on parent or wasn't returned
                        designation = "Not Set"
                parent_epic_designations[key] = designation
            except Exception as exc:
                logging.error(f"Error fetching parent details for key {key}: {exc}")
                parent_epic_designations[key] = "Error Fetching Parent" # Indicate error

logging.info("Mapping Epic Designations to tickets...")
for ticket in processed_tickets:
    parent_key = ticket.get('parent_key')
    if parent_key in parent_epic_designations:
        designation = parent_epic_designations[parent_key]
        ticket['epic_designation'] = designation
        # Add to set again here just to be safe, though should be covered above
        if designation not in ["N/A", "Not Set", "Parent Not Found/No Access", "Unknown Format", "Error Fetching Parent"]:
             all_epic_designations.add(designation)

# 4. Get Tempo Worklogs (Filtered by Sprint Tickets)
logging.info("Fetching Tempo worklogs...")
tempo_time_per_ticket_user = defaultdict(lambda: defaultdict(float)) # Stores {issue_key: {account_id: total_hours}}
tempo_time_per_ticket_total = defaultdict(float) # Stores {issue_key: total_hours}
issue_ids_from_tempo_logs = set() # Track Jira issue IDs found in Tempo logs
tempo_contributors_per_ticket = defaultdict(set) # NEW: Stores {issue_key: {account_id, account_id,...}}

# Only proceed if we have assignees AND a valid date range
if assignee_account_ids and sprint_start_date_tempo and sprint_end_date_tempo:
    all_tempo_logs = []
    processed_log_ids = set() # Avoid double-counting logs if fetched multiple times

    # Fetch logs per assignee found in the sprint issues
    # Consider fetching for *all* users if needed, but per-assignee is usually sufficient
    logging.info(f"Fetching Tempo logs for {len(assignee_account_ids)} unique assignees found in sprint tickets...")
    for assignee_id in assignee_account_ids:
        display_name_for_log = accountid_to_displayname.get(assignee_id, assignee_id) # Use name if available
        logging.info(f"Fetching Tempo logs for assignee: {display_name_for_log}")
        limit = 100000 # Max results per Tempo page (adjust if needed, 1000 is common)
        tempo_search_url = f"{TEMPO_SERVER}/worklogs/search?limit={limit}" # POST endpoint for search
        current_offset = 0

        while True:
            tempo_payload = {
                "authorIds": [assignee_id], # Search by author
                "from": sprint_start_date_tempo,
                "to": sprint_end_date_tempo,
                "limit": limit
            }
            response_data = make_request("POST", tempo_search_url, headers=TEMPO_HEADERS, json_payload=tempo_payload, is_tempo=True)

            if not isinstance(response_data, dict) or 'results' not in response_data:
                logging.warning(f"Invalid or empty response from Tempo search for assignee {display_name_for_log} at offset {current_offset}. Stopping fetch for this assignee.")
                break

            current_logs = response_data.get('results', [])
            if not isinstance(current_logs, list):
                 logging.warning(f"Tempo search 'results' field is not a list for assignee {display_name_for_log} at offset {current_offset}. Stopping fetch for this assignee.")
                 break

            new_logs_count = 0
            for log in current_logs:
                 log_id = log.get('tempoWorklogId')
                 # Skip if already processed (e.g., if API returns duplicates across pages)
                 if log_id and log_id in processed_log_ids: continue
                 if log_id: processed_log_ids.add(log_id)

                 all_tempo_logs.append(log)
                 new_logs_count += 1
                 # Extract Jira issue ID from the log
                 issue_data = log.get('issue')
                 if isinstance(issue_data, dict) and 'id' in issue_data:
                     issue_ids_from_tempo_logs.add(str(issue_data['id']))

            logging.info(f"Fetched {new_logs_count} new Tempo logs for {display_name_for_log} (offset {current_offset})... (Total collected: {len(all_tempo_logs)})")

            # Pagination check (Tempo v4 often relies on fetched count vs limit)
            fetched_count_this_page = len(current_logs)
            if fetched_count_this_page < limit: # Reached the end for this user
                 break
            else:
                current_offset += limit
                time.sleep(0.1) # Short delay between pages

    # Fetch Jira details for any issue IDs found in Tempo logs but not already cached
    issue_ids_needing_details = issue_ids_from_tempo_logs - set(JIRA_TICKETS.keys())
    if issue_ids_needing_details:
         logging.info(f"Fetching Jira details for {len(issue_ids_needing_details)} additional issue IDs found in Tempo logs...")
         fetch_jira_ticket_details_concurrently(list(issue_ids_needing_details), JIRA_SERVER, JIRA_HEADERS)
         # Also try to get display names for authors of these logs if we don't have them
         authors_to_lookup = set()
         for log in all_tempo_logs:
             author_data = log.get('author')
             issue_data = log.get('issue')
             if isinstance(author_data, dict) and isinstance(issue_data, dict):
                 author_id = author_data.get('accountId')
                 issue_id = str(issue_data.get('id'))
                 if issue_id in issue_ids_needing_details and author_id and author_id not in accountid_to_displayname:
                     authors_to_lookup.add(author_id)
         # You might need a separate function/call to fetch user details by accountId if needed
         # For now, we rely on the names fetched during the main issue processing.

    logging.info(f"Processing {len(all_tempo_logs)} collected Tempo worklogs...")
    for log in all_tempo_logs:
        if not isinstance(log, dict): continue
        author_data = log.get('author'); issue_data = log.get('issue'); time_spent_seconds = log.get('timeSpentSeconds', 0)

        if isinstance(author_data, dict) and isinstance(issue_data, dict) and time_spent_seconds > 0:
            author_account_id = author_data.get('accountId')
            issue_id = str(issue_data.get('id'))

            # Get issue key: Prioritize cache, fallback to log data
            issue_details = JIRA_TICKETS.get(issue_id) # Returns dict or None
            issue_key = issue_details.get('key') if isinstance(issue_details, dict) else None
            if not issue_key: issue_key = issue_data.get('key') # Fallback

            if not issue_key:
                logging.debug(f"Skipping Tempo log {log.get('tempoWorklogId')} - could not determine issue key for ID {issue_id}")
                continue

            # IMPORTANT: Only aggregate time for issues that are part of our sprint
            if issue_key in sprint_issue_keys and author_account_id:
                logged_hours = seconds_to_hours(time_spent_seconds)
                tempo_time_per_ticket_user[issue_key][author_account_id] += logged_hours
                tempo_time_per_ticket_total[issue_key] += logged_hours
                # NEW: Add contributor account ID to the set for this ticket
                tempo_contributors_per_ticket[issue_key].add(author_account_id)
                # Ensure we have a display name mapping for this contributor if possible
                if author_account_id not in accountid_to_displayname:
                    author_name = author_data.get('displayName')
                    if author_name:
                        accountid_to_displayname[author_account_id] = author_name
                        logging.debug(f"Added contributor mapping from Tempo log: {author_account_id} -> {author_name}")


    # Update processed_tickets with the aggregated Tempo time
    for ticket in processed_tickets:
        ticket['tempo_logged_hours_total'] = round(tempo_time_per_ticket_total.get(ticket['key'], 0.0), 2)
else:
    logging.warning("Skipping Tempo worklog fetch: No assignees found in sprint tickets or sprint date range is incomplete.")

# print("Tempo time logged per ticket per user (AccountID):", json.dumps(tempo_time_per_ticket_user))
# print("Tempo contributors per ticket (AccountID):", json.dumps({k: list(v) for k, v in tempo_contributors_per_ticket.items()})) # Convert set to list for printing

# 5. Aggregate Data for Reporting
logging.info("Aggregating data for report...")
assignee_summary = defaultdict(lambda: {
    'ticket_count': 0, 'total_original_estimate': 0.0, 'total_time_spent_jira': 0.0,
    'epic_counts': Counter(), 'status_counts': Counter(), 'total_logged_tempo': 0.0,
    'account_id': None
})
assignee_remaining_hours_by_status = defaultdict(lambda: defaultdict(float))

status_counts_sprint = Counter()
total_sprint_estimate = 0.0
total_sprint_spent_jira = 0.0

for ticket in processed_tickets:
    assignee = ticket['assignee']
    acc_id = ticket.get('assignee_account_id')
    status = ticket['status']
    estimate = ticket['original_estimate_hours']
    spent_jira = ticket['time_spent_hours_jira']

    # Update standard summary
    assignee_summary[assignee]['ticket_count'] += 1
    assignee_summary[assignee]['total_original_estimate'] += estimate
    assignee_summary[assignee]['total_time_spent_jira'] += spent_jira
    assignee_summary[assignee]['epic_counts'][ticket['epic_designation']] += 1
    assignee_summary[assignee]['status_counts'][status] += 1
    if acc_id: assignee_summary[assignee]['account_id'] = acc_id

    # Update sprint totals
    status_counts_sprint[status] += 1
    total_sprint_estimate += estimate
    total_sprint_spent_jira += spent_jira

    # Calculate and aggregate remaining hours
    remaining_hours = max(0.0, estimate - spent_jira) # Ensure non-negative
    assignee_remaining_hours_by_status[assignee][status] += round(remaining_hours, 2)

# Aggregate total Tempo logged time per assignee using the already processed data
tempo_total_logged_per_assignee = defaultdict(float)
for issue_key, user_logs in tempo_time_per_ticket_user.items():
     # No need to check sprint_issue_keys again, already filtered during processing
     for acc_id, hours in user_logs.items():
         tempo_total_logged_per_assignee[acc_id] += hours

# Map Tempo totals back to assignee display names in the summary
for acc_id, logged_hours in tempo_total_logged_per_assignee.items():
     display_name = accountid_to_displayname.get(acc_id)
     if display_name and display_name in assignee_summary:
         assignee_summary[display_name]['total_logged_tempo'] = round(logged_hours, 2)
     elif display_name: # User logged time but wasn't assigned tickets in the initial fetch
         if display_name not in assignee_summary:
             assignee_summary[display_name]['total_logged_tempo'] = round(logged_hours, 2)
             if acc_id: assignee_summary[display_name]['account_id'] = acc_id
     else: # Could not find display name for this account ID
          # Create an entry using the account ID itself? Or log warning?
          assignee_key = f"Unknown User ({acc_id})"
          if assignee_key not in assignee_summary:
               assignee_summary[assignee_key]['total_logged_tempo'] = round(logged_hours, 2)
               assignee_summary[assignee_key]['account_id'] = acc_id
          logging.warning(f"Could not map Tempo logs for account ID {acc_id} (Total: {round(logged_hours,2)}h) to a known display name. Using ID.")


total_tickets_in_sprint = len(processed_tickets) or 1
status_percentages_sprint = {status: round((count / total_tickets_in_sprint) * 100, 1) for status, count in status_counts_sprint.items()}
valid_designations = sorted([d for d in all_epic_designations if d not in ["N/A", "Not Set", "Parent Not Found/No Access", "Unknown Format", "Error Fetching Parent"]])

# --- Prepare DataFrames for Google Sheets ---

# Sprint Details DataFrame
df_sprint_details = pd.DataFrame([
    ["Sprint Name", sprint_name_actual], ["Start Date", sprint_start_date_fmt],
    ["End Date", sprint_end_date_fmt], ["Days Remaining", days_remaining],
    ["Report Generated", current_timestamp]
], columns=['Metric', 'Value'])

# Assignee Summary DataFrames (Split)
capacity_time_rows = []
epic_rows = []
status_percent_rows = []
remaining_hours_rows = []

capacity_time_header = ['Assignee', 'Capacity', 'Remaining', 'New', 'Loading %', 'Ticket Count', 'Total Estimated (h)', 'Total Logged (Tempo API, h)']
epic_header = ['Assignee'] + [f'% {des}' for des in valid_designations]

# --- Define Status Column Order and Headers ---
other_statuses = sorted([
    s for s in all_sprint_statuses_sorted
    if s not in PRIMARY_STATUS_ORDER and s not in COMBINED_DONE_STATUSES
])
ordered_status_list = PRIMARY_STATUS_ORDER + ['Done'] + other_statuses # For % table header

status_percent_header = ['Assignee'] + \
                        [f'% {s}' for s in PRIMARY_STATUS_ORDER] + \
                        ['% Done'] + \
                        [f'% {s}' for s in other_statuses]

# Header for the Remaining Hours table (Uses raw status names in a consistent order)
ordered_status_list_for_hours = sorted(list(sprint_statuses)) # Use all statuses found, sorted alphabetically
remaining_hours_header = ['Assignee'] + [status_name for status_name in ordered_status_list_for_hours]
# --- End Status Column Definition ---

# Ensure we iterate through assignees consistently, e.g., alphabetically
sorted_assignees = sorted(assignee_summary.keys())

for assignee in sorted_assignees:
    data = assignee_summary[assignee]
    total_assignee_tickets = data.get('ticket_count', 0) or 1 # Use .get for safety

    # Capacity/Time Row
    capacity_time_rows.append([
        assignee, 80, "", "", "", data.get('ticket_count', 0),
        round(data.get('total_original_estimate', 0.0), 2),
        round(data.get('total_logged_tempo', 0.0), 2) # Use aggregated Tempo time
    ])

    # Epic Row
    epic_percentages = {des: round((data.get('epic_counts', Counter()).get(des, 0) / total_assignee_tickets) * 100, 1) for des in valid_designations}
    epic_rows.append([assignee] + [epic_percentages.get(des, 0.0) for des in valid_designations])

    # Status Percentage Row (Ordered and Combined)
    status_percentages_raw = {stat: round((data.get('status_counts', Counter()).get(stat, 0) / total_assignee_tickets) * 100, 1) for stat in all_sprint_statuses_sorted}
    done_resolved_pct = sum(status_percentages_raw.get(s, 0.0) for s in COMBINED_DONE_STATUSES)
    current_status_percent_row = [assignee]
    current_status_percent_row.extend(status_percentages_raw.get(s, 0.0) for s in PRIMARY_STATUS_ORDER)
    current_status_percent_row.append(round(done_resolved_pct, 1))
    current_status_percent_row.extend(status_percentages_raw.get(s, 0.0) for s in other_statuses)
    status_percent_rows.append(current_status_percent_row)

    # Remaining Hours Row
    current_remaining_hours_row = [assignee]
    # Iterate through the status header order defined for this table
    for status_name in ordered_status_list_for_hours: # Use the sorted list of all statuses
        hours = assignee_remaining_hours_by_status[assignee].get(status_name, 0.0)
        current_remaining_hours_row.append(round(hours, 2))
    remaining_hours_rows.append(current_remaining_hours_row)


df_assignee_capacity_time = pd.DataFrame(capacity_time_rows, columns=capacity_time_header)
df_assignee_epics = pd.DataFrame(epic_rows, columns=epic_header)
df_assignee_statuses_percent = pd.DataFrame(status_percent_rows, columns=status_percent_header)
df_assignee_remaining_hours = pd.DataFrame(remaining_hours_rows, columns=remaining_hours_header)


# Sprint Summary DataFrame (Overall Progress)
sprint_summary_rows = [
    ['Total Tickets', len(processed_tickets)], ['Total Time Estimated (h)', round(total_sprint_estimate, 2)],
    ['Total Time Spent (Jira Field, h)', round(total_sprint_spent_jira, 2)],
    ['Total Time Logged (Tempo API Sum, h)', round(sum(data.get('total_logged_tempo', 0.0) for data in assignee_summary.values()), 2)],
    ['-', '-'], ['Overall Sprint Status (%)', '']
]
for status, percentage in sorted(status_percentages_sprint.items()):
    sprint_summary_rows.append([f'% {status}', percentage])
df_summary = pd.DataFrame(sprint_summary_rows, columns=['Sprint Summary Metric', 'Value'])

# "Needs Attention" DataFrame
needs_attention_rows = []
needs_attention_header = ['Assignee', 'Ticket Link', 'Status', 'Original Estimate (h)', 'Total Logged (Jira, h)', 'Delay (h)']
for ticket in processed_tickets:
    estimate = ticket['original_estimate_hours']
    time_spent_jira = ticket['time_spent_hours_jira']
    if time_spent_jira > estimate > 0: # Only show if estimate was > 0 and spent > estimate
        key = ticket['key']; url_formula = create_hyperlink_formula(f"{JIRA_SERVER}/browse/{key}", key)
        needs_attention_rows.append([ticket['assignee'], url_formula, ticket['status'], estimate, time_spent_jira, round(time_spent_jira - estimate, 2)])
df_needs_attention = pd.DataFrame(needs_attention_rows, columns=needs_attention_header)

# "Tickets with Problems" DataFrame
problem_tickets_rows = []
problem_tickets_header = ['Ticket Link', 'Reporter', 'Assignee', 'Reason(s)']
for ticket in processed_tickets:
    key = ticket['key']; reasons = []
    if ticket['original_estimate_hours'] == 0: reasons.append("Missing Estimate")
    # elif ticket['original_estimate_hours'] > 8: reasons.append("Estimate > 8h") # Example threshold

    # Description Check (Handle ADF)
    desc = ticket.get('description')
    desc_text = ""
    if isinstance(desc, dict) and desc.get('type') == 'doc' and 'content' in desc: # Basic ADF check
        try:
            for node in desc['content']:
                if node.get('type') == 'paragraph' and 'content' in node:
                    for item in node['content']:
                        if item.get('type') == 'text' and 'text' in item:
                            desc_text += item['text'] + " "
            desc_text = desc_text.strip()
        except Exception as desc_parse_err:
            logging.debug(f"Could not parse ADF description for {key}: {desc_parse_err}")
            desc_text = "[Complex Description Format]"
    elif isinstance(desc, str): # Handle plain text description
        desc_text = desc.strip()

    if not desc_text or desc_text == "[Complex Description Format]":
        reasons.append("Missing/Empty Description")
    else:
        # Compare with summary (strip whitespace)
        if desc_text == ticket.get('summary', '').strip(): reasons.append("Desc = Summary")
        if len(desc_text) < 50: reasons.append("Desc < 50 chars")

    if reasons:
        url_formula = create_hyperlink_formula(f"{JIRA_SERVER}/browse/{key}", key)
        problem_tickets_rows.append([url_formula, ticket['reporter'], ticket['assignee'], ", ".join(reasons)])
df_problems = pd.DataFrame(problem_tickets_rows, columns=problem_tickets_header)

# --- NEW: Ticket Dump DataFrame ---
logging.info("Preparing ticket dump data...")
ticket_dump_rows = []
ticket_dump_header = ['Ticket Link', 'Assignee', 'Reporter', 'Original Estimate (h)', 'Time Spent (Jira, h)', 'Remaining Estimate (h)', 'Contributors in Current Sprint']

for ticket in processed_tickets:
    key = ticket['key']
    estimate = ticket['original_estimate_hours']
    spent_jira = ticket['time_spent_hours_jira']
    remaining_hours = round(max(0.0, estimate - spent_jira), 2)
    ticket_link_formula = create_hyperlink_formula(f"{JIRA_SERVER}/browse/{key}", key)

    # Get contributors
    contributor_ids = tempo_contributors_per_ticket.get(key, set())
    contributor_names = []
    for acc_id in contributor_ids:
        # Use display name if available, otherwise use ID as fallback
        name = accountid_to_displayname.get(acc_id, f"ID:{acc_id}")
        contributor_names.append(name)

    contributors_str = ", ".join(sorted(contributor_names)) if contributor_names else "" # Comma-separated string

    ticket_dump_rows.append([
        ticket_link_formula,
        ticket['assignee'],
        ticket['reporter'],
        estimate,
        spent_jira,
        remaining_hours,
        contributors_str
    ])

df_ticket_dump = pd.DataFrame(ticket_dump_rows, columns=ticket_dump_header)
# --- End NEW Ticket Dump ---


# --- Write to Google Sheet ---
logging.info(f"Preparing worksheet: '{sprint_name_actual}'...")
try:
    # --- Delete existing worksheet if found ---
    try:
        worksheet_to_delete = sh.worksheet(sprint_name_actual)
        sh.del_worksheet(worksheet_to_delete)
        logging.info(f"Deleted existing worksheet: '{sprint_name_actual}'")
        time.sleep(2) # Pause
    except gspread.WorksheetNotFound:
        logging.info(f"Worksheet '{sprint_name_actual}' not found. A new one will be created.")
    except Exception as del_err:
        logging.warning(f"Error occurred while trying to delete worksheet '{sprint_name_actual}': {del_err}. Proceeding to create.")
        time.sleep(1)

    # --- Create the new worksheet ---
    def get_df_dims(df):
        if df is None or df.empty: return 0, 0
        return df.shape[0], df.shape[1]

    rows_sprint, cols_sprint = get_df_dims(df_sprint_details)
    rows_cap, cols_cap = get_df_dims(df_assignee_capacity_time)
    rows_epic, cols_epic = get_df_dims(df_assignee_epics)
    rows_stat_pct, cols_stat_pct = get_df_dims(df_assignee_statuses_percent)
    rows_rem_hrs, cols_rem_hrs = get_df_dims(df_assignee_remaining_hours)
    rows_summ, cols_summ = get_df_dims(df_summary)
    rows_attn, cols_attn = get_df_dims(df_needs_attention)
    rows_prob, cols_prob = get_df_dims(df_problems)
    rows_dump, cols_dump = get_df_dims(df_ticket_dump) # NEW

    # Calculate rows: (data_rows + header_row + title_row + blank_row_after)
    required_rows = (rows_sprint + 3) + (rows_cap + 3) + (rows_epic + 3) + \
                    (rows_stat_pct + 3) + (rows_rem_hrs + 3) + (rows_summ + 3) + \
                    (rows_attn + 3) + (rows_prob + 3) + \
                    (rows_dump + 2) + 100 # NEW: Add dump rows (+2 for title/header, no blank after) + buffer

    required_cols = max(cols_sprint, cols_cap, cols_epic, cols_stat_pct, cols_rem_hrs,
                        cols_summ, cols_attn, cols_prob, cols_dump, 2) + 20 # NEW: Include dump cols + buffer

    worksheet = sh.add_worksheet(title=sprint_name_actual, rows=required_rows, cols=required_cols)
    logging.info(f"Created new worksheet: '{sprint_name_actual}' with {required_rows} rows and {required_cols} columns.")

    current_row = 1 # Start writing at row 1

    # --- Define Formats ---
    section_header_format = {
        "backgroundColor": {"red": 0.7, "green": 0.7, "blue": 0.7},
        "textFormat": {"fontSize": 12, "bold": True},
        "horizontalAlignment": "CENTER"
    }
    data_header_bg_color_hex = "#d9d2e9" # Light purple
    data_header_bg_color_rgb = hex_to_rgb_dict(data_header_bg_color_hex)
    if data_header_bg_color_rgb is None:
        logging.warning(f"Invalid hex color '{data_header_bg_color_hex}' for data headers. Using default.")
        data_header_bg_color_rgb = {"red": 0.85, "green": 0.85, "blue": 0.85} # Fallback gray
    data_header_format = {
        "backgroundColor": data_header_bg_color_rgb,
        "textFormat": {"bold": True}
    }
    # --- End Define Formats ---

    # Helper to write section header
    def write_section_header(title, start_row, num_cols):
        num_cols = max(1, num_cols)
        range_name = f'A{start_row}'
        values = [[title] + [''] * (num_cols - 1)]
        try:
            # Use named arguments: range_name=, values=
            worksheet.update(range_name=range_name, values=values)
            if num_cols > 1:
                worksheet.merge_cells(f'A{start_row}:{gspread.utils.rowcol_to_a1(start_row, num_cols)}')
            worksheet.format(range_name, section_header_format) # Format the single cell A{start_row} which now holds the merged value
        except gspread.exceptions.APIError as api_err:
             logging.error(f"API Error writing section header '{title}' at row {start_row}: {api_err}")
        except Exception as e:
             logging.error(f"Unexpected error writing section header '{title}' at row {start_row}: {e}")
        return start_row + 1

    # Helper to write DataFrame
    def write_dataframe(df, start_row, header_fmt):
        if df is None or df.empty:
             range_name = f'A{start_row}'
             values = [["No data in this section."]]
             try:
                 # Use named arguments: range_name=, values=
                 worksheet.update(range_name=range_name, values=values)
             except gspread.exceptions.APIError as api_err:
                  logging.error(f"API Error writing 'No data' message at row {start_row}: {api_err}")
             except Exception as e:
                  logging.error(f"Unexpected error writing 'No data' message at row {start_row}: {e}")
             return start_row + 1

        # Proceed with writing non-empty DataFrame
        header_list = [df.columns.values.tolist()]
        # Convert potential numpy types/NaN to standard Python types for JSON serialization
        data_list = df.astype(object).where(pd.notnull(df), None).values.tolist()
        num_cols_df = len(header_list[0])
        num_rows_df = len(data_list)
        header_range_name = f'A{start_row}:{gspread.utils.rowcol_to_a1(start_row, num_cols_df)}'
        data_range_name = f'A{start_row + 1}' # gspread expands this automatically

        try:
            # Write header using named arguments
            worksheet.update(range_name=header_range_name, values=header_list, value_input_option='USER_ENTERED')
            # Format header
            worksheet.format(header_range_name, header_fmt)

            # Write data if any exists using named arguments
            if data_list:
                worksheet.update(range_name=data_range_name, values=data_list, value_input_option='USER_ENTERED') # USER_ENTERED allows formulas

            return start_row + 1 + num_rows_df # Next available row
        except gspread.exceptions.APIError as api_err:
            logging.error(f"API Error writing DataFrame starting at row {start_row}: {api_err.response.text if hasattr(api_err, 'response') else api_err}")
            return start_row + 1 # Skip writing data
        except Exception as e:
            logging.error(f"Unexpected error writing DataFrame starting at row {start_row}: {e}")
            return start_row + 1 # Skip writing data

    # --- Write Sections ---
    logging.info("Writing Sprint Details...")
    current_row = write_section_header("Sprint Details", current_row, cols_sprint or 2)
    current_row = write_dataframe(df_sprint_details, current_row, data_header_format)
    current_row += 1 # Add blank row

    logging.info("Writing Assignee Capacity/Time Summary...")
    current_row = write_section_header("Assignee Summary: Capacity & Time", current_row, cols_cap or 2)
    current_row = write_dataframe(df_assignee_capacity_time, current_row, data_header_format)
    current_row += 1 # Add blank row

    logging.info("Writing Assignee Epic Designation Summary...")
    current_row = write_section_header("Assignee Summary: Epic Designation %", current_row, cols_epic or 2)
    current_row = write_dataframe(df_assignee_epics, current_row, data_header_format)
    current_row += 1 # Add blank row

    logging.info("Writing Assignee Status % Summary...")
    status_percent_section_start_row = current_row
    current_row = write_section_header("Assignee Summary: Status %", current_row, cols_stat_pct or 2)
    status_percent_header_row = current_row
    status_percent_data_start_row = status_percent_header_row + 1
    status_percent_data_end_row = status_percent_header_row + rows_stat_pct
    current_row = write_dataframe(df_assignee_statuses_percent, current_row, data_header_format)
    current_row += 1 # Add blank row

    logging.info("Writing Assignee Remaining Hours by Status Summary...")
    current_row = write_section_header("Assignee Summary: Remaining Hours by Status", current_row, cols_rem_hrs or 2)
    current_row = write_dataframe(df_assignee_remaining_hours, current_row, data_header_format)
    current_row += 1 # Add blank row

    logging.info("Writing Overall Sprint Summary...")
    current_row = write_section_header("Overall Sprint Summary", current_row, cols_summ or 2)
    current_row = write_dataframe(df_summary, current_row, data_header_format)
    current_row += 1 # Add blank row

    logging.info("Writing 'Needs Attention' section...")
    current_row = write_section_header("Needs Attention (Jira Logged > Estimate)", current_row, cols_attn or 2)
    current_row = write_dataframe(df_needs_attention, current_row, data_header_format)
    current_row += 1 # Add blank row

    logging.info("Writing 'Tickets with Problems' section...")
    current_row = write_section_header("Tickets with Problems (Estimate/Description Issues)", current_row, cols_prob or 2)
    current_row = write_dataframe(df_problems, current_row, data_header_format)
    current_row += 1 # Add blank row

    # --- NEW: Write Ticket Dump Section ---
    logging.info("Writing Ticket Dump section...")
    current_row = write_section_header("Sprint - Raw Data", current_row, cols_dump or 2)
    current_row = write_dataframe(df_ticket_dump, current_row, data_header_format)
    # No blank row needed at the very end
    # --- End NEW Section ---


    # --- Apply Specific Background Colors to Status PERCENTAGE DATA Columns ---
    logging.info("Applying specific background colors to status PERCENTAGE DATA columns...")
    if not df_assignee_statuses_percent.empty and status_percent_data_start_row <= status_percent_data_end_row:
        status_cols_percent = df_assignee_statuses_percent.columns.tolist()
        for col_idx, col_name in enumerate(status_cols_percent):
            if col_name in STATUS_COLUMN_COLORS:
                hex_color = STATUS_COLUMN_COLORS[col_name]
                rgb_dict = hex_to_rgb_dict(hex_color)
                if rgb_dict:
                    col_letter = gspread.utils.rowcol_to_a1(1, col_idx + 1)[:-1]
                    range_to_format = f"{col_letter}{status_percent_data_start_row}:{col_letter}{status_percent_data_end_row}"
                    try:
                        logging.debug(f"Formatting PERCENTAGE DATA range {range_to_format} for status '{col_name}' with color {hex_color}")
                        worksheet.format(range_to_format, {"backgroundColor": rgb_dict})
                    except gspread.exceptions.APIError as api_err:
                        logging.error(f"API Error formatting range {range_to_format} for status '{col_name}': {api_err.response.text if hasattr(api_err, 'response') else api_err}")
                    except Exception as e:
                        logging.error(f"Unexpected error formatting range {range_to_format} for status '{col_name}': {e}")
                else:
                    logging.warning(f"Invalid hex color format for status '{col_name}': {hex_color}")
    else:
        logging.info("Skipping status percentage column coloring as the section is empty or has no data rows.")
    # --- End Apply Specific Background Colors ---


    logging.info(f"Successfully populated new Google Sheet. Link: {sh.url}")

except gspread.exceptions.APIError as ge:
     # Try to get more details from the API error response
     error_details = ge.response.text if hasattr(ge, 'response') and hasattr(ge.response, 'text') else str(ge)
     logging.error(f"Error during Google Sheets operation (APIError): {error_details}", exc_info=True)
     logging.error("Check sheet permissions for the service account email, API quota, and if the sheet name is valid.")
except gspread.exceptions.GSpreadException as gse:
    logging.error(f"A gspread library error occurred: {gse}", exc_info=True)
except Exception as e:
    logging.error(f"An unexpected error occurred during sheet operations: {e}", exc_info=True)

logging.info("Script finished.")