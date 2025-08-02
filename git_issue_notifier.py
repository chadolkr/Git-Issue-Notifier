import configparser
import schedule
import time
import requests
import gitlab
from github import Github
from slack_sdk.webhook import WebhookClient
import smtplib
from email.mime.text import MIMEText
import logging
import logging.handlers
import os
import gzip
from datetime import datetime
import sys

# 로그 파일을 압축하는 함수
def namer(name):
    return name + '.gz'

def rotator(source, dest):
    with open(source, 'rb') as f_in, gzip.open(dest, 'wb') as f_out:
        f_out.writelines(f_in)
    os.remove(source)

# 설정 파일이 없거나 읽을 수 없을 때 발생하는 치명적인 오류를 처리하는 함수
def _read_config(config_file):
    """
    설정 파일을 읽고, 오류 발생 시 프로그램을 종료합니다.
    이 함수는 프로그램 시작 시 단 한 번만 호출됩니다.
    """
    config = configparser.ConfigParser()
    if not os.path.exists(config_file):
        print(f"CRITICAL: '{config_file}' 설정 파일을 찾을 수 없습니다. config.ini.sec 파일을 생성하고 설정을 입력해주세요. 프로그램이 종료됩니다.")
        sys.exit(1)
    
    try:
        config.read(config_file, encoding='utf-8')
    except UnicodeDecodeError:
        print(f"CRITICAL: config.ini 파일을 utf-8로 읽는 데 실패했습니다. 파일 인코딩을 확인해주세요. 프로그램이 종료됩니다.")
        sys.exit(1)
    
    return config

def setup_logging(config):
    """
    로그 설정 및 로그 파일 경로를 동적으로 결정합니다.
    configparser 객체를 인수로 받도록 수정하여 중복 읽기를 방지합니다.
    """
    log_path = config.get('logging', 'log_path', fallback=None)
    
    if log_path:
        log_dir = os.path.abspath(log_path)
    else:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
        
    os.makedirs(log_dir, exist_ok=True)
    
    log_file = os.path.join(log_dir, 'issue_monitor.log')
    LOG_BACKUP_COUNT = 7

    logger = logging.getLogger('issue_monitor')
    logger.setLevel(logging.INFO)

    handler = logging.handlers.TimedRotatingFileHandler(
        log_file,
        when='midnight',
        interval=1,
        backupCount=LOG_BACKUP_COUNT,
        encoding='utf-8'
    )
    handler.suffix = '%Y-%m-%d'
    handler.rotator = rotator
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    return logger

