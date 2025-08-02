import configparser
import schedule
import time
import requests
import gitlab
from github import Github
from slack_sdk.webhook import WebhookClient
import smtplib
from email.mime.text import MIMEText

# Global state to store the previous issues and comments
previous_issues = {}
previous_comments = {}

def get_config():
    config = configparser.ConfigParser()
    config.read('config.ini')
    return config

def get_gitlab_client():
    config = get_config()
    private_token = config['gitlab']['private_token']
    server_url = config['gitlab']['server_url']
    project_id = config['gitlab']['project_id']
    gl = gitlab.Gitlab(server_url, private_token=private_token)
    project = gl.projects.get(project_id)
    return project

def get_github_client():
    config = get_config()
    access_token = config['github']['access_token']
    repo_name = config['github']['repo_name']
    g = Github(access_token)
    repo = g.get_repo(repo_name)
    return repo

def send_slack_notification(webhook_url, message):
    try:
        webhook = WebhookClient(webhook_url)
        response = webhook.send(text=message)
        print(f"Slack notification sent: {response.status_code}")
    except Exception as e:
        print(f"Error sending Slack notification: {e}")

def send_email_notification(config, subject, message):
    try:
        smtp_server = config['mail']['smtp_server']
        smtp_port = int(config['mail']['smtp_port'])
        smtp_user = config['mail']['smtp_user']
        smtp_password = config['mail']['smtp_password']
        recipient_email = config['mail']['recipient_email']

        msg = MIMEText(message)
        msg['Subject'] = subject
        msg['From'] = smtp_user
        msg['To'] = recipient_email

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
            print("Email notification sent successfully.")
    except Exception as e:
        print(f"Error sending email notification: {e}")

def send_api_notification(config, payload):
    try:
        url = config['api']['url']
        bearer_token = config['api']['bearer_token']
        headers = {
            'Authorization': f'Bearer {bearer_token}',
            'Content-Type': 'application/json'
        }
        response = requests.post(url, json=payload, headers=headers)
        print(f"API notification sent. Status Code: {response.status_code}")
        print(f"Response: {response.text}")
    except Exception as e:
        print(f"Error sending API notification: {e}")

def notify(config, subject, message, payload):
    notification_type = config['notification']['type']
    if notification_type == 'slack':
        send_slack_notification(config['slack']['webhook_url'], message)
    elif notification_type == 'mail':
        send_email_notification(config, subject, message)
    elif notification_type == 'api':
        send_api_notification(config, payload)

