import asyncio
import email
import imaplib
import json
import logging
import os
import re
import sys
from datetime import datetime
from email.header import decode_header
from email.utils import parsedate_tz, mktime_tz
from typing import List, Dict, Any, Optional
from collections import defaultdict, Counter

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

MAX_BODY_LENGTH = 500  # Reduced from 2000 to limit LLM input
MAX_TOTAL_EMAILS = 50  # Maximum total emails to prevent overload
DEFAULT_TIMEOUT = 30.0

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993

EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")


class EmailMessage(BaseModel):
    uid: str
    subject: str
    sender: str
    date: str
    body: str
    has_attachments: bool = False
    is_read: bool = False
    date_info: Optional[Dict[str, Any]] = None

class EmailSummary(BaseModel):
    category: str
    priority: str
    summary: str
    action_required: bool = False
    time_urgency: str = "normal"
    sentiment: str = "neutral"
    topics: List[str] = []
    meeting_info: Optional[Dict[str, Any]] = None
    deadline_info: Optional[Dict[str, Any]] = None

class EmailGroup(BaseModel):
    category: str
    emails: List[Dict[str, Any]]
    common_summary: str
    total_count: int
    priority_distribution: Dict[str, int]

class EmailStats(BaseModel):
    total_emails: int
    unread_emails: int
    read_emails: int
    emails_with_attachments: int
    sender_distribution: Dict[str, int]
    domain_distribution: Dict[str, int]
    date_distribution: Dict[str, int]
    time_distribution: Dict[str, int]
    email_types: Dict[str, int]
    keyword_matches: Dict[str, int]
    recommendations: List[str]

class KeywordMatch(BaseModel):
    keyword: str
    email_uid: str
    subject: str
    sender: str
    date: str
    match_location: str  # "subject", "body", "both"
    match_count: int
    context: str  # snippet of text around the match

