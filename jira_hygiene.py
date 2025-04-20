import requests
from collections import defaultdict
import gspread
from oauth2client.service_account import ServiceAccountCredentials

scope = ['https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/drive']
SHEET_KEY = '' 
spreadsheet = None

GSHEET_CREDENTIALS = {}
creds = ServiceAccountCredentials.from_json_keyfile_dict(GSHEET_CREDENTIALS, scope)
client = gspread.authorize(creds)
gsheet = client.open_by_key(SHEET_KEY)



JIRA_URL = ""
JIRA_AUTH_TOKEN = ""
JIRA_SQUAD_SPRINTS = {
    "XXX": ["Sprint1"],
    # "MCE": ["25Q1 MX [Mar 13 - Mar 27]", "25Q2MX1"],
    # "DL": ["DCT_20250318_20250401"],
    # "SFDC": ["[25Q1] SFDC 6th Sprint", "[25Q1] SFDC 5th Sprint", "[25Q2] SFDC 1st Sprint"],
    # "MAD": ["MA Q1 25 (March 24 - April 7)"]
}
CURRENT_SPRINT_QUERY = "openSprints()"

ROLES = {
    "XXX": {
        "abc": "dev",
        "xyz": "em",
        "pqr": "pm",
    },
}

def get_jira_api_data(endpoint, headers, params=None):
    try:
        response = requests.get(endpoint, headers=headers, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching data from Jira API: {e}")
        return None

def process_tickets(squad, tickets):
    result = {
        'monthly_tickets': defaultdict(list),
        'reporter_stats': defaultdict(list),
        'reporter_role_stats': defaultdict(list),
        'estimate_stats': {
            'less_than_4h': [],
            'more_than_4h': [],
            'no_estimate': []
        },
        'description_stats': {
            'poor_description': [],
            'detailed_description': []
        }
    }
    
    for ticket in tickets:
        ticket_key = ticket['key']
        fields = ticket['fields']

        reporter = fields['reporter']['displayName'] if 'reporter' in fields and fields['reporter'] else 'Unknown'
        result['reporter_stats'][reporter].append(ticket_key)
        result['reporter_role_stats'][ROLES.get(squad, {}).get(reporter, 'External')].append(ticket_key)
        
        estimate_seconds = fields.get('timeoriginalestimate', 0)
        if estimate_seconds is None:
            result['estimate_stats']['no_estimate'].append(ticket_key)
        elif estimate_seconds <= 14400:  # 4 hours in seconds
            result['estimate_stats']['less_than_4h'].append(ticket_key)
        else:
            result['estimate_stats']['more_than_4h'].append(ticket_key)
        
        description = fields.get('description', '') or ''
        summary = fields.get('summary', '') or ''
        
        if (not description or 
            description == summary or 
            len(description) < 50):
            result['description_stats']['poor_description'].append(ticket_key)
        else:
            result['description_stats']['detailed_description'].append(ticket_key)
    
    return result

def generate_jira_analysis_report(squad, sprint_id, processed_data, total_tickets):
    report = [f"{squad}, {sprint_id}"]
    
    reporter_stats = ["\n\n"]
    reporter_role_stats = ["\n\n", "Reporter, Role, Percentage, Total Tickets"]
    report.append("TICKETS CREATED BY EACH REPORTER")
    report.append("Reporter, Percentage, Total Tickets")
    for i in range(4 - len(processed_data['reporter_role_stats'].keys())):
        report.append(" ") #Appending empty rows to resolve issues with summary generation

    for reporter, tickets in processed_data['reporter_role_stats'].items():
        percentage = (len(tickets) / total_tickets) * 100
        report.append(f"{reporter}, {percentage:.2f}%, {len(tickets)}")
    
    for reporter, tickets in processed_data['reporter_stats'].items():
        percentage = (len(tickets) / total_tickets) * 100
        reporter_role_stats.append(f"{reporter}, {ROLES.get(squad, {}).get(reporter, 'External')}, {percentage:.2f}%, {len(tickets)}")
        reporter_stats.append(f"Tickets reported by {reporter}")
        reporter_stats.append('\n'.join([f"{t}, {JIRA_URL}/browse/{t} " for t in tickets]))
    
    report.append("\nTICKETS BY ESTIMATE")
    estimate_stats_less = ["\n\n", "Type, Percentage, Total Tickets"]
    less_than_4h = len(processed_data['estimate_stats']['less_than_4h'])
    less_than_4h_percentage = (less_than_4h / total_tickets) * 100 if total_tickets > 0 else 0
    report.append(f"Less than 4 hours, {less_than_4h_percentage:.2f}%, {less_than_4h}")
    estimate_stats_less.append(f"Ticket IDs with less than 4 hours")
    estimate_stats_less.append('\n'.join([f"{t}, {JIRA_URL}/browse/{t} " for t in processed_data['estimate_stats']['less_than_4h']]))
    
    estimate_stats_more = ["\n\n"]
    more_than_4h = len(processed_data['estimate_stats']['more_than_4h'])
    more_than_4h_percentage = (more_than_4h / total_tickets) * 100 if total_tickets > 0 else 0
    report.append(f"More than 4 hours,  {more_than_4h_percentage:.2f}%, {more_than_4h}")
    estimate_stats_more.append(f"Ticket IDs with more than 4 hours")
    estimate_stats_more.append('\n'.join([f"{t}, {JIRA_URL}/browse/{t} " for t in processed_data['estimate_stats']['more_than_4h']]))
    
    estimate_stats_no_hrs = ["\n\n"]
    no_estimate = len(processed_data['estimate_stats']['no_estimate'])
    no_estimate_percentage = (no_estimate / total_tickets) * 100 if total_tickets > 0 else 0
    report.append(f"No estimate, {no_estimate_percentage:.2f}%, {no_estimate}")
    estimate_stats_no_hrs.append(f"Ticket IDs with no estimate")
    estimate_stats_no_hrs.append('\n'.join([f"{t}, {JIRA_URL}/browse/{t} " for t in processed_data['estimate_stats']['no_estimate']]))
    
    desc_stats = ["\n\n"]
    report.append("\nTICKETS BY DESCRIPTION")
    poor_desc_tickets = processed_data['description_stats']['poor_description']
    poor_desc_percentage = (len(poor_desc_tickets) / total_tickets) * 100 if total_tickets > 0 else 0
    report.append(f"Poor description, {poor_desc_percentage:.2f}%, {len(poor_desc_tickets)}")
    desc_stats.append(f"Tickets with poor description")
    desc_stats.append('\n'.join([f"{t}, {JIRA_URL}/browse/{t} " for t in poor_desc_tickets]))

    detailed_desc_tickets = processed_data['description_stats']['detailed_description']
    detailed_desc_percentage = (len(detailed_desc_tickets) / total_tickets) * 100 if total_tickets > 0 else 0
    report.append(f"Detailed description, {detailed_desc_percentage:.2f}%, {len(detailed_desc_tickets)}")
    desc_stats.append(f"Tickets with detailed description")
    desc_stats.append('\n'.join([f"{t}, {JIRA_URL}/browse/{t} " for t in detailed_desc_tickets]))
    
    report.extend(reporter_role_stats)
    report.extend(reporter_stats)
    report.extend(estimate_stats_less)
    report.extend(estimate_stats_more)
    report.extend(estimate_stats_no_hrs)
    report.extend(desc_stats)

    return "\n".join(report)

def get_sprint_tickets(sqaud, sprint_id):
    sprint_query = f"'{sprint_id}'" if sprint_id else CURRENT_SPRINT_QUERY
    jql_query = f"project = '{sqaud}' AND sprint in ({sprint_query})"
    print(jql_query)
    
    url = f"{JIRA_URL}/rest/api/2/search"
    headers = {"Accept": "application/json", "Authorization": f"Basic {JIRA_AUTH_TOKEN}"}
    params = {"jql": jql_query, "maxResults": 1000, "fields": "key,summary,description,created,updated,reporter,timeoriginalestimate,assignee,status"}
    response = get_jira_api_data(url, headers, params)
    jira_tickets = response.get("issues", [])
    return jira_tickets


def write_to_gsheet(worksheet_name, report):
    try:
        worksheet = gsheet.worksheet(worksheet_name)
        # Clear existing content if worksheet already exists
        worksheet.clear()
    except gspread.exceptions.WorksheetNotFound:
        worksheet = gsheet.add_worksheet(title=worksheet_name, rows=100, cols=20)
    
    report_lines = report.split('\n')
    data = [list(map(lambda x: x.strip().lstrip("\'"), line.split(","))) for line in report_lines]
    if data:
        worksheet.update('A1', data, value_input_option='USER_ENTERED')
    print(f"\nReport saved to Google Sheet: {worksheet_name}")

# Main function to orchestrate the entire process
def main():
    print("Fetching Jira tickets...")
    for squad, sprint_ids in JIRA_SQUAD_SPRINTS.items():
        for i in range(max(len(sprint_ids), 1)):
            sprint_id = sprint_ids[i] if len(sprint_ids) > 0 else CURRENT_SPRINT_QUERY
            print(f"\nFetching Jira tickets for Squad: {squad}, Sprint: {sprint_id}")
            jira_tickets = get_sprint_tickets(squad, sprint_id)
            print("Jira tickets fetched \n ", [v["key"] for v in jira_tickets], len(jira_tickets))
            processed_data = process_tickets(squad, jira_tickets)
            # print(processed_data)
            report = generate_jira_analysis_report(squad, sprint_id, processed_data, len(jira_tickets))
            # print("\nJIRA TICKET ANALYSIS REPORT")
            # print("=========================\n")
            # print(report)

            worksheet_name = f"{squad}_{sprint_id}"
            write_to_gsheet(worksheet_name, report)
            
            # Save report to file
            # output_file = f"{squad}_{sprint_id}_jira_ticket_report.txt"
            # with open(output_file, 'w') as f:
            #     f.write(report)

            # print(f"\nReport saved to {output_file}")

if __name__ == "__main__":
    main()
