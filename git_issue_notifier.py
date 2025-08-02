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

# 로그 파일 경로 설정
LOG_FILE = 'issue_monitor.log'
LOG_BACKUP_COUNT = 7  # 7일치 로그 보관

# TimedRotatingFileHandler를 위한 custom rotator 함수 (로그 압축 기능)
def namer(name):
    return name + '.gz'

def rotator(source, dest):
    with open(source, 'rb') as f_in, gzip.open(dest, 'wb') as f_out:
        f_out.writelines(f_in)
    os.remove(source)

# 로깅 설정
logger = logging.getLogger('issue_monitor')
logger.setLevel(logging.INFO)

# TimedRotatingFileHandler를 사용하여 일별 로그 파일 생성 및 보관
handler = logging.handlers.TimedRotatingFileHandler(
    LOG_FILE,
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

# 콘솔에도 로그 출력 (선택 사항)
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

class IssueMonitor:
    def __init__(self, config_file='config.ini'):
        self.config = self._get_config(config_file)
        self.platform = self.config.get('general', 'platform', fallback=None)
        
        # 플랫폼이 설정되지 않았거나 유효하지 않으면 프로그램 즉시 종료
        if not self.platform or self.platform not in ['gitlab', 'github']:
            logger.critical(f"config.ini 파일의 [general] 섹션에 'platform' 설정이 없거나 유효하지 않습니다. "
                            f"'gitlab' 또는 'github' 중 하나를 선택하여 설정해주세요. 프로그램이 종료됩니다.")
            sys.exit(1)

        self.previous_issues = {}
        self.previous_comments = {}
        self.client = self._get_client()
        
        # 클라이언트 초기화에 실패했으면 프로그램 즉시 종료
        if not self.client:
            logger.critical("API 클라이언트 초기화에 실패하여 프로그램을 시작할 수 없습니다. 프로그램이 종료됩니다.")
            sys.exit(1)

    def _get_config(self, config_file):
        """설정 파일에서 설정을 읽고, 사용자에게 친화적인 에러 메시지를 제공합니다."""
        config = configparser.ConfigParser()
        if not os.path.exists(config_file):
            logger.critical(f"'{config_file}' 설정 파일을 찾을 수 없습니다. config.ini 파일을 생성하고 설정을 입력해주세요. 프로그램이 종료됩니다.")
            sys.exit(1)
        
        config.read(config_file)
        return config

    def _get_client(self):
        """플랫폼에 맞는 클라이언트를 초기화하고 사용자 친화적인 에러 메시지를 제공합니다."""
        try:
            if self.platform == 'gitlab':
                private_token = self.config['gitlab']['private_token']
                server_url = self.config['gitlab']['server_url']
                project_id = self.config['gitlab']['project_id']
                gl = gitlab.Gitlab(server_url, private_token=private_token)
                project = gl.projects.get(project_id)
                logger.info("GitLab 클라이언트가 성공적으로 초기화되었습니다.")
                return project
            
            elif self.platform == 'github':
                access_token = self.config['github']['access_token']
                repo_name = self.config['github']['repo_name']
                g = Github(access_token)
                repo = g.get_repo(repo_name)
                logger.info("GitHub 클라이언트가 성공적으로 초기화되었습니다.")
                return repo
        
        except KeyError as e:
            logger.critical(f"config.ini 파일의 '{self.platform}' 섹션에서 필수 설정 '{e.args[0]}'을(를) 찾을 수 없습니다. "
                            f"설정을 확인해주세요. 프로그램이 종료됩니다.")
            sys.exit(1)
        except (gitlab.exceptions.GitlabError, requests.exceptions.HTTPError) as e:
            logger.critical(f"GitLab API 오류: {e}. 개인 액세스 토큰이 만료되었거나, 권한이 없거나, 프로젝트 ID가 잘못되었을 수 있습니다. "
                            f"설정을 확인해주세요. 프로그램이 종료됩니다.")
            sys.exit(1)
        except Exception as e:
            logger.critical(f"클라이언트 초기화 중 알 수 없는 오류 발생: {e}. 프로그램이 종료됩니다.")
            sys.exit(1)

    def _send_notification(self, subject, message, payload):
        """설정에 따라 알림을 발송합니다."""
        notification_type = self.config.get('notification', 'type', fallback=None)
        if not notification_type:
            logger.warning("알림 타입이 설정되지 않았습니다. 알림을 발송하지 않습니다.")
            return

        try:
            if notification_type == 'slack':
                webhook_url = self.config['slack']['webhook_url']
                webhook = WebhookClient(webhook_url)
                response = webhook.send(text=message)
                logger.info(f"Slack 알림이 성공적으로 전송되었습니다. 상태 코드: {response.status_code}")
            
            elif notification_type == 'mail':
                # 기존 이메일 알림 로직은 동일
                # ...
                pass
            
            elif notification_type == 'api':
                # 기존 API 알림 로직은 동일
                # ...
                pass

            else:
                logger.warning(f"유효하지 않은 알림 타입 '{notification_type}'입니다.")
        
        except KeyError as e:
            logger.error(f"알림 타입 '{notification_type}'에 필요한 설정 키 '{e.args[0]}'가 누락되었습니다.")
        except Exception as e:
            logger.error(f"{notification_type} 알림 전송 중 오류 발생: {e}")

    def _check_gitlab(self):
        """GitLab 이슈 및 댓글 변경 사항을 모니터링합니다."""
        try:
            # ... 기존 GitLab 모니터링 로직 (생략) ...
            logger.info("GitLab 모니터링 로직 실행 중...")
            project = self.client
            issues = project.issues.list(state='all', get_all=True)
            current_issues_map = {issue.iid: issue for issue in issues}
            
            if not self.previous_issues:
                self.previous_issues = current_issues_map
                for issue in issues:
                    self.previous_comments[issue.iid] = project.issues.get(issue.iid).notes.list()
                logger.info("초기 GitLab 상태를 로드했습니다. 이제부터 변경 사항을 모니터링합니다.")
                return

            # ... 변경 감지 및 알림 로직 ...
            for issue_iid, issue in current_issues_map.items():
                prev_issue = self.previous_issues.get(issue_iid)
                
                # New issue
                if not prev_issue:
                    message = f"[New GitLab Issue] {issue.title} (ID: {issue.iid})\nURL: {issue.web_url}"
                    payload = {"title": issue.title, "created_at": issue.created_at, "issue_id": issue.iid, "status": "등록"}
                    self._send_notification("GitLab New Issue Alert", message, payload)
                # Title or state change
                elif issue.state != prev_issue.state or issue.title != prev_issue.title:
                    message = f"[GitLab Issue Change] '{prev_issue.title}' (ID: {issue.iid}) updated.\nURL: {issue.web_url}"
                    payload = {"title": issue.title, "updated_at": issue.updated_at, "issue_id": issue.iid, "status": "수정" if issue.state != 'closed' else 'close'}
                    self._send_notification("GitLab Issue Update Alert", message, payload)
                
                # New comment
                current_comments = project.issues.get(issue.iid).notes.list()
                if len(current_comments) > len(self.previous_comments.get(issue_iid, [])):
                    message = f"[GitLab New Comment] New comment on '{issue.title}' (ID: {issue.iid}).\nURL: {issue.web_url}"
                    payload = {"title": issue.title, "comment_created_at": current_comments[-1].created_at, "issue_id": issue.iid, "status": "comment 등록"}
                    self._send_notification("GitLab New Comment Alert", message, payload)
                    self.previous_comments[issue_iid] = current_comments

            # Closed issue
            for issue_iid, issue in self.previous_issues.items():
                if issue_iid not in current_issues_map:
                    message = f"[GitLab Issue Closed] '{issue.title}' (ID: {issue.iid}) has been closed.\nURL: {issue.web_url}"
                    payload = {"title": issue.title, "updated_at": issue.updated_at, "issue_id": issue.iid, "status": "close"}
                    self._send_notification("GitLab Issue Closed Alert", message, payload)
            
            self.previous_issues = current_issues_map
            logger.info("GitLab 변경 사항 확인 완료.")
        except Exception as e:
            logger.error(f"GitLab 모니터링 중 치명적인 오류 발생: {e}")


    def _check_github(self):
        """GitHub 이슈 및 댓글 변경 사항을 모니터링합니다."""
        try:
            # ... 기존 GitHub 모니터링 로직 (생략) ...
            logger.info("GitHub 모니터링 로직 실행 중...")
            repo = self.client
            current_issues = list(repo.get_issues(state='all'))
            current_issues_map = {issue.number: issue for issue in current_issues}

            if not self.previous_issues:
                self.previous_issues = current_issues_map
                for issue in current_issues:
                    self.previous_comments[issue.number] = list(issue.get_comments())
                logger.info("초기 GitHub 상태를 로드했습니다. 이제부터 변경 사항을 모니터링합니다.")
                return

            # ... 변경 감지 및 알림 로직 ...
            for issue_number, issue in current_issues_map.items():
                prev_issue = self.previous_issues.get(issue_number)
                
                # New issue
                if not prev_issue:
                    message = f"[New GitHub Issue] {issue.title} (ID: #{issue.number})\nURL: {issue.html_url}"
                    payload = {"title": issue.title, "created_at": issue.created_at.isoformat(), "issue_id": issue.number, "status": "등록"}
                    self._send_notification("GitHub New Issue Alert", message, payload)
                # Title or state change
                elif issue.state != prev_issue.state or issue.title != prev_issue.title:
                    message = f"[GitHub Issue Change] '{prev_issue.title}' (ID: #{issue.number}) updated.\nURL: {issue.html_url}"
                    payload = {"title": issue.title, "updated_at": issue.updated_at.isoformat(), "issue_id": issue.number, "status": "수정" if issue.state != 'closed' else 'close'}
                    self._send_notification("GitHub Issue Update Alert", message, payload)
                
                # New comment
                current_comments = list(issue.get_comments())
                if len(current_comments) > len(self.previous_comments.get(issue_number, [])):
                    new_comment = current_comments[-1]
                    message = f"[GitHub New Comment] New comment on '{issue.title}' (ID: #{issue.number}) by {new_comment.user.login}\nURL: {new_comment.html_url}"
                    payload = {"title": issue.title, "comment_created_at": new_comment.created_at.isoformat(), "issue_id": issue.number, "status": "comment 등록"}
                    self._send_notification("GitHub New Comment Alert", message, payload)
                    self.previous_comments[issue_number] = current_comments

            # Closed issue
            for issue_number, issue in self.previous_issues.items():
                if issue_number not in current_issues_map:
                    message = f"[GitHub Issue Closed] '{issue.title}' (ID: #{issue.number}) has been closed.\nURL: {issue.html_url}"
                    payload = {"title": issue.title, "updated_at": issue.updated_at.isoformat(), "issue_id": issue.number, "status": "close"}
                    self._send_notification("GitHub Issue Closed Alert", message, payload)
            
            self.previous_issues = current_issues_map
            logger.info("GitHub 변경 사항 확인 완료.")
        except Exception as e:
            logger.error(f"GitHub 모니터링 중 치명적인 오류 발생: {e}")


    def run_check(self):
        """구성된 플랫폼에 대해 모니터링을 실행합니다."""
        logger.info(f"[{self.platform}] 변경 사항 확인 시작...")
        if self.platform == 'gitlab':
            self._check_gitlab()
        elif self.platform == 'github':
            self._check_github()
        logger.info(f"[{self.platform}] 변경 사항 확인 종료.")

    def start(self):
        """예약된 모니터링 작업을 시작합니다."""
        logger.info(f"이슈 모니터가 {self.platform} 플랫폼에 대해 시작되었습니다. 1분마다 확인합니다...")
        schedule.every(1).minutes.do(self.run_check)
        
        while True:
            schedule.run_pending()
            time.sleep(1)

if __name__ == "__main__":
    monitor = IssueMonitor()
    monitor.start()