class GmailClient:
    
    def __init__(self):
        self.mail = None
        self.connected = False
    
    async def connect(self) -> bool:
        try:
            self.mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            result = self.mail.login(EMAIL_ADDRESS, APP_PASSWORD)
            
            if result[0] == 'OK':
                self.mail.select('inbox')
                self.connected = True
                logger.info("Connected to Gmail successfully")
                return True
            else:
                logger.error(f"Gmail login failed: {result}")
                return False
                
        except Exception as e:
            logger.error(f"Gmail connection error: {e}")
            return False
    
    def disconnect(self):
        if self.mail and self.connected:
            try:
                self.mail.close()
                self.mail.logout()
                self.connected = False
                logger.info("Disconnected from Gmail")
            except Exception as e:
                logger.error(f"Error disconnecting from Gmail: {e}")
    
    @staticmethod
    def decode_mime_header(header_value: str) -> str:
        if not header_value:
            return ""
        
        try:
            decoded_parts = decode_header(header_value)
            decoded_string = ""
            for part, encoding in decoded_parts:
                if isinstance(part, bytes):
                    decoded_string += part.decode(encoding or 'utf-8')
                else:
                    decoded_string += part
            return decoded_string
        except Exception as e:
            logger.error(f"Error decoding header '{header_value}': {e}")
            return str(header_value)
    
    @staticmethod
    def extract_email_body(msg) -> str:
        try:
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            return payload.decode('utf-8', errors='ignore').strip()
            else:
                if msg.get_content_type() == "text/plain":
                    payload = msg.get_payload(decode=True)
                    if payload:
                        return payload.decode('utf-8', errors='ignore').strip()
                        
        except Exception as e:
            logger.error(f"Error extracting email body: {e}")
            return "Error reading email body"
            
        return ""
    
    @staticmethod
    def _parse_email_date(date_str: str) -> Dict[str, Any]:
        result = {
            "formatted_date": date_str,
            "timestamp": None,
            "datetime_obj": None,
            "timezone": None,
            "age_hours": None,
            "age_days": None,
            "is_recent": False,
            "is_today": False,
            "is_weekend": False,
            "time_period": "unknown"
        }
        
        try:
            date_tuple = parsedate_tz(date_str)
            if date_tuple:
                timestamp = mktime_tz(date_tuple)
                dt = datetime.fromtimestamp(timestamp)
                now = datetime.now()
                
                result["formatted_date"] = dt.strftime('%Y-%m-%d %H:%M:%S')
                result["timestamp"] = timestamp
                result["datetime_obj"] = dt
                
                if date_tuple[9] is not None:
                    tz_offset = date_tuple[9]
                    result["timezone"] = f"UTC{'+' if tz_offset >= 0 else ''}{tz_offset // 3600:02d}:{(abs(tz_offset) % 3600) // 60:02d}"
                
                age_delta = now - dt
                result["age_hours"] = age_delta.total_seconds() / 3600
                result["age_days"] = age_delta.days
                
                result["is_recent"] = age_delta.total_seconds() < 24 * 3600
                result["is_today"] = dt.date() == now.date()
                result["is_weekend"] = dt.weekday() >= 5
                
                hour = dt.hour
                if 5 <= hour < 12:
                    result["time_period"] = "morning"
                elif 12 <= hour < 17:
                    result["time_period"] = "afternoon"
                elif 17 <= hour < 21:
                    result["time_period"] = "evening"
                else:
                    result["time_period"] = "night"
                    
        except Exception as e:
            logger.debug(f"Error parsing date '{date_str}': {e}")
        
        return result
    
    @staticmethod
    def _has_attachments(msg) -> bool:
        if not msg.is_multipart():
            return False
        
        for part in msg.walk():
            if part.get_content_disposition() == 'attachment':
                return True
        return False
    
    def extract_sender_domain(self, sender: str) -> str:
        try:
            if '<' in sender and '>' in sender:
                email_part = sender.split('<')[1].split('>')[0]
            else:
                email_part = sender
            
            if '@' in email_part:
                return email_part.split('@')[1].lower()
            return "unknown"
        except:
            return "unknown"
    
    def search_keywords_in_email(self, keywords: List[str], subject: str, body: str) -> Dict[str, Any]:
        matches = {}
        
        for keyword in keywords:
            keyword_lower = keyword.lower()
            subject_lower = subject.lower()
            body_lower = body.lower()
            
            subject_matches = len(re.findall(re.escape(keyword_lower), subject_lower))
            body_matches = len(re.findall(re.escape(keyword_lower), body_lower))
            total_matches = subject_matches + body_matches
            
            if total_matches > 0:
                match_location = []
                if subject_matches > 0:
                    match_location.append("subject")
                if body_matches > 0:
                    match_location.append("body")
                
                context = ""
                if body_matches > 0:
                    pattern = re.compile(f".{{0,50}}{re.escape(keyword_lower)}.{{0,50}}", re.IGNORECASE)
                    match = pattern.search(body)
                    if match:
                        context = match.group(0).strip()
                
                matches[keyword] = {
                    "total_count": total_matches,
                    "subject_count": subject_matches,
                    "body_count": body_matches,
                    "location": ", ".join(match_location),
                    "context": context
                }
        
        return matches
    
    def classify_email_type(self, subject: str, sender: str, body: str = "") -> str:
        subject_lower = subject.lower()
        sender_lower = sender.lower()
        
        marketing_keywords = ['sale', 'discount', 'offer', 'promotion', 'deal', 'newsletter', 
                             'subscribe', 'unsubscribe', 'marketing', 'campaign']
        notification_keywords = ['notification', 'alert', 'reminder', 'update', 'report',
                               'status', 'confirmation', 'receipt', 'invoice']
        support_keywords = ['support', 'help', 'service', 'ticket', 'issue', 'problem',
                           'assistance', 'customer']
        work_keywords = ['meeting', 'project', 'deadline', 'report', 'proposal', 'contract',
                        'business', 'work', 'office', 'team']
        social_keywords = ['hi', 'hello', 'thank you', 'thanks', 'regards', 'personal',
                          'family', 'friend']
        
        if any(domain in sender_lower for domain in ['noreply', 'no-reply', 'donotreply']):
            return "automated"
        
        if any(keyword in subject_lower for keyword in marketing_keywords):
            return "marketing"
        elif any(keyword in subject_lower for keyword in notification_keywords):
            return "notification"
        elif any(keyword in subject_lower for keyword in support_keywords):
            return "support"
        elif any(keyword in subject_lower for keyword in work_keywords):
            return "work"
        elif any(keyword in subject_lower for keyword in social_keywords):
            return "personal"
        else:
            return "other"
    
    async def fetch_unread_emails(self, count: int = 50) -> List[EmailMessage]:
        if not self.connected:
            if not await self.connect():
                return []
        
        emails = []
        
        try:
            status, messages = self.mail.search(None, 'UNSEEN')
            
            if status != 'OK':
                logger.error("Failed to search unread emails")
                return []
            
            message_uids = messages[0].split()
            
            if not message_uids:
                logger.info("No unread emails found")
                return []
            
            recent_uids = message_uids[-count:] if len(message_uids) >= count else message_uids
            recent_uids.reverse()
            
            for uid in recent_uids:
                try:
                    status, msg_data = self.mail.fetch(uid, '(RFC822)')
                    
                    if status != 'OK':
                        continue
                    
                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    
                    subject = self.decode_mime_header(msg.get('Subject', 'No Subject'))
                    sender = self.decode_mime_header(msg.get('From', 'Unknown Sender'))
                    date_str = msg.get('Date', '')
                    
                    date_info = self._parse_email_date(date_str)
                    date_formatted = date_info["formatted_date"]
                    
                    body = self.extract_email_body(msg)
                    has_attachments = self._has_attachments(msg)
                    
                    email_obj = EmailMessage(
                        uid=uid.decode('utf-8'),
                        subject=subject,
                        sender=sender,
                        date=date_formatted,
                        body=body[:MAX_BODY_LENGTH],
                        has_attachments=has_attachments,
                        is_read=False,
                        date_info=date_info
                    )
                    
                    emails.append(email_obj)
                    
                except Exception as e:
                    logger.error(f"Error processing email {uid}: {e}")
                    continue
            
            logger.info(f"Successfully fetched {len(emails)} unread emails")
            return emails
            
        except Exception as e:
            logger.error(f"Error fetching unread emails: {e}")
            return []
    
    async def fetch_recent_emails(self, count: int = 30) -> List[EmailMessage]:
        if not self.connected:
            if not await self.connect():
                return []
        
        emails = []
        
        try:
            status, messages = self.mail.search(None, 'ALL')
            
            if status != 'OK':
                logger.error("Failed to search emails")
                return []
            
            message_uids = messages[0].split()
            recent_uids = message_uids[-count:] if len(message_uids) >= count else message_uids
            recent_uids.reverse()
            
            for uid in recent_uids:
                try:
                    status, msg_data = self.mail.fetch(uid, '(RFC822)')
                    
                    if status != 'OK':
                        continue
                    
                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    
                    subject = self.decode_mime_header(msg.get('Subject', 'No Subject'))
                    sender = self.decode_mime_header(msg.get('From', 'Unknown Sender'))
                    date_str = msg.get('Date', '')
                    
                    date_info = self._parse_email_date(date_str)
                    date_formatted = date_info["formatted_date"]
                    
                    body = self.extract_email_body(msg)
                    has_attachments = self._has_attachments(msg)
                    
                    email_obj = EmailMessage(
                        uid=uid.decode('utf-8'),
                        subject=subject,
                        sender=sender,
                        date=date_formatted,
                        body=body[:MAX_BODY_LENGTH],
                        has_attachments=has_attachments,
                        is_read=True,  # Assume read for recent emails analysis
                        date_info=date_info
                    )
                    
                    emails.append(email_obj)
                    
                except Exception as e:
                    logger.error(f"Error processing email {uid}: {e}")
                    continue
            
            logger.info(f"Successfully fetched {len(emails)} recent emails")
            return emails
            
        except Exception as e:
            logger.error(f"Error fetching recent emails: {e}")
            return []

    async def fetch_email_statistics(self, count: int = 100, keywords: List[str] = None) -> EmailStats:
        if not self.connected:
            if not await self.connect():
                return EmailStats(
                    total_emails=0, unread_emails=0, read_emails=0,
                    emails_with_attachments=0, sender_distribution={},
                    domain_distribution={}, date_distribution={},
                    time_distribution={}, email_types={},
                    keyword_matches={}, recommendations=["Failed to connect to Gmail"]
                )
        
        try:
            status, messages = self.mail.search(None, 'ALL')
            total_count = len(messages[0].split()) if status == 'OK' and messages[0] else 0
            
            status, unread = self.mail.search(None, 'UNSEEN')
            unread_count = len(unread[0].split()) if status == 'OK' and unread[0] else 0
            
            sender_counter = Counter()
            domain_counter = Counter()
            date_counter = Counter()
            time_counter = Counter()
            type_counter = Counter()
            attachment_count = 0
            keyword_counter = Counter()
            keyword_matches = []
            if keywords is None:
                keywords = []
            
            status, messages = self.mail.search(None, 'ALL')
            if status != 'OK':
                raise Exception("Failed to search emails")
            
            message_uids = messages[0].split()
            recent_uids = message_uids[-count:] if len(message_uids) >= count else message_uids
            recent_uids.reverse()
            
            for uid in recent_uids:
                try:
                    status, msg_data = self.mail.fetch(uid, '(RFC822)')
                    if status != 'OK':
                        continue
                    
                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    
                    subject = self.decode_mime_header(msg.get('Subject', 'No Subject'))
                    sender = self.decode_mime_header(msg.get('From', 'Unknown Sender'))
                    date_str = msg.get('Date', '')
                    
                    sender_counter[sender] += 1
                    domain = self.extract_sender_domain(sender)
                    domain_counter[domain] += 1
                    
                    try:
                        date_tuple = parsedate_tz(date_str)
                        if date_tuple:
                            dt = datetime.fromtimestamp(mktime_tz(date_tuple))
                            date_counter[dt.strftime('%Y-%m-%d')] += 1
                            hour = dt.hour
                            if 5 <= hour < 12:
                                time_counter['morning'] += 1
                            elif 12 <= hour < 17:
                                time_counter['afternoon'] += 1
                            elif 17 <= hour < 21:
                                time_counter['evening'] += 1
                            else:
                                time_counter['night'] += 1
                    except:
                        pass
                    
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_disposition() == 'attachment':
                                attachment_count += 1
                                break
                    
                    body = ""
                    try:
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == "text/plain":
                                    payload = part.get_payload(decode=True)
                                    if payload:
                                        body = payload.decode('utf-8', errors='ignore').strip()
                                        break
                        else:
                            if msg.get_content_type() == "text/plain":
                                payload = msg.get_payload(decode=True)
                                if payload:
                                    body = payload.decode('utf-8', errors='ignore').strip()
                    except Exception as e:
                        logger.debug(f"Error extracting body for keyword search: {e}")
                        body = ""
                    
                    if keywords:
                        keyword_results = self.search_keywords_in_email(keywords, subject, body)
                        for keyword, result in keyword_results.items():
                            keyword_counter[keyword] += result["total_count"]
                            keyword_matches.append({
                                "keyword": keyword,
                                "email_uid": uid.decode('utf-8'),
                                "subject": subject,
                                "sender": sender,
                                "date": date_str,
                                "match_location": result["location"],
                                "match_count": result["total_count"],
                                "context": result["context"]
                            })
                    
                    email_type = self.classify_email_type(subject, sender, body)
                    type_counter[email_type] += 1
                    
                except Exception as e:
                    logger.error(f"Error processing email {uid}: {e}")
                    continue
            
            # Generate recommendations
            recommendations = self._generate_monitor_recommendations(
                total_count, unread_count, sender_counter, domain_counter, 
                type_counter, attachment_count, count, keyword_counter
            )
            
            return EmailStats(
                total_emails=total_count,
                unread_emails=unread_count,
                read_emails=total_count - unread_count,
                emails_with_attachments=attachment_count,
                sender_distribution=dict(sender_counter.most_common(10)),
                domain_distribution=dict(domain_counter.most_common(10)),
                date_distribution=dict(sorted(date_counter.items())[-7:]),
                time_distribution=dict(time_counter),
                email_types=dict(type_counter),
                keyword_matches=dict(keyword_counter),
                recommendations=recommendations
            )
            
        except Exception as e:
            logger.error(f"Error fetching email statistics: {e}")
            return EmailStats(
                total_emails=0, unread_emails=0, read_emails=0,
                emails_with_attachments=0, sender_distribution={},
                domain_distribution={}, date_distribution={},
                time_distribution={}, email_types={},
                keyword_matches={}, recommendations=[f"Error analyzing emails: {str(e)}"]
            )
    
    def _generate_monitor_recommendations(self, total_count: int, unread_count: int, 
                                        sender_counter: Counter, domain_counter: Counter,
                                        type_counter: Counter, attachment_count: int, 
                                        analyzed_count: int, keyword_counter: Counter = None) -> List[str]:
        recommendations = []
        
        if unread_count > 50:
            recommendations.append(f"High unread count ({unread_count}). Consider processing or archiving old emails.")
        elif unread_count > 20:
            recommendations.append(f"Moderate unread count ({unread_count}). Review and prioritize important emails.")
        
        top_senders = sender_counter.most_common(3)
        if top_senders:
            top_sender, count = top_senders[0]
            if count > analyzed_count * 0.2:
                recommendations.append(f"High frequency sender detected: {top_sender} ({count} emails). Consider filtering or creating a rule.")
        
        marketing_domains = ['marketing', 'newsletter', 'noreply', 'no-reply']
        marketing_count = sum(count for domain, count in domain_counter.items() 
                            if any(keyword in domain.lower() for keyword in marketing_domains))
        
        if marketing_count > analyzed_count * 0.3:
            recommendations.append(f"High marketing email volume ({marketing_count}). Consider unsubscribing or filtering.")
        
        if type_counter.get('marketing', 0) > analyzed_count * 0.4:
            recommendations.append("High marketing email ratio. Review subscriptions and set up filters.")
        
        if type_counter.get('notification', 0) > analyzed_count * 0.3:
            recommendations.append("Many notification emails. Consider consolidating or reducing notification frequency.")
        
        if attachment_count > analyzed_count * 0.1:
            recommendations.append(f"Many emails with attachments ({attachment_count}). Review storage and download important files.")
        
        if keyword_counter:
            total_keyword_matches = sum(keyword_counter.values())
            if total_keyword_matches > 0:
                top_keywords = keyword_counter.most_common(3)
                recommendations.append(f"Keyword tracking found {total_keyword_matches} matches. Top keywords: {', '.join([f'{k}({c})' for k, c in top_keywords])}")
        
        if total_count > 5000:
            recommendations.append("Large inbox size. Consider archiving old emails or using folders for organization.")
        
        if not recommendations:
            recommendations.append("Inbox health looks good! Continue current email management practices.")
        
        return recommendations