def check_changes():
    global previous_issues, previous_comments
    
    config = get_config()
    platform = config['general']['platform']
    
    if platform == 'gitlab':
        project = get_gitlab_client()
        issues = project.issues.list(state='all', get_all=True)
        current_issues_map = {issue.iid: issue for issue in issues}
        
        if not previous_issues:
            previous_issues = current_issues_map
            for issue in issues:
                previous_comments[issue.iid] = project.issues.get(issue.iid).notes.list()
            print("Initial GitLab state loaded. Monitoring will begin.")
            return

        # Check for changes in GitLab issues
        for issue_iid, issue in current_issues_map.items():
            prev_issue = previous_issues.get(issue_iid)
            
            # New issue
            if not prev_issue:
                message = f"[New GitLab Issue] {issue.title} (ID: {issue.iid})\nURL: {issue.web_url}"
                payload = {"title": issue.title, "created_at": issue.created_at, "issue_id": issue.iid, "status": "등록"}
                notify(config, "GitLab New Issue Alert", message, payload)
            # Title or state change
            elif issue.state != prev_issue.state or issue.title != prev_issue.title:
                message = f"[GitLab Issue Change] '{prev_issue.title}' (ID: {issue.iid}) updated.\nURL: {issue.web_url}"
                payload = {"title": issue.title, "updated_at": issue.updated_at, "issue_id": issue.iid, "status": "수정" if issue.state != 'closed' else 'close'}
                notify(config, "GitLab Issue Update Alert", message, payload)
            
            # New comment
            current_comments = project.issues.get(issue.iid).notes.list()
            if len(current_comments) > len(previous_comments.get(issue_iid, [])):
                message = f"[GitLab New Comment] New comment on '{issue.title}' (ID: {issue.iid}).\nURL: {issue.web_url}"
                payload = {"title": issue.title, "comment_created_at": current_comments[-1].created_at, "issue_id": issue.iid, "status": "comment 등록"}
                notify(config, "GitLab New Comment Alert", message, payload)
                previous_comments[issue_iid] = current_comments

        # Closed issue
        for issue_iid, issue in previous_issues.items():
            if issue_iid not in current_issues_map:
                message = f"[GitLab Issue Closed] '{issue.title}' (ID: {issue.iid}) has been closed.\nURL: {issue.web_url}"
                payload = {"title": issue.title, "updated_at": issue.updated_at, "issue_id": issue.iid, "status": "close"}
                notify(config, "GitLab Issue Closed Alert", message, payload)

        previous_issues = current_issues_map

    elif platform == 'github':
        repo = get_github_client()
        current_issues = list(repo.get_issues(state='all'))
        current_issues_map = {issue.number: issue for issue in current_issues}

        if not previous_issues:
            previous_issues = current_issues_map
            for issue in current_issues:
                previous_comments[issue.number] = list(issue.get_comments())
            print("Initial GitHub state loaded. Monitoring will begin.")
            return

        # Check for changes in GitHub issues
        for issue_number, issue in current_issues_map.items():
            prev_issue = previous_issues.get(issue_number)
            
            # New issue
            if not prev_issue:
                message = f"[New GitHub Issue] {issue.title} (ID: #{issue.number})\nURL: {issue.html_url}"
                payload = {"title": issue.title, "created_at": issue.created_at.isoformat(), "issue_id": issue.number, "status": "등록"}
                notify(config, "GitHub New Issue Alert", message, payload)
            # Title or state change
            elif issue.state != prev_issue.state or issue.title != prev_issue.title:
                message = f"[GitHub Issue Change] '{prev_issue.title}' (ID: #{issue.number}) updated.\nURL: {issue.html_url}"
                payload = {"title": issue.title, "updated_at": issue.updated_at.isoformat(), "issue_id": issue.number, "status": "수정" if issue.state != 'closed' else 'close'}
                notify(config, "GitHub Issue Update Alert", message, payload)
            
            # New comment
            current_comments = list(issue.get_comments())
            if len(current_comments) > len(previous_comments.get(issue_number, [])):
                new_comment = current_comments[-1]
                message = f"[GitHub New Comment] New comment on '{issue.title}' (ID: #{issue.number}) by {new_comment.user.login}\nURL: {new_comment.html_url}"
                payload = {"title": issue.title, "comment_created_at": new_comment.created_at.isoformat(), "issue_id": issue.number, "status": "comment 등록"}
                notify(config, "GitHub New Comment Alert", message, payload)
                previous_comments[issue_number] = current_comments

        # Closed issue
        for issue_number, issue in previous_issues.items():
            if issue_number not in current_issues_map:
                message = f"[GitHub Issue Closed] '{issue.title}' (ID: #{issue.number}) has been closed.\nURL: {issue.html_url}"
                payload = {"title": issue.title, "updated_at": issue.updated_at.isoformat(), "issue_id": issue.number, "status": "close"}
                notify(config, "GitHub Issue Closed Alert", message, payload)

        previous_issues = current_issues_map
        
    else:
        print("Invalid platform specified in config.ini. Please choose 'gitlab' or 'github'.")

# Schedule the job to run every minute
schedule.every(1).minutes.do(check_changes)

print("Issue monitor started. Checking every 1 minute...")

while True:
    schedule.run_pending()
    time.sleep(1)