class IssueMonitor:
    def __init__(self, config_file='config.ini.sec'):
        self.config = _read_config(config_file)
        
        self.logger = setup_logging(self.config)

        self.platform = self.config.get('general', 'platform', fallback=None)
        
        if not self.platform or self.platform not in ['gitlab', 'github']:
            self.logger.critical(f"config.ini 파일의 [general] 섹션에 'platform' 설정이 없거나 유효하지 않습니다. "
                                f"'gitlab' 또는 'github' 중 하나를 선택하여 설정해주세요. 프로그램이 종료됩니다.")
            sys.exit(1)

        self.previous_issues = {}
        self.previous_comments = {}
        self.client = self._get_client()
        
        if not self.client:
            self.logger.critical("API 클라이언트 초기화에 실패하여 프로그램을 시작할 수 없습니다. 프로그램이 종료됩니다.")
            sys.exit(1)

    def _get_client(self):
        """플랫폼에 맞는 클라이언트를 초기화하고 사용자 친화적인 에러 메시지를 제공합니다."""
        try:
            if self.platform == 'gitlab':
                private_token = self.config['gitlab']['private_token']
                server_url = self.config['gitlab']['server_url']
                project_id = self.config['gitlab']['project_id']
                gl = gitlab.Gitlab(server_url, private_token=private_token)
                project = gl.projects.get(project_id)
                self.logger.info("GitLab 클라이언트가 성공적으로 초기화되었습니다.")
                return project
            
            elif self.platform == 'github':
                access_token = self.config['github']['access_token']
                repo_name = self.config['github']['repo_name']
                g = Github(access_token)
                repo = g.get_repo(repo_name)
                self.logger.info("GitHub 클라이언트가 성공적으로 초기화되었습니다.")
                return repo
        
        except KeyError as e:
            self.logger.critical(f"config.ini 파일의 '{self.platform}' 섹션에서 필수 설정 '{e.args[0]}'을(를) 찾을 수 없습니다. "
                                 f"설정을 확인해주세요. 프로그램이 종료됩니다.")
            sys.exit(1)
        except (gitlab.exceptions.GitlabError, requests.exceptions.HTTPError) as e:
            self.logger.critical(f"API 오류: {e}. 개인 액세스 토큰이 만료되었거나, 권한이 없거나, 프로젝트 ID가 잘못되었을 수 있습니다. "
                                 f"설정을 확인해주세요. 프로그램이 종료됩니다.")
            sys.exit(1)
        except Exception as e:
            self.logger.critical(f"클라이언트 초기화 중 알 수 없는 오류 발생: {e}. 프로그램이 종료됩니다.")
            sys.exit(1)

    def _send_notification(self, subject, message, payload):
        """
        설정에 따라 알림을 발송합니다.
        Slack 알림의 경우 메시지를 일정 길이로 요약하여 전송합니다.
        """
        notification_type = self.config.get('notification', 'type', fallback=None)
        if not notification_type:
            self.logger.warning("알림 타입이 설정되지 않았습니다. 알림을 발송하지 않습니다.")
            return

        def truncate_by_lines(text, max_lines=3):
            """텍스트를 최대 지정된 줄 수로 자르고 '...'을 추가합니다."""
            if not text:
                return ''
            
            lines = text.splitlines()
            if len(lines) > max_lines:
                return '\n'.join(lines[:max_lines]) + '...'
            return text

        try:
            if notification_type == 'slack':
                webhook_url = self.config['slack']['webhook_url']
                webhook = WebhookClient(webhook_url)

                # payload에서 제목, 본문, 댓글 내용 추출
                title = payload.get('title', '')
                content = payload.get('content', '')
                comment = payload.get('comment', '')
                url = payload.get('url', '')

                # 텍스트를 줄 단위로 요약
                title_summary = truncate_by_lines(title)
                content_summary = truncate_by_lines(content)
                comment_summary = truncate_by_lines(comment)

                # 상태에 따라 Slack 메시지 포맷 재구성
                status = payload.get('status', '수정')
                if status == '등록':
                    slack_message = f"**[새로운 이슈 등록]**\n제목: {title_summary}\n내용: {content_summary}\nURL: {url}"
                elif status == '수정':
                    slack_message = f"**[이슈 수정]**\n제목: {title_summary}\n내용: {content_summary}\nURL: {url}"
                elif status == 'reopen':
                    slack_message = f"**[이슈 재오픈]**\n제목: {title_summary}\n내용: {content_summary}\nURL: {url}"
                elif status == 'close':
                    slack_message = f"**[이슈 종료]**\n제목: {title_summary}\nURL: {url}"
                elif status == 'comment 등록':
                    slack_message = f"**[새로운 댓글]**\n이슈 제목: {title_summary}\n내용: {comment_summary}\nURL: {url}"
                else:
                    slack_message = message  # 기본 메시지 사용

                response = webhook.send(text=slack_message)
                self.logger.info(f"Slack 알림이 성공적으로 전송되었습니다. 상태 코드: {response.status_code}")
            
            elif notification_type == 'mail':
                smtp_server = self.config['mail']['smtp_server']
                smtp_port = int(self.config['mail']['smtp_port'])
                smtp_user = self.config['mail']['smtp_user']
                smtp_password = self.config['mail']['smtp_password']
                recipient_email = self.config['mail']['recipient_email']
                
                msg = MIMEText(message)
                msg['Subject'] = subject
                msg['From'] = smtp_user
                msg['To'] = recipient_email
                
                with smtplib.SMTP(smtp_server, smtp_port) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_password)
                    server.send_message(msg)
                self.logger.info("Email 알림이 성공적으로 전송되었습니다.")
            
            elif notification_type == 'api':
                url = self.config['api']['url']
                bearer_token = self.config['api']['bearer_token']
                headers = {'Authorization': f'Bearer {bearer_token}', 'Content-Type': 'application/json'}
                response = requests.post(url, json=payload, headers=headers)
                self.logger.info(f"API 알림이 성공적으로 전송되었습니다. 상태 코드: {response.status_code}")
            
            else:
                self.logger.warning(f"유효하지 않은 알림 타입 '{notification_type}'입니다.")
        
        except KeyError as e:
            self.logger.error(f"알림 타입 '{notification_type}'에 필요한 설정 키 '{e.args[0]}'가 누락되었습니다.")
        except Exception as e:
            self.logger.error(f"{notification_type} 알림 전송 중 오류 발생: {e}")

    def _check_gitlab(self):
        """GitLab 이슈 및 댓글 변경 사항을 모니터링합니다."""
        try:
            self.logger.info("GitLab 모니터링 로직 실행 중...")
            project = self.client
            issues = project.issues.list(state='all', get_all=True)
            current_issues_map = {issue.iid: issue for issue in issues}
            
            if not self.previous_issues:
                self.previous_issues = current_issues_map
                for issue in issues:
                    self.previous_comments[issue.iid] = project.issues.get(issue.iid).notes.list()
                self.logger.info("초기 GitLab 상태를 로드했습니다. 이제부터 변경 사항을 모니터링합니다.")
                return

            for issue_iid, issue in current_issues_map.items():
                prev_issue = self.previous_issues.get(issue_iid)
                
                if not prev_issue:
                    message = f"[New GitLab Issue] {issue.title} (ID: {issue.iid})\nURL: {issue.web_url}"
                    payload = {"title": issue.title, "content": issue.description, "created_at": issue.created_at, "issue_id": issue.iid, "status": "등록", "url": issue.web_url}
                    self._send_notification("GitLab New Issue Alert", message, payload)
                elif issue.state != prev_issue.state or issue.title != prev_issue.title:
                    if prev_issue.state == 'closed' and issue.state == 'opened':
                        message = f"[GitLab Issue Reopened] '{issue.title}' (ID: {issue.iid}) has been reopened.\nURL: {issue.web_url}"
                        payload = {"title": issue.title, "content": issue.description, "updated_at": issue.updated_at, "issue_id": issue.iid, "status": "reopen", "url": issue.web_url}
                        self._send_notification("GitLab Issue Reopened Alert", message, payload)
                    else:
                        message = f"[GitLab Issue Change] '{prev_issue.title}' (ID: {issue.iid}) updated.\nURL: {issue.web_url}"
                        payload = {"title": issue.title, "content": issue.description, "updated_at": issue.updated_at, "issue_id": issue.iid, "status": "수정" if issue.state != 'closed' else 'close', "url": issue.web_url}
                        self._send_notification("GitLab Issue Update Alert", message, payload)
                
                current_comments = project.issues.get(issue.iid).notes.list()
                if len(current_comments) > len(self.previous_comments.get(issue_iid, [])):
                    last_comment = current_comments[-1]
                    message = f"[GitLab New Comment] New comment on '{issue.title}' (ID: {issue.iid}).\nURL: {issue.web_url}"
                    payload = {"title": issue.title, "comment": last_comment.body, "comment_created_at": last_comment.created_at, "issue_id": issue.iid, "status": "comment 등록", "url": issue.web_url}
                    self._send_notification("GitLab New Comment Alert", message, payload)
                    self.previous_comments[issue_iid] = current_comments

            for issue_iid, issue in self.previous_issues.items():
                if issue_iid not in current_issues_map:
                    message = f"[GitLab Issue Closed] '{issue.title}' (ID: {issue.iid}) has been closed.\nURL: {issue.web_url}"
                    payload = {"title": issue.title, "updated_at": issue.updated_at, "issue_id": issue.iid, "status": "close", "url": issue.web_url}
                    self._send_notification("GitLab Issue Closed Alert", message, payload)
            
            self.previous_issues = current_issues_map
            self.logger.info("GitLab 변경 사항 확인 완료.")
        except Exception as e:
            self.logger.error(f"GitLab 모니터링 중 오류 발생: {e}")

    def _check_github(self):
        """GitHub 이슈 및 댓글 변경 사항을 모니터링합니다."""
        try:
            self.logger.info("GitHub 모니터링 로직 실행 중...")
            repo = self.client
            current_issues = list(repo.get_issues(state='all'))
            current_issues_map = {issue.number: issue for issue in current_issues}

            if not self.previous_issues:
                self.previous_issues = current_issues_map
                for issue in current_issues:
                    self.previous_comments[issue.number] = list(issue.get_comments())
                self.logger.info("초기 GitHub 상태를 로드했습니다. 이제부터 변경 사항을 모니터링합니다.")
                return

            for issue_number, issue in current_issues_map.items():
                prev_issue = self.previous_issues.get(issue_number)
                
                if not prev_issue:
                    message = f"[New GitHub Issue] {issue.title} (ID: #{issue.number})\nURL: {issue.html_url}"
                    payload = {"title": issue.title, "content": issue.body, "created_at": issue.created_at.isoformat(), "issue_id": issue.number, "status": "등록", "url": issue.html_url}
                    self._send_notification("GitHub New Issue Alert", message, payload)
                elif issue.state != prev_issue.state or issue.title != prev_issue.title:
                    if prev_issue.state == 'closed' and issue.state == 'open':
                        message = f"[GitHub Issue Reopened] '{issue.title}' (ID: #{issue.number}) has been reopened.\nURL: {issue.html_url}"
                        payload = {"title": issue.title, "content": issue.body, "updated_at": issue.updated_at.isoformat(), "issue_id": issue.number, "status": "reopen", "url": issue.html_url}
                        self._send_notification("GitHub Issue Reopened Alert", message, payload)
                    else:
                        message = f"[GitHub Issue Change] '{prev_issue.title}' (ID: #{issue.number}) updated.\nURL: {issue.html_url}"
                        payload = {"title": issue.title, "content": issue.body, "updated_at": issue.updated_at.isoformat(), "issue_id": issue.number, "status": "수정" if issue.state != 'closed' else 'close', "url": issue.html_url}
                        self._send_notification("GitHub Issue Update Alert", message, payload)
                
                current_comments = list(issue.get_comments())
                if len(current_comments) > len(self.previous_comments.get(issue_number, [])):
                    new_comment = current_comments[-1]
                    message = f"[GitHub New Comment] New comment on '{issue.title}' (ID: #{issue.number}) by {new_comment.user.login}\nURL: {new_comment.html_url}"
                    payload = {"title": issue.title, "comment": new_comment.body, "comment_created_at": new_comment.created_at.isoformat(), "issue_id": issue.number, "status": "comment 등록", "url": new_comment.html_url}
                    self._send_notification("GitHub New Comment Alert", message, payload)
                    self.previous_comments[issue_number] = current_comments

            for issue_number, issue in self.previous_issues.items():
                if issue_number not in current_issues_map:
                    message = f"[GitHub Issue Closed] '{issue.title}' (ID: #{issue.number}) has been closed.\nURL: {issue.html_url}"
                    payload = {"title": issue.title, "updated_at": issue.updated_at.isoformat(), "issue_id": issue.number, "status": "close", "url": issue.html_url}
                    self._send_notification("GitHub Issue Closed Alert", message, payload)
            
            self.previous_issues = current_issues_map
            self.logger.info("GitHub 변경 사항 확인 완료.")
        except Exception as e:
            self.logger.error(f"GitHub 모니터링 중 오류 발생: {e}")

    def run_check(self):
        """구성된 플랫폼에 대해 모니터링을 실행합니다."""
        self.logger.info(f"[{self.platform}] 변경 사항 확인 시작...")
        if self.platform == 'gitlab':
            self._check_gitlab()
        elif self.platform == 'github':
            self._check_github()
        self.logger.info(f"[{self.platform}] 변경 사항 확인 종료.")

    def start(self):
        """예약된 모니터링 작업을 시작합니다."""
        self.run_check()

        self.logger.info(f"이슈 모니터가 {self.platform} 플랫폼에 대해 시작되었습니다. 1분마다 확인합니다...")
        schedule.every(1).minutes.do(self.run_check)
        
        while True:
            schedule.run_pending()
            time.sleep(1)

if __name__ == "__main__":
    monitor = IssueMonitor()
    monitor.start()