class EmailProcessor:
    
    def __init__(self):
        pass
    
    def prepare_emails_for_analysis(self, emails: List[EmailMessage]) -> List[Dict[str, Any]]:
        email_data = []
        for email in emails:
            # Truncate body more aggressively and add preview indicator
            body_preview = email.body[:200] if email.body else ""
            if len(email.body) > 200:
                body_preview += "... [truncated]"
            
            email_info = {
                "uid": email.uid,
                "subject": email.subject,
                "sender": email.sender,
                "date": email.date,
                "body_preview": body_preview,  # Changed from 'body' to 'body_preview'
                "body_length": len(email.body) if email.body else 0,  # Add length info
                "has_attachments": email.has_attachments,
                "is_read": email.is_read
            }
            
            if email.date_info:
                date_info = email.date_info
                # Only include essential date info to reduce payload
                email_info.update({
                    "is_recent": date_info.get("is_recent", False),
                    "is_today": date_info.get("is_today", False),
                    "age_hours": round(date_info.get("age_hours", 0), 1) if date_info.get("age_hours") else None,
                    "time_period": date_info.get("time_period", "unknown")
                })
            
            email_data.append(email_info)
        
        return email_data
    
    def group_emails_by_sender(self, emails: List[Dict[str, Any]]) -> Dict[str, Any]:
        sender_groups = defaultdict(list)
        for email in emails:
            sender = email.get("sender", "Unknown")
            sender_groups[sender].append(email)
        
        return {
            "total_senders": len(sender_groups),
            "groups": dict(sender_groups),
            "group_stats": {sender: len(emails) for sender, emails in sender_groups.items()}
        }
    
    def create_date_timeline(self, emails: List[Dict[str, Any]]) -> Dict[str, Any]:
        timeline = defaultdict(list)
        
        for email in emails:
            email_date = email.get("date", "")
            if email_date:
                try:
                    date_part = email_date.split()[0] if " " in email_date else email_date[:10]
                    timeline[date_part].append({
                        "subject": email.get("subject", ""),
                        "sender": email.get("sender", ""),
                        "time": email_date,
                        "has_attachments": email.get("has_attachments", False)
                    })
                except:
                    timeline["unknown_date"].append(email)
        
        sorted_dates = sorted([d for d in timeline.keys() if d != "unknown_date"])
        
        return {
            "total_dates": len(timeline),
            "date_range": {"start": sorted_dates[0] if sorted_dates else None, "end": sorted_dates[-1] if sorted_dates else None},
            "timeline": dict(timeline),
            "daily_counts": {date: len(emails) for date, emails in timeline.items()}
        }


