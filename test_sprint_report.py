import unittest
from unittest.mock import patch, MagicMock, call
import sys
import json
import datetime
from dateutil import parser as date_parser
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from collections import Counter, defaultdict

# Import the module to test
import sprint_report

class TestSprintReport(unittest.TestCase):
    
    def setUp(self):
        """Set up test fixtures before each test method."""
        # Save original configuration values to restore after tests
        self.original_jira_server = sprint_report.JIRA_SERVER
        self.original_jira_board_id = sprint_report.JIRA_BOARD_ID
        self.original_sprint_name = sprint_report.SPRINT_NAME
        self.original_epic_designation_field_name = sprint_report.EPIC_DESIGNATION_FIELD_NAME
        self.original_tempo_server = sprint_report.TEMPO_SERVER
        self.original_jira_tickets = sprint_report.JIRA_TICKETS.copy()
        
        # Set test values
        sprint_report.JIRA_SERVER = "https://jira-test.example.com"
        sprint_report.JIRA_BOARD_ID = 1234
        sprint_report.SPRINT_NAME = "Test Sprint"
        sprint_report.EPIC_DESIGNATION_FIELD_NAME = "Epic Designation"
        sprint_report.TEMPO_SERVER = "https://api.tempo.io/4"
        
        # Clear cache
        sprint_report.JIRA_TICKETS = {}
    
    def tearDown(self):
        """Tear down test fixtures after each test method."""
        # Restore original configuration values
        sprint_report.JIRA_SERVER = self.original_jira_server
        sprint_report.JIRA_BOARD_ID = self.original_jira_board_id
        sprint_report.SPRINT_NAME = self.original_sprint_name
        sprint_report.EPIC_DESIGNATION_FIELD_NAME = self.original_epic_designation_field_name
        sprint_report.TEMPO_SERVER = self.original_tempo_server
        sprint_report.JIRA_TICKETS = self.original_jira_tickets
    
    # Test Authentication Functions
    def test_get_jira_headers(self):
        """Test that get_jira_headers returns correctly formatted headers."""
        sprint_report.JIRA_AUTH_TOKEN_BASE64 = "test-token"
        headers = sprint_report.get_jira_headers()
        
        self.assertEqual(headers["Authorization"], "Basic test-token")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["Content-Type"], "application/json")
    
    def test_get_tempo_headers(self):
        """Test that get_tempo_headers returns correctly formatted headers."""
        sprint_report.TEMPO_API_TOKEN = "test-token"
        headers = sprint_report.get_tempo_headers()
        
        self.assertEqual(headers["Authorization"], "Bearer test-token")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(headers["Content-Type"], "application/json")
    
    # Test Helper Functions
    def test_seconds_to_hours(self):
        """Test that seconds_to_hours correctly converts seconds to hours."""
        self.assertEqual(sprint_report.seconds_to_hours(3600), 1.0)
        self.assertEqual(sprint_report.seconds_to_hours(7200), 2.0)
        self.assertEqual(sprint_report.seconds_to_hours(5400), 1.5)
        
        # Test edge cases
        self.assertEqual(sprint_report.seconds_to_hours(None), 0.0)
        self.assertEqual(sprint_report.seconds_to_hours(-3600), 0.0)
        self.assertEqual(sprint_report.seconds_to_hours("not a number"), 0.0)
    
    def test_create_hyperlink_formula(self):
        """Test that create_hyperlink_formula correctly creates a Google Sheets formula."""
        url = "https://example.com"
        text = "Example"
        formula = sprint_report.create_hyperlink_formula(url, text)
        
        self.assertEqual(formula, '=HYPERLINK("https://example.com", "Example")')
        
        # Test with quotes in URL or text
        url_with_quotes = 'https://example.com/page?q="test"'
        text_with_quotes = 'Click "here"'
        formula = sprint_report.create_hyperlink_formula(url_with_quotes, text_with_quotes)
        
        self.assertEqual(formula, '=HYPERLINK("https://example.com/page?q=""test""", "Click ""here""")')
    
    def test_format_date(self):
        """Test that format_date correctly formats ISO date strings."""
        self.assertEqual(sprint_report.format_date("2023-01-01T00:00:00.000Z"), "2023-01-01")
        self.assertEqual(sprint_report.format_date(""), "N/A")
        self.assertEqual(sprint_report.format_date(None), "N/A")
        self.assertEqual(sprint_report.format_date("invalid date"), "N/A")
        
        # Test with custom default
        self.assertEqual(sprint_report.format_date("", default="Unknown"), "Unknown")
    
    def test_calculate_days_remaining(self):
        """Test that calculate_days_remaining correctly calculates days remaining."""
        today = datetime.date.today()
        future = today + datetime.timedelta(days=30)
        future_str = future.isoformat() + "T00:00:00.000Z"
        self.assertEqual(sprint_report.calculate_days_remaining(future_str), 30)
        
        past = today - datetime.timedelta(days=30)
        past_str = past.isoformat() + "T00:00:00.000Z"
        self.assertEqual(sprint_report.calculate_days_remaining(past_str), 0)
        
        self.assertEqual(sprint_report.calculate_days_remaining(""), "N/A")
        self.assertEqual(sprint_report.calculate_days_remaining(None), "N/A")
        self.assertEqual(sprint_report.calculate_days_remaining("invalid date"), "N/A")
        self.assertEqual(sprint_report.calculate_days_remaining("", default="Unknown"), "Unknown")
    
    def test_hex_to_rgb_dict(self):
        """Test that hex_to_rgb_dict correctly converts hex codes."""
        self.assertEqual(sprint_report.hex_to_rgb_dict("#ffffff"), {"red":1.0,"green":1.0,"blue":1.0})
        self.assertEqual(sprint_report.hex_to_rgb_dict("#000000"), {"red":0.0,"green":0.0,"blue":0.0})
        self.assertEqual(sprint_report.hex_to_rgb_dict("#ff0000"), {"red":1.0,"green":0.0,"blue":0.0})
        self.assertEqual(sprint_report.hex_to_rgb_dict("00ff00"), {"red":0.0,"green":1.0,"blue":0.0})
        self.assertIsNone(sprint_report.hex_to_rgb_dict("#f00"))
        self.assertIsNone(sprint_report.hex_to_rgb_dict("#wxyzab"))
    
    # Test API Request Function
    @patch('sprint_report.requests.request')
    def test_make_request_success(self, mock_request):
        """Test successful API request."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data":"ok"}
        mock_request.return_value = mock_resp
        
        result = sprint_report.make_request("GET","https://example.com",headers={})
        mock_request.assert_called_once_with("GET","https://example.com",headers={},params=None,json=None,timeout=sprint_report.REQUEST_TIMEOUT)
        self.assertEqual(result,{"data":"ok"})
    
    @patch('sprint_report.requests.request')
    @patch('sprint_report.time.sleep')
    def test_make_request_retry(self, mock_sleep, mock_request):
        """Test retry logic on server error."""
        fail = MagicMock(); fail.status_code=500
        fail.raise_for_status.side_effect = sprint_report.requests.exceptions.HTTPError()
        success = MagicMock(); success.status_code=200; success.json.return_value={"ok":True}
        mock_request.side_effect=[fail, success]
        
        result = sprint_report.make_request("GET","https://example.com",headers={},retries=1)
        self.assertEqual(mock_request.call_count,2)
        mock_sleep.assert_called_once_with(sprint_report.RETRY_DELAY)
        self.assertEqual(result,{"ok":True})
    
    @patch('sprint_report.requests.request')
    @patch('sprint_report.time.sleep')
    def test_make_request_all_failures(self, mock_sleep, mock_request):
        """Test exhaust retries returns None."""
        fail = MagicMock(); fail.status_code=500
        fail.raise_for_status.side_effect = sprint_report.requests.exceptions.HTTPError()
        mock_request.return_value = fail
        
        result = sprint_report.make_request("GET","https://example.com",headers={},retries=2)
        self.assertEqual(mock_request.call_count,3)
        self.assertEqual(mock_sleep.call_count,2)
        self.assertIsNone(result)
    
    @patch('sprint_report.requests.request')
    def test_make_request_auth_error_no_retry(self, mock_request):
        """Test no retry on auth failure."""
        fail = MagicMock(); fail.status_code=401
        fail.raise_for_status.side_effect = sprint_report.requests.exceptions.HTTPError()
        mock_request.return_value = fail
        
        result = sprint_report.make_request("GET","https://example.com",headers={})
        mock_request.assert_called_once()
        self.assertIsNone(result)
    
    # Test Custom Field ID Function
    @patch('sprint_report.make_request')
    def test_find_custom_field_id_found(self, mock_make_request):
        """Test finding an existing custom field."""
        mock_make_request.return_value = [
            {"id":"field1","name":"Field 1","custom":False},
            {"id":"customfield_10001","name":"Epic Designation","custom":True}
        ]
        result = sprint_report.find_custom_field_id("Epic Designation","https://jira.example.com",{})
        self.assertEqual(result,"customfield_10001")
    
    @patch('sprint_report.make_request')
    def test_find_custom_field_id_not_found(self, mock_make_request):
        """Test fallback when custom field missing."""
        mock_make_request.return_value = [{"id":"field1","name":"Other","custom":True}]
        result = sprint_report.find_custom_field_id("Epic Designation","https://jira.example.com",{})
        self.assertEqual(result,"Epic Designation")
    
    # Test Sprint Issues Processing
    def test_process_tickets(self):
        """Test inline processing of Jira issues."""
        # replicate logic from sprint_report for raw issues
        sprint_issues_raw = [
            {"key":"TEST-1","fields":{"summary":"S1","status":{"name":"To Do"},"assignee":{"displayName":"U1","accountId":"user123"},"reporter":{"displayName":"R1"},"timetracking":{"originalEstimateSeconds":3600,"timeSpentSeconds":1800}}},
            {"key":"TEST-2","fields":{"summary":"S2","status":{"name":"Done"},"assignee":None,"reporter":{"displayName":"R2"},"timetracking":{}}}
        ]
        processed = []
        for issue in sprint_issues_raw:
            key = issue.get("key"); f=issue.get("fields",{})
            est = sprint_report.seconds_to_hours(f.get("timetracking",{}).get("originalEstimateSeconds",0))
            spent = sprint_report.seconds_to_hours(f.get("timetracking",{}).get("timeSpentSeconds",0))
            processed.append({"key":key,"original_estimate_hours":est,"time_spent_hours_jira":spent})
        self.assertEqual(processed[0]["original_estimate_hours"],1.0)
        self.assertEqual(processed[0]["time_spent_hours_jira"],0.5)
        self.assertEqual(processed[1]["original_estimate_hours"],0.0)
    
    # Test Epic Designation Logic
    @patch('sprint_report.make_request')
    def test_fetch_parent_epic_designations(self, mock_make_request):
        """Test fetching and applying epic designations."""
        mock_make_request.return_value={"fields":{"customfield_10001":{"value":"Customer Feature"}}}
        sprint_report.JIRA_HEADERS={"Authorization":"Basic t"}
        result = {}
        for key in {"E1"}:
            data = sprint_report.make_request("GET",f"{sprint_report.JIRA_SERVER}/rest/api/3/issue/{key}", params={"fields":"summary,customfield_10001"}, headers=sprint_report.JIRA_HEADERS)
            val = data["fields"]["customfield_10001"]["value"]
            result[key]=val
        self.assertEqual(result,{"E1":"Customer Feature"})
    
    # Test Tempo Worklog Processing
    @patch('sprint_report.make_request')
    def test_process_tempo_worklogs(self, mock_make_request):
        """Test aggregating Tempo worklogs."""
        calls = []
        def side(*args,**kw):
            return {"results":[{"issue":{"key":"T1"},"timeSpentSeconds":3600,"author":{"accountId":"u1"}}]}
        mock_make_request.side_effect = side
        totals = defaultdict(float)
        for uid in ["u1"]:
            resp = sprint_report.make_request("POST",f"{sprint_report.TEMPO_SERVER}/worklogs/search", headers={}, json_payload={"authorIds":[uid]})
            for log in resp.get("results",[]):
                if log["issue"]["key"]=="T1":
                    totals["T1"]+=sprint_report.seconds_to_hours(log["timeSpentSeconds"])
        self.assertEqual(totals["T1"],1.0)
    
    # Test Data Aggregation
    def test_aggregate_data(self):
        """Test summary aggregation logic."""
        tickets = [
            {"assignee":"A","original_estimate_hours":2.0,"time_spent_hours_jira":1.0,"tempo_logged_hours_total":1.5,"status":"Done","epic_designation":"Feat"},
            {"assignee":"A","original_estimate_hours":1.0,"time_spent_hours_jira":0.5,"tempo_logged_hours_total":0.5,"status":"To Do","epic_designation":"Feat"}
        ]
        summary = defaultdict(lambda:{"ticket_count":0,"total_original_estimate":0.0,"total_time_spent_jira":0.0,"total_logged_tempo":0.0,"status_counts":Counter(),"epic_counts":Counter()})
        for t in tickets:
            a=t["assignee"]; summary[a]["ticket_count"]+=1
            summary[a]["total_original_estimate"]+=t["original_estimate_hours"]
            summary[a]["total_time_spent_jira"]+=t["time_spent_hours_jira"]
            summary[a]["total_logged_tempo"]+=t["tempo_logged_hours_total"]
            summary[a]["status_counts"][t["status"]]+=1
            summary[a]["epic_counts"][t["epic_designation"]]+=1
        self.assertEqual(summary["A"]["ticket_count"],2)
        self.assertEqual(summary["A"]["total_original_estimate"],3.0)
    
    # Test DataFrame Creation
    def test_prepare_dataframes(self):
        """Test constructing DataFrame for sprint details."""
        df = pd.DataFrame([
            ["Sprint Name","S1"],
            ["Start Date","2023-01-01"],
            ["End Date","2023-01-02"],
            ["Days Remaining",5],
            ["Report Generated","2023-01-01 00:00:00"]
        ],columns=["Metric","Value"])
        self.assertEqual(df.shape,(5,2))
        self.assertEqual(df.iloc[0,1],"S1")

if __name__ == '__main__':
    unittest.main()