gmail_client = GmailClient()
email_processor = EmailProcessor()
server = Server("email-server")

@server.list_tools()
async def list_tools() -> List[Tool]:
    return [
        Tool(
            name="comprehensive_email_analysis",
            description="Complete email summary analysis including unread emails, sender grouping, timeline, and structured data in one comprehensive report",
            inputSchema={
                "type": "object",
                "properties": {
                    "unread_count": {
                        "type": "integer",
                        "description": "Number of unread emails to analyze/summarize (default: 50, max: 100)",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 100
                    },
                    "recent_count": {
                        "type": "integer",
                        "description": "Number of recent emails to include for comprehensive analysis (default: 100, max: 200)",
                        "default": 100,
                        "minimum": 20,
                        "maximum": 200
                    },
                    "include_timeline": {
                        "type": "boolean",
                        "description": "Include date-based timeline analysis (default: true)",
                        "default": True
                    },
                    "include_sender_groups": {
                        "type": "boolean",
                        "description": "Include sender grouping analysis (default: true)",
                        "default": True
                    },
                    "include_metadata": {
                        "type": "boolean",
                        "description": "Include detailed metadata for each email (default: true)",
                        "default": True
                    }
                }
            }
        ),
        Tool(
            name="comprehensive_email_monitoring",
            description="Complete email monitoring analysis including statistics, sender analysis, domain analysis, email classification, keyword tracking, and recommendations in one comprehensive report",
            inputSchema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of emails to analyze (default: 200, max: 500)",
                        "default": 200,
                        "minimum": 50,
                        "maximum": 500
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of keywords to track in emails (e.g., ['meeting', 'urgent', 'deadline', 'sports'])",
                        "default": []
                    },
                    "top_senders_limit": {
                        "type": "integer",
                        "description": "Number of top senders to include in analysis (default: 15, max: 50)",
                        "default": 15,
                        "minimum": 5,
                        "maximum": 50
                    },
                    "include_productivity_score": {
                        "type": "boolean",
                        "description": "Include productivity score calculation based on email types (default: true)",
                        "default": True
                    },
                    "include_detailed_recommendations": {
                        "type": "boolean",
                        "description": "Include detailed recommendations and insights (default: true)",
                        "default": True
                    }
                }
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> List[TextContent]:
    
    if name == "comprehensive_email_analysis":
        unread_count = arguments.get("unread_count", 50)
        recent_count = arguments.get("recent_count", 100)
        include_timeline = arguments.get("include_timeline", True)
        include_sender_groups = arguments.get("include_sender_groups", True)
        include_metadata = arguments.get("include_metadata", True)
        
        try:
            unread_emails = await gmail_client.fetch_unread_emails(unread_count)
            recent_emails = await gmail_client.fetch_recent_emails(recent_count)
            
            if not unread_emails and not recent_emails:
                return [TextContent(type="text", text="No emails found or failed to connect to Gmail")]
            
            processed_unread = email_processor.prepare_emails_for_analysis(unread_emails) if unread_emails else []
            processed_recent = email_processor.prepare_emails_for_analysis(recent_emails) if recent_emails else []
            
            result = {
                "analysis_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "summary": {
                    "total_unread": len(unread_emails),
                    "total_recent_analyzed": len(recent_emails),
                    "unread_with_attachments": len([e for e in processed_unread if e.get("has_attachments", False)]),
                    "recent_emails_today": len([e for e in processed_recent if e.get("is_today", False)]),
                    "unread_emails_today": len([e for e in processed_unread if e.get("is_today", False)])
                },
                "unread_emails": {
                    "count": len(processed_unread),
                    "emails": processed_unread if include_metadata else [{"subject": e.get("subject", ""), "sender": e.get("sender", ""), "date": e.get("date", ""), "body_length": e.get("body_length", 0)} for e in processed_unread],
                    "recent_unread_count": len([email for email in processed_unread if email.get("is_recent", False)]),
                    "today_unread_count": len([email for email in processed_unread if email.get("is_today", False)]),
                    "emails_with_attachments_count": len([email for email in processed_unread if email.get("has_attachments", False)])
                },
                "recent_emails": {
                    "count": len(processed_recent),
                    "emails": processed_recent if include_metadata else [{"subject": e.get("subject", ""), "sender": e.get("sender", ""), "date": e.get("date", ""), "body_length": e.get("body_length", 0)} for e in processed_recent]
                }
            }
            
            if include_sender_groups:
                # Only include stats, not full email groups to reduce payload
                unread_groups = email_processor.group_emails_by_sender(processed_unread) if processed_unread else {"total_senders": 0, "group_stats": {}}
                recent_groups = email_processor.group_emails_by_sender(processed_recent) if processed_recent else {"total_senders": 0, "group_stats": {}}
                combined_groups = email_processor.group_emails_by_sender(processed_unread + processed_recent) if (processed_unread or processed_recent) else {"total_senders": 0, "group_stats": {}}
                
                result["sender_analysis"] = {
                    "unread_sender_stats": {"total_senders": unread_groups["total_senders"], "group_stats": unread_groups["group_stats"]},
                    "recent_sender_stats": {"total_senders": recent_groups["total_senders"], "group_stats": recent_groups["group_stats"]},
                    "combined_sender_stats": {"total_senders": combined_groups["total_senders"], "group_stats": combined_groups["group_stats"]}
                }
            
            if include_timeline:
                # Only include summary stats, not full timeline details to reduce payload
                unread_timeline = email_processor.create_date_timeline(processed_unread) if processed_unread else {"total_dates": 0, "date_range": {"start": None, "end": None}, "daily_counts": {}}
                recent_timeline = email_processor.create_date_timeline(processed_recent) if processed_recent else {"total_dates": 0, "date_range": {"start": None, "end": None}, "daily_counts": {}}
                combined_timeline = email_processor.create_date_timeline(processed_unread + processed_recent) if (processed_unread or processed_recent) else {"total_dates": 0, "date_range": {"start": None, "end": None}, "daily_counts": {}}
                
                result["timeline_analysis"] = {
                    "unread_timeline_summary": {"total_dates": unread_timeline["total_dates"], "date_range": unread_timeline["date_range"], "daily_counts": unread_timeline["daily_counts"]},
                    "recent_timeline_summary": {"total_dates": recent_timeline["total_dates"], "date_range": recent_timeline["date_range"], "daily_counts": recent_timeline["daily_counts"]},
                    "combined_timeline_summary": {"total_dates": combined_timeline["total_dates"], "date_range": combined_timeline["date_range"], "daily_counts": combined_timeline["daily_counts"]}
                }
            
            result["insights"] = {
                "unread_backlog": "High" if len(processed_unread) > 30 else "Medium" if len(processed_unread) > 10 else "Low",
                "attachment_ratio": round((len([e for e in processed_unread if e.get("has_attachments", False)]) / len(processed_unread) * 100) if processed_unread else 0, 1),
                "recent_activity": "High" if len([e for e in processed_recent if e.get("is_today", False)]) > 10 else "Medium" if len([e for e in processed_recent if e.get("is_today", False)]) > 5 else "Low",
                "recommendations": _generate_summary_recommendations(processed_unread, processed_recent)
            }
            
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        except Exception as e:
            logger.error(f"Error in comprehensive_email_analysis: {e}")
            return [TextContent(type="text", text=f"Error performing comprehensive analysis: {str(e)}")]
    
    elif name == "comprehensive_email_monitoring":
        count = arguments.get("count", 200)
        keywords = arguments.get("keywords", [])
        top_senders_limit = arguments.get("top_senders_limit", 15)
        include_productivity_score = arguments.get("include_productivity_score", True)
        include_detailed_recommendations = arguments.get("include_detailed_recommendations", True)
        
        try:
            stats = await gmail_client.fetch_email_statistics(count, keywords)
            
            # Calculate type percentages for productivity score
            type_percentages = {}
            for email_type, type_count in stats.email_types.items():
                type_percentages[email_type] = round((type_count / count * 100) if count > 0 else 0, 1)
            
            # Categorize domains
            business_domains = []
            personal_domains = []
            service_domains = []
            
            common_personal = ['gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'icloud.com']
            common_service = ['noreply', 'no-reply', 'donotreply', 'support', 'service']
            
            for domain, count_val in stats.domain_distribution.items():
                if domain in common_personal:
                    personal_domains.append({"domain": domain, "count": count_val})
                elif any(service in domain.lower() for service in common_service):
                    service_domains.append({"domain": domain, "count": count_val})
                else:
                    business_domains.append({"domain": domain, "count": count_val})
            
            # Generate comprehensive insights
            comprehensive_insights = []
            if type_percentages.get('marketing', 0) > 30:
                comprehensive_insights.append("High marketing email volume - consider subscription management")
            if type_percentages.get('work', 0) > 50:
                comprehensive_insights.append("Work-heavy inbox - ensure proper prioritization")
            if type_percentages.get('automated', 0) > 40:
                comprehensive_insights.append("Many automated emails - review notification settings")
            if type_percentages.get('personal', 0) < 10:
                comprehensive_insights.append("Low personal email ratio - inbox may be too business-focused")
            
            # Sender concentration analysis
            sender_concentration = {
                "top_3_percentage": sum(list(stats.sender_distribution.values())[:3]) / count * 100 if count > 0 else 0,
                "top_10_percentage": sum(list(stats.sender_distribution.values())[:10]) / count * 100 if count > 0 else 0
            }
            
            # Keyword analysis if keywords provided
            keyword_analysis = None
            if keywords:
                keyword_analysis = {
                    "tracked_keywords": keywords,
                    "keyword_matches": stats.keyword_matches,
                    "total_matches": sum(stats.keyword_matches.values()),
                    "match_rate_percentage": round((sum(stats.keyword_matches.values()) / count * 100) if count > 0 else 0, 2),
                    "keyword_insights": []
                }
                
                for keyword, match_count in stats.keyword_matches.items():
                    if match_count > 0:
                        match_rate = round((match_count / count * 100), 2)
                        keyword_analysis["keyword_insights"].append({
                            "keyword": keyword,
                            "matches": match_count,
                            "match_rate_percentage": match_rate,
                            "recommendation": f"Found {match_count} emails containing '{keyword}' ({match_rate}% of analyzed emails)"
                        })
                
                if not keyword_analysis["keyword_insights"]:
                    keyword_analysis["keyword_insights"].append({
                        "message": "No matches found for any of the tracked keywords in the analyzed emails"
                    })
            
            # Build comprehensive report
            comprehensive_report = {
                "analysis_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "summary": {
                    "total_emails": stats.total_emails,
                    "unread_emails": stats.unread_emails,
                    "read_emails": stats.read_emails,
                    "read_percentage": round((stats.read_emails / stats.total_emails * 100) if stats.total_emails > 0 else 0, 1),
                    "emails_with_attachments": stats.emails_with_attachments,
                    "analyzed_emails": count,
                    "unique_senders": len(stats.sender_distribution),
                    "unique_domains": len(stats.domain_distribution)
                },
                "sender_analysis": {
                    "total_unique_senders": len(stats.sender_distribution),
                    "top_senders": dict(list(stats.sender_distribution.items())[:top_senders_limit]),
                    "sender_concentration": sender_concentration,
                    "full_distribution": stats.sender_distribution
                },
                "domain_analysis": {
                    "total_unique_domains": len(stats.domain_distribution),
                    "domain_distribution": stats.domain_distribution,
                    "categorized_domains": {
                        "business": business_domains,
                        "personal": personal_domains,
                        "service_automated": service_domains
                    },
                    "domain_diversity": round(len(stats.domain_distribution) / count if count > 0 else 0, 3)
                },
                "email_type_analysis": {
                    "email_type_counts": stats.email_types,
                    "email_type_percentages": type_percentages,
                    "type_insights": comprehensive_insights
                },
                "temporal_analysis": {
                    "date_distribution": stats.date_distribution,
                    "time_distribution": stats.time_distribution
                }
            }
            
            if include_productivity_score:
                comprehensive_report["productivity_analysis"] = _calculate_productivity_score(type_percentages)
            
            if keyword_analysis:
                comprehensive_report["keyword_analysis"] = keyword_analysis
            
            if include_detailed_recommendations:
                comprehensive_report["recommendations"] = stats.recommendations
                comprehensive_report["actionable_insights"] = _generate_actionable_insights(stats, type_percentages, sender_concentration)
            
            return [TextContent(type="text", text=json.dumps(comprehensive_report, indent=2))]
            
        except Exception as e:
            logger.error(f"Error in comprehensive_email_monitoring: {e}")
            return [TextContent(type="text", text=f"Error performing comprehensive monitoring: {str(e)}")]
    
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

def _generate_summary_recommendations(unread_emails: List[Dict[str, Any]], recent_emails: List[Dict[str, Any]]) -> List[str]:
    recommendations = []
    
    if len(unread_emails) > 50:
        recommendations.append("High unread count detected. Consider prioritizing or archiving emails.")
    elif len(unread_emails) > 20:
        recommendations.append("Moderate unread backlog. Review and process important emails first.")
    
    unread_today = len([e for e in unread_emails if e.get("is_today", False)])
    if unread_today > 10:
        recommendations.append(f"Multiple unread emails today ({unread_today}). Check for urgent items.")
    
    attachment_emails = len([e for e in unread_emails if e.get("has_attachments", False)])
    if attachment_emails > 5:
        recommendations.append(f"Several unread emails with attachments ({attachment_emails}). Review for important documents.")
    
    all_emails = unread_emails + recent_emails
    senders = {}
    for email in all_emails:
        sender = email.get("sender", "Unknown")
        senders[sender] = senders.get(sender, 0) + 1
    
    frequent_senders = [(sender, count) for sender, count in senders.items() if count > 5]
    if frequent_senders:
        top_sender = max(frequent_senders, key=lambda x: x[1])
        recommendations.append(f"Frequent sender detected: {top_sender[0]} ({top_sender[1]} emails). Consider filtering or priority rules.")
    
    if not recommendations:
        recommendations.append("Email management looks healthy. Continue current practices.")
    
    return recommendations

def _generate_actionable_insights(stats, type_percentages: Dict[str, float], sender_concentration: Dict[str, float]) -> List[str]:
    insights = []
    
    # Unread analysis
    if stats.unread_emails > 100:
        insights.append("URGENT: Very high unread count. Consider bulk archiving or creating priority filters.")
    elif stats.unread_emails > 50:
        insights.append("HIGH: Significant unread backlog. Schedule dedicated email processing time.")
    
    # Sender concentration
    if sender_concentration["top_3_percentage"] > 40:
        insights.append("HIGH: Top 3 senders dominate inbox. Create specific folders or filters for frequent senders.")
    
    # Email type balance
    marketing_ratio = type_percentages.get('marketing', 0)
    work_ratio = type_percentages.get('work', 0)
    
    if marketing_ratio > 40:
        insights.append("HIGH: Marketing emails exceed 40%. Unsubscribe from unnecessary lists and set up filters.")
    if work_ratio > 70:
        insights.append("MEDIUM: Work emails dominate (>70%). Ensure work-life balance in email habits.")
    if work_ratio < 20 and marketing_ratio > 30:
        insights.append("MEDIUM: Low work-to-marketing ratio suggests need for better filtering.")
    
    # Productivity insights
    if type_percentages.get('notification', 0) > 25:
        insights.append("MEDIUM: High notification volume. Review and reduce automated email subscriptions.")
    
    # Domain insights
    if len(stats.domain_distribution) < 5:
        insights.append("LOW: Limited domain diversity. Consider expanding professional network or interests.")
    elif len(stats.domain_distribution) > 50:
        insights.append("MEDIUM: Very high domain diversity. Consider consolidating or organizing communications.")
    
    if not insights:
        insights.append("GOOD: Email patterns appear healthy and well-managed.")
    
    return insights

def _calculate_productivity_score(type_percentages: Dict[str, float]) -> Dict[str, Any]:
    work_weight = type_percentages.get('work', 0) * 0.3
    personal_weight = type_percentages.get('personal', 0) * 0.2
    marketing_penalty = type_percentages.get('marketing', 0) * -0.2
    notification_penalty = type_percentages.get('notification', 0) * -0.1
    
    raw_score = work_weight + personal_weight + marketing_penalty + notification_penalty + 50
    productivity_score = max(0, min(100, raw_score))
    
    if productivity_score >= 80:
        rating = "Excellent"
    elif productivity_score >= 60:
        rating = "Good"
    elif productivity_score >= 40:
        rating = "Fair"
    else:
        rating = "Needs Improvement"
    
    return {
        "score": round(productivity_score, 1),
        "rating": rating,
        "factors": {
            "work_focus": work_weight,
            "personal_balance": personal_weight,
            "marketing_overhead": marketing_penalty,
            "notification_overhead": notification_penalty
        }
    }

async def main():
    if not EMAIL_ADDRESS or not APP_PASSWORD:
        print("ERROR: EMAIL_ADDRESS and APP_PASSWORD environment variables must be set.", file=sys.stderr)
        sys.exit(1)
    logger.info("Starting Email Server...")

    if await gmail_client.connect():
        logger.info("Gmail connection successful")
        gmail_client.disconnect()
    else:
        logger.warning("Gmail connection failed - tools will attempt to reconnect when used")
